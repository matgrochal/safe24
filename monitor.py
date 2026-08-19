#!/usr/bin/env python3
"""
Monitoring sklepu sklep.technica.pl.

Sprawdza:
  * dostępność stron (kod HTTP z interpretacją 4xx/5xx),
  * obecność kluczowych elementów (przyciski CTA, frazy kontrolne),
  * zmiany treści (hash + czytelny diff),
  * zmiany pilnowanych wartości (cena, kod produktu, dane kontaktowe),
  * scenariusze zakupowe: dodanie produktu do koszyka -> /cart -> /checkout
    razem z przyciskami "Przejdź do kasy", "Zapytaj o ofertę", "Zamów i zapłać".

Powiadomienia: Discord, Telegram, Microsoft Teams (Workflows), SMS (SMSAPI.pl).

Harmonogram jest ustawiany OSOBNO DLA KAŻDEJ STRONY:
  check_every_minutes  – jak często sprawdzać,
  alert_after_minutes  – jak długo musi trwać awaria, zanim przyjdzie alarm.

Uruchomienie:
    python monitor.py                       # jeden przebieg
    python monitor.py --loop-minutes 9      # pętla przez 9 minut (co 60 s)
    python monitor.py --only checkout       # tylko wybrany wpis
    python monitor.py --dry-run             # bez wysyłki i bez zapisu stanu
    python monitor.py --test-alerts         # wiadomość testowa (bez SMS)
    python monitor.py --test-alerts --with-sms   # wiadomość testowa też SMS-em
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "state.json"
SNAPSHOT_DIR = STATE_DIR / "snapshots"

MAX_SNAPSHOT_BYTES = 512_000
MAX_DIFF_LINES = 20
MAX_DIFF_LINE_LEN = 160
MAX_RETRY_AFTER = 60

DEFAULT_UA = "Mozilla/5.0 (compatible; TechnicaMonitorBot/1.0; +https://sklep.technica.pl)"
RUN_URL = os.environ.get("RUN_URL", "")

LEVELS = {
    "down":    {"icon": "🔴", "color": 0xE01E1E, "teams": "Attention", "label": "AWARIA"},
    "up":      {"icon": "🟢", "color": 0x2ECC71, "teams": "Good",      "label": "PRZYWRÓCONA"},
    "changed": {"icon": "🟡", "color": 0xF1C40F, "teams": "Warning",   "label": "ZMIANA TREŚCI"},
    "field":   {"icon": "🟠", "color": 0xE67E22, "teams": "Warning",   "label": "ZMIANA DANYCH"},
    "info":    {"icon": "ℹ️", "color": 0x3498DB, "teams": "Accent",    "label": "INFO"},
}

# =========================================================================
# Interpretacja kodów HTTP
# =========================================================================
STATUS_INFO: dict[int, dict[str, str]] = {
    400: {"name": "Bad Request",
          "desc": "Nieprawidłowe żądanie — serwer nie potrafi go zinterpretować.",
          "hint": "Sprawdź poprawność adresu URL i parametrów zapytania."},
    401: {"name": "Unauthorized",
          "desc": "Wymagane uwierzytelnienie — strona żąda logowania.",
          "hint": "Czy strona nie została zamknięta hasłem (np. .htpasswd)?"},
    403: {"name": "Forbidden",
          "desc": "Dostęp zabroniony — serwer rozumie żądanie, ale odmawia autoryzacji. "
                  "Zwykle problem z uprawnieniami albo blokada przez zaporę WAF.",
          "hint": "Sprawdź reguły WAF/Cloudflare, blokady IP i uprawnienia katalogów (755/644). "
                  "Częsta przyczyna: bot monitorujący uznany za niepożądany ruch."},
    404: {"name": "Not Found",
          "desc": "Nie znaleziono strony — podany adres nie istnieje na serwerze.",
          "hint": "Czy produkt/kategoria nie została usunięta lub czy nie zmienił się adres URL? "
                  "Jeśli zmiana jest celowa — popraw config.json i dodaj przekierowanie 301."},
    405: {"name": "Method Not Allowed",
          "desc": "Metoda HTTP niedozwolona dla tego zasobu.",
          "hint": "Sprawdź regułę serwera lub WAF blokującą tę metodę."},
    410: {"name": "Gone",
          "desc": "Zasób trwale usunięty przez właściciela serwisu.",
          "hint": "Jeśli to celowe — usuń adres z config.json."},
    429: {"name": "Too Many Requests",
          "desc": "Zbyt wiele żądań — serwer blokuje ruch z powodu przekroczenia limitu "
                  "zapytań (rate limiting).",
          "hint": "Zwiększ check_every_minutes lub dodaj IP monitoringu do wyjątków "
                  "w zaporze / rate limiterze."},
    500: {"name": "Internal Server Error",
          "desc": "Wewnętrzny błąd serwera lub aplikacji — najczęściej błąd w kodzie PHP "
                  "albo brak połączenia z bazą danych.",
          "hint": "Sprawdź logi błędów PHP i logi aplikacji sklepu z ostatnich minut."},
    502: {"name": "Bad Gateway",
          "desc": "Błędna brama — serwer pośredniczący (Nginx/Cloudflare) otrzymał "
                  "nieprawidłową odpowiedź od serwera głównego.",
          "hint": "Sprawdź, czy działa PHP-FPM / backend i czy proces nie został ubity (OOM)."},
    503: {"name": "Service Unavailable",
          "desc": "Usługa niedostępna — serwer przeciążony albo trwa aktualizacja/konserwacja.",
          "hint": "Sprawdź obciążenie serwera i tryb maintenance sklepu."},
    504: {"name": "Gateway Timeout",
          "desc": "Przekroczono czas oczekiwania bramy — serwer nie odpowiedział w limicie czasu. "
                  "Częsty objaw problemu z bazą danych.",
          "hint": "Sprawdź czas wykonania zapytań SQL i obciążenie bazy."},
}

CLASS_INFO = {
    4: {"name": "Błąd żądania (4xx)", "desc": "Serwer odrzucił żądanie po stronie klienta.",
        "hint": "Sprawdź adres URL oraz reguły blokad na serwerze."},
    5: {"name": "Błąd serwera (5xx)", "desc": "Serwer nie obsłużył poprawnego żądania.",
        "hint": "Sprawdź logi serwera i aplikacji."},
}


def describe_status(code: int) -> dict[str, str]:
    if code in STATUS_INFO:
        return STATUS_INFO[code]
    return CLASS_INFO.get(code // 100,
                          {"name": "Nieoczekiwany kod odpowiedzi", "desc": "", "hint": ""})


def status_line(code: int) -> str:
    info = describe_status(code)
    return f"HTTP {code} {info['name']} — {info['desc']}".strip(" —")


# =========================================================================
# Pomocnicze
# =========================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def human_duration(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower() or "site"


def strip_diacritics(text: str) -> str:
    """
    Usuwa polskie znaki diakrytyczne. Ważne dla SMS: jeden znak spoza GSM 7-bit
    skraca pojedynczą wiadomość ze 160 do 70 znaków, czyli podnosi koszt wysyłki.
    Litery ł/Ł wymagają osobnej podmiany, bo NFKD ich nie rozkłada.
    """
    text = text.replace("ł", "l").replace("Ł", "L")
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


# =========================================================================
# Konfiguracja i stan
# =========================================================================
def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("sites"):
        raise SystemExit("Konfiguracja nie zawiera żadnych wpisów w polu 'sites'.")
    return cfg


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print("[!] state.json uszkodzony — startuję od zera.")
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def snapshot_path(key: str) -> Path:
    return SNAPSHOT_DIR / f"{key}.txt"


def read_snapshot(key: str) -> str | None:
    p = snapshot_path(key)
    return p.read_text(encoding="utf-8") if p.exists() else None


def write_snapshot(key: str, text: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")[:MAX_SNAPSHOT_BYTES].decode("utf-8", "ignore")
    snapshot_path(key).write_text(data, encoding="utf-8")


# =========================================================================
# Pobieranie
# =========================================================================
def build_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl,en;q=0.8",
    })
    return s


def request(session: requests.Session, method: str, url: str, timeout: int,
            retries: int, delay: int, retry_on: list[int], **kwargs):
    """Zwraca (response, error_text)."""
    last_error = None
    for attempt in range(1, retries + 2):
        try:
            resp = session.request(method, url, timeout=timeout, allow_redirects=True, **kwargs)
            if "charset" not in resp.headers.get("Content-Type", "").lower():
                resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code in retry_on and attempt <= retries:
                wait = delay
                if resp.status_code == 429:
                    try:
                        wait = min(int(resp.headers.get("Retry-After", delay)), MAX_RETRY_AFTER)
                    except ValueError:
                        wait = delay
                print(f"    ponawiam po HTTP {resp.status_code} (odczekanie {wait}s)…")
                last_error = f"HTTP {resp.status_code}"
                time.sleep(wait)
                continue
            return resp, None
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print(f"    błąd połączenia: {last_error}")
            if attempt <= retries:
                time.sleep(delay)
    return None, last_error


# =========================================================================
# Normalizacja i porównywanie treści
# =========================================================================
def cut_between(text: str, start: str | None, end: str | None,
                occurrence: str = "first") -> str:
    """
    Obcina tekst do fragmentu między znacznikami. W AtomStore każda podstrona
    zawiera to samo menu kategorii (tysiące linków) i tę samą stopkę — bez
    tego cięcia hash zmieniałby się przy edycji dowolnej kategorii w sklepie.
    """
    if start:
        idx = text.rfind(start) if occurrence == "last" else text.find(start)
        if idx != -1:
            text = text[idx + len(start):]
    if end:
        idx = text.find(end)
        if idx != -1:
            text = text[:idx]
    return text


def normalize_html(html: str, site: dict) -> str:
    text = html
    selector = site.get("selector")
    ignore_selectors = site.get("ignore_selectors") or []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        for sel in ignore_selectors:
            for tag in soup.select(sel):
                tag.decompose()
        if selector:
            nodes = soup.select(selector)
            text = ("\n".join(n.get_text("\n", strip=True) for n in nodes)
                    if nodes else soup.get_text("\n", strip=True))
        else:
            text = soup.get_text("\n", strip=True)
    else:
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)

    between = site.get("content_between") or {}
    text = cut_between(text, between.get("start"), between.get("end"),
                       between.get("start_occurrence", "first"))

    default_patterns = [
        r'nonce="[^"]*"',
        r'(?i)csrf[-_]?token"?\s*[:=]\s*"[^"]*"',
        r"\?v=[0-9a-f]{6,}",
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?\b",
        r"\b\d{10,13}\b",
    ]
    for pattern in default_patterns + list(site.get("ignore_patterns") or []):
        try:
            text = re.sub(pattern, "", text)
        except re.error as exc:
            print(f"[!] Błędne wyrażenie regularne '{pattern}': {exc}")

    lines = [re.sub(r"[\s\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_diff(old: str, new: str) -> str:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                lineterm="", n=0, fromfile="poprzednia", tofile="aktualna")
    out = []
    for line in diff:
        if line.startswith(("---", "+++", "@@")):
            continue
        if len(line) > MAX_DIFF_LINE_LEN:
            line = line[:MAX_DIFF_LINE_LEN] + " […]"
        out.append(line)
        if len(out) >= MAX_DIFF_LINES:
            out.append("… (dalsze zmiany pominięte)")
            break
    return "\n".join(out) or "(zmiana niewidoczna w tekście — np. wyłącznie w kodzie HTML)"


def extract_fields(text: str, watch_fields: dict) -> dict:
    result = {}
    for name, pattern in (watch_fields or {}).items():
        try:
            match = re.search(pattern, text, re.IGNORECASE)
        except re.error as exc:
            print(f"[!] Błędny wzorzec pola '{name}': {exc}")
            continue
        if not match:
            result[name] = "BRAK"
        else:
            value = match.group(1) if match.groups() else match.group(0)
            result[name] = re.sub(r"[\s\u00a0]+", " ", value).strip()
    return result


def find_missing_elements(html: str, required: dict) -> list[str]:
    """Zwraca nazwy elementów (przycisków CTA itd.), których nie ma na stronie."""
    missing = []
    for label, pattern in (required or {}).items():
        try:
            if not re.search(pattern, html, re.IGNORECASE | re.DOTALL):
                missing.append(label)
        except re.error as exc:
            print(f"[!] Błędny wzorzec elementu '{label}': {exc}")
    return missing


# =========================================================================
# Kanały powiadomień
# =========================================================================
def _post(url: str, channel: str, **kwargs) -> bool:
    try:
        r = requests.post(url, timeout=20, **kwargs)
        if r.status_code >= 300:
            print(f"[!] {channel}: HTTP {r.status_code} — {r.text[:300]}")
            return False
        print(f"[+] {channel}: wysłano powiadomienie.")
        return True
    except requests.RequestException as exc:
        print(f"[!] {channel}: błąd wysyłki — {exc}")
        return False


def notify_discord(event: dict) -> None:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    meta = LEVELS[event["level"]]
    description = event["body"]
    if event.get("diff"):
        description += f"\n\n```diff\n{event['diff']}\n```"
    if RUN_URL:
        description += f"\n\n[Log uruchomienia]({RUN_URL})"
    payload = {
        "username": "Monitor sklep.technica.pl",
        "embeds": [{
            "title": f"{meta['icon']} {event['title']}"[:250],
            "url": event["url"],
            "description": description[:4000],
            "color": meta["color"],
            "timestamp": iso(now_utc()),
        }],
    }
    _post(url, "Discord", json=payload)


def notify_telegram(event: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    meta = LEVELS[event["level"]]

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = f"{meta['icon']} <b>{esc(event['title'])}</b>\n{esc(event['body'])}"
    if event.get("diff"):
        text += f"\n\n<pre>{esc(event['diff'])}</pre>"
    text += f"\n\n<a href=\"{esc(event['url'])}\">Otwórz stronę</a>"
    if RUN_URL:
        text += f" · <a href=\"{esc(RUN_URL)}\">log</a>"
    payload = {"chat_id": chat_id, "text": text[:4000],
               "parse_mode": "HTML", "disable_web_page_preview": True}
    _post(f"https://api.telegram.org/bot{token}/sendMessage", "Telegram", json=payload)


def notify_teams(event: dict) -> None:
    """
    Teams przez webhook aplikacji Workflows (Power Automate) — Adaptive Card.
    Stare webhooki Office 365 Connectors Microsoft wyłączył w maju 2026.
    TEAMS_PAYLOAD=messagecard przełącza na starszy format.
    """
    url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        return
    meta = LEVELS[event["level"]]

    if os.environ.get("TEAMS_PAYLOAD", "adaptive").lower() == "messagecard":
        sections = [{"text": event["body"].replace("\n", "\n\n")}]
        if event.get("diff"):
            sections.append({"text": f"<pre>{event['diff']}</pre>"})
        payload = {
            "@type": "MessageCard", "@context": "http://schema.org/extensions",
            "themeColor": f"{meta['color']:06X}", "summary": event["title"],
            "title": f"{meta['icon']} {event['title']}", "sections": sections,
            "potentialAction": [{"@type": "OpenUri", "name": "Otwórz stronę",
                                 "targets": [{"os": "default", "uri": event["url"]}]}],
        }
        _post(url, "Teams (MessageCard)", json=payload)
        return

    body = [
        {"type": "TextBlock", "text": f"{meta['icon']} {event['title']}", "weight": "Bolder",
         "size": "Medium", "wrap": True, "color": meta["teams"]},
        {"type": "TextBlock", "text": event["body"], "wrap": True, "spacing": "Small"},
    ]
    if event.get("diff"):
        body.append({"type": "TextBlock", "text": event["diff"], "wrap": True,
                     "fontType": "Monospace", "size": "Small", "spacing": "Small"})
    actions = [{"type": "Action.OpenUrl", "title": "Otwórz stronę", "url": event["url"]}]
    if RUN_URL:
        actions.append({"type": "Action.OpenUrl", "title": "Log GitHub Actions", "url": RUN_URL})

    payload = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": body, "actions": actions}}]}
    _post(url, "Teams (Adaptive Card)", json=payload)


def notify_sms(event: dict) -> bool:
    """
    SMS przez SMSAPI.pl — POST https://api.smsapi.pl/sms.do, autoryzacja tokenem OAuth.
    SMS kosztuje, więc treść jest krótka, bez polskich znaków (normalize=1),
    a wysyłka podlega osobnym regułom (sms_levels + sms_cooldown_minutes).
    """
    token = os.environ.get("SMSAPI_TOKEN")
    recipients = os.environ.get("SMSAPI_TO")
    if not (token and recipients):
        print("[i] SMS pominięty — brak SMSAPI_TOKEN lub SMSAPI_TO.")
        return False

    sender = os.environ.get("SMSAPI_FROM", "").strip()
    text = strip_diacritics(event.get("sms") or f"{event['title']}: {event['body']}")
    text = re.sub(r"\s+", " ", text).strip()[:320]

    data = {"to": recipients, "message": text, "format": "json", "normalize": "1"}
    if sender:
        data["from"] = sender          # bez tego SMSAPI użyje pola "Info" (Eco)

    headers = {"Authorization": f"Bearer {token}"}
    for url in ("https://api.smsapi.pl/sms.do", "https://api2.smsapi.pl/sms.do"):
        try:
            r = requests.post(url, data=data, headers=headers, timeout=20)
            payload = r.json() if r.headers.get("Content-Type", "").startswith("application/json") \
                else {"raw": r.text[:300]}
            if r.status_code == 200 and "error" not in payload:
                print(f"[+] SMS: wysłano ({payload.get('count', '?')} wiadomości).")
                return True
            print(f"[!] SMS: odpowiedź {r.status_code} — {payload}")
        except requests.RequestException as exc:
            print(f"[!] SMS: błąd wysyłki przez {url} — {exc}")
    return False


def notify(event: dict, site: dict | None = None, cfg: dict | None = None,
           state_entry: dict | None = None, dry_run: bool = False) -> None:
    """Wysyła zdarzenie na kanały wskazane dla danej strony."""
    meta = LEVELS[event["level"]]
    print(f"\n{meta['icon']} [{meta['label']}] {event['title']}\n{event['body']}")
    if event.get("diff"):
        print(event["diff"])
    if dry_run:
        print("[dry-run] powiadomienia nie zostały wysłane.")
        return

    site = site or {}
    cfg = cfg or {}
    channels = site.get("notify", cfg.get("notify", ["teams", "discord", "telegram"]))

    if "discord" in channels:
        notify_discord(event)
    if "telegram" in channels:
        notify_telegram(event)
    if "teams" in channels:
        notify_teams(event)

    if "sms" in channels:
        sms_levels = site.get("sms_levels", cfg.get("sms_levels", ["down"]))
        if event["level"] not in sms_levels:
            return
        cooldown = site.get("sms_cooldown_minutes", cfg.get("sms_cooldown_minutes", 30))
        if state_entry is not None:
            last = parse_iso(state_entry.get("last_sms_at"))
            if last and (now_utc() - last) < timedelta(minutes=cooldown):
                print(f"[i] SMS pominięty — wysłano mniej niż {cooldown} min temu.")
                return
        if notify_sms(event) and state_entry is not None:
            state_entry["last_sms_at"] = iso(now_utc())


# =========================================================================
# Ocena awarii i progów czasowych
# =========================================================================
def is_immediate(code: int | None, rules: list) -> bool:
    if code is None:
        return False
    for rule in rules:
        if isinstance(rule, int) and rule == code:
            return True
        if isinstance(rule, str) and rule.lower().endswith("xx"):
            try:
                if code // 100 == int(rule[0]):
                    return True
            except ValueError:
                continue
    return False


def handle_failure(site: dict, cfg: dict, entry: dict, problem: str, hint: str,
                   code: int | None, dry_run: bool) -> None:
    """Zapisuje awarię i alarmuje dopiero po przekroczeniu progu czasowego."""
    now = now_utc()
    alert_after = site.get("alert_after_minutes", cfg.get("alert_after_minutes", 5))
    immediate = site.get("immediate_alert_codes", cfg.get("immediate_alert_codes", []))

    down_since = parse_iso(entry.get("down_since"))
    if down_since is None:
        down_since = now
        entry["down_since"] = iso(now)
    entry["status"] = "down"
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1

    minutes_down = (now - down_since).total_seconds() / 60
    print(f"[-] {problem}\n    awaria trwa {human_duration(minutes_down)} "
          f"(próg alarmu: {alert_after} min)")

    if entry.get("alerted_down"):
        return
    if minutes_down + 0.01 < alert_after and not is_immediate(code, immediate):
        print("    alarm wstrzymany — próg czasowy jeszcze nieprzekroczony.")
        return

    entry["alerted_down"] = True
    body = problem
    if hint:
        body += f"\n\nCo sprawdzić: {hint}"
    body += (f"\n\nAwaria trwa: {human_duration(minutes_down)} "
             f"(od {iso(down_since)} UTC)\nPróg alarmu: {alert_after} min")
    notify({
        "level": "down",
        "title": f"{site.get('name')} — awaria",
        "url": site.get("url") or site.get("public_url", "https://sklep.technica.pl"),
        "body": body,
        "sms": f"AWARIA {site.get('name')}: {problem.splitlines()[0]} "
               f"(trwa {human_duration(minutes_down)})",
    }, site, cfg, entry, dry_run)


def handle_recovery(site: dict, cfg: dict, entry: dict, detail: str, dry_run: bool) -> None:
    down_since = parse_iso(entry.get("down_since"))
    was_alerted = entry.get("alerted_down")
    entry["status"] = "up"
    entry["consecutive_failures"] = 0
    entry.pop("down_since", None)
    entry.pop("alerted_down", None)
    if not was_alerted:
        return
    minutes = (now_utc() - down_since).total_seconds() / 60 if down_since else 0
    notify({
        "level": "up",
        "title": f"{site.get('name')} — działa ponownie",
        "url": site.get("url") or site.get("public_url", "https://sklep.technica.pl"),
        "body": f"{detail}\n\nŁączny czas awarii: {human_duration(minutes)}.",
        "sms": f"OK {site.get('name')}: dziala ponownie po {human_duration(minutes)}.",
    }, site, cfg, entry, dry_run)


# =========================================================================
# Sprawdzenie zwykłej strony
# =========================================================================
def check_page(site: dict, cfg: dict, entry: dict, dry_run: bool) -> bool:
    url = site["url"]
    expected = site.get("expected_status", cfg.get("expected_status", 200))
    timeout = site.get("timeout_seconds", cfg.get("timeout_seconds", 20))
    retries = site.get("retries", cfg.get("retries", 2))
    delay = site.get("retry_delay_seconds", cfg.get("retry_delay_seconds", 5))
    ua = site.get("user_agent", cfg.get("user_agent", DEFAULT_UA))
    retry_on = site.get("retry_on_status", cfg.get("retry_on_status", [429, 500, 502, 503, 504]))

    session = build_session(ua)
    started = time.monotonic()
    resp, error = request(session, "GET", url, timeout, retries, delay, retry_on)
    elapsed = round(time.monotonic() - started, 2)

    code = resp.status_code if resp is not None else None
    entry["last_response_time"] = elapsed
    if code is not None:
        entry["last_status_code"] = code

    problem = hint = None
    if resp is None:
        problem = f"Brak odpowiedzi serwera (DNS / timeout / zerwane połączenie).\nSzczegóły: {error}"
        hint = "Sprawdź DNS, certyfikat SSL i dostępność serwera z zewnątrz."
    elif code != expected:
        problem = f"{status_line(code)}\n(oczekiwano HTTP {expected})"
        hint = describe_status(code)["hint"]
    else:
        keyword = site.get("keyword_required")
        if keyword and keyword.lower() not in resp.text.lower():
            problem = (f"Strona odpowiada poprawnie (HTTP {code}), ale brakuje kontrolnej "
                       f"frazy „{keyword}”.")
            hint = "Typowy objaw pustej strony lub przerwanego renderowania szablonu."
        else:
            missing = find_missing_elements(resp.text, site.get("required_elements"))
            if missing:
                problem = (f"Strona odpowiada poprawnie (HTTP {code}), ale brakuje na niej "
                           f"elementów: " + ", ".join(missing) + ".")
                hint = ("Sprawdź szablon strony. Jeśli element został celowo zmieniony, "
                        "popraw wzorce w sekcji required_elements w config.json.")

    if problem:
        handle_failure(site, cfg, entry, problem, hint, code, dry_run)
        return False

    print(f"[+] HTTP {code} w {elapsed}s")
    handle_recovery(site, cfg, entry,
                    f"HTTP {code} OK, czas odpowiedzi {elapsed}s.", dry_run)
    analyse_content(site, cfg, entry, resp.text, dry_run)
    return True


def analyse_content(site: dict, cfg: dict, entry: dict, html: str, dry_run: bool) -> None:
    """Porównanie pilnowanych wartości i całej treści strony."""
    key = site["_key"]
    normalized = None

    if site.get("watch_fields"):
        normalized = normalize_html(html, site)
        fields = extract_fields(normalized, site["watch_fields"])
        old_fields = entry.get("fields") or {}
        entry["fields"] = fields
        print(f"[i] Pilnowane wartości: {fields}")
        changes = [(k, old_fields[k], v) for k, v in fields.items()
                   if k in old_fields and old_fields[k] != v]
        if changes:
            lines = [f"• {k}: „{o}” → „{n}”" for k, o, n in changes]
            missing = [k for k, _, n in changes if n == "BRAK"]
            body = "Zmiana pilnowanych wartości na stronie:\n" + "\n".join(lines)
            if missing:
                body += ("\n\nUwaga: pole(a) " + ", ".join(missing) +
                         " zniknęły ze strony — może to oznaczać wycofanie produktu "
                         "albo błąd szablonu.")
            notify({"level": "field", "title": f"{site['name']} — zmiana danych",
                    "url": site["url"], "body": body,
                    "sms": f"Zmiana danych {site['name']}: " +
                           "; ".join(f"{k} {o}->{n}" for k, o, n in changes[:2])},
                   site, cfg, entry, dry_run)

    if site.get("check_content", True):
        if normalized is None:
            normalized = normalize_html(html, site)
        digest = content_hash(normalized)
        old_hash = entry.get("content_hash")
        entry["content_hash"] = digest
        if old_hash is None:
            print("[i] Pierwsze sprawdzenie treści — zapisuję punkt odniesienia.")
        elif old_hash != digest:
            print("[!] Treść strony uległa zmianie.")
            entry["content_changed_at"] = iso(now_utc())
            notify({"level": "changed", "title": f"{site['name']} — zmiana treści",
                    "url": site["url"],
                    "body": f"Wykryto zmianę zawartości strony.\n"
                            f"Hash: {old_hash[:12]}… → {digest[:12]}…",
                    "diff": make_diff(read_snapshot(key) or "", normalized)},
                   site, cfg, entry, dry_run)
        else:
            print("[=] Treść bez zmian.")
        if not dry_run:
            write_snapshot(key, normalized)


# =========================================================================
# Scenariusz zakupowy (koszyk / checkout)
# =========================================================================
def check_flow(site: dict, cfg: dict, entry: dict, dry_run: bool) -> bool:
    """
    Wykonuje kolejne kroki w jednej sesji (ciasteczka są zachowywane),
    dzięki czemu /cart i /checkout widzą koszyk z produktem — a więc także
    przyciski "Przejdź do kasy", "Zapytaj o ofertę" i "Zamów i zapłać".
    """
    timeout = site.get("timeout_seconds", cfg.get("timeout_seconds", 25))
    retries = site.get("retries", cfg.get("retries", 1))
    delay = site.get("retry_delay_seconds", cfg.get("retry_delay_seconds", 5))
    ua = site.get("user_agent", cfg.get("user_agent", DEFAULT_UA))
    retry_on = site.get("retry_on_status", cfg.get("retry_on_status", [429, 500, 502, 503, 504]))

    session = build_session(ua)
    variables: dict[str, str] = {}
    last_html = ""

    for step in site.get("steps", []):
        label = step.get("name", step.get("url", "krok"))
        url = step["url"].format(**variables) if variables else step["url"]
        method = step.get("method", "GET").upper()
        expected = step.get("expected_status", [200])
        if isinstance(expected, int):
            expected = [expected]

        payload = None
        if step.get("form"):
            payload = {k: (v.format(**variables) if isinstance(v, str) else v)
                       for k, v in step["form"].items()}

        print(f"  → {label}: {method} {url}")
        kwargs = {}
        if payload:
            kwargs["data"] = payload
        if step.get("headers"):
            kwargs["headers"] = step["headers"]

        resp, error = request(session, method, url, timeout, retries, delay, retry_on, **kwargs)

        if resp is None:
            handle_failure(site, cfg, entry,
                           f"Krok „{label}” nie doszedł do skutku — brak odpowiedzi serwera.\n"
                           f"Szczegóły: {error}",
                           "Sprawdź dostępność sklepu i certyfikat SSL.", None, dry_run)
            return False

        if resp.status_code not in expected:
            handle_failure(site, cfg, entry,
                           f"Krok „{label}” zwrócił {status_line(resp.status_code)}\n"
                           f"(oczekiwano {', '.join(str(c) for c in expected)})",
                           describe_status(resp.status_code)["hint"], resp.status_code, dry_run)
            return False

        last_html = resp.text

        # wyciągnięcie danych do kolejnych kroków (id produktu, token CSRF itp.)
        for name, pattern in (step.get("extract") or {}).items():
            match = re.search(pattern, resp.text, re.IGNORECASE | re.DOTALL)
            if not match:
                handle_failure(site, cfg, entry,
                               f"Krok „{label}”: nie udało się odczytać wartości „{name}” "
                               f"ze strony.",
                               "Prawdopodobnie zmienił się szablon sklepu — popraw wzorzec "
                               "w sekcji extract w config.json.", None, dry_run)
                return False
            variables[name] = match.group(1) if match.groups() else match.group(0)
            print(f"     odczytano {name} = {variables[name][:40]}")

        missing = find_missing_elements(resp.text, step.get("required_elements"))
        if missing:
            handle_failure(site, cfg, entry,
                           f"Krok „{label}”: strona odpowiada poprawnie (HTTP "
                           f"{resp.status_code}), ale brakuje elementów: "
                           + ", ".join(missing) + ".",
                           "To oznacza, że ścieżka zakupowa jest przerwana — klient nie może "
                           "dokończyć zamówienia, mimo że strona się otwiera.", None, dry_run)
            return False

        forbidden = step.get("forbidden_text")
        if forbidden and re.search(forbidden, resp.text, re.IGNORECASE):
            handle_failure(site, cfg, entry,
                           f"Krok „{label}”: na stronie pojawił się komunikat, którego nie "
                           f"powinno tam być (wzorzec: {forbidden}).",
                           "Sprawdź, czy produkt da się dodać do koszyka i czy koszyk nie jest "
                           "czyszczony przez błąd sesji.", None, dry_run)
            return False

        print(f"     OK (HTTP {resp.status_code})")
        time.sleep(step.get("pause_seconds", 1))

    print("[+] Cały scenariusz zakupowy przeszedł poprawnie.")
    handle_recovery(site, cfg, entry, "Ścieżka zakupowa działa na wszystkich krokach.", dry_run)

    if site.get("check_content") and last_html:
        analyse_content(site, cfg, entry, last_html, dry_run)
    return True


# =========================================================================
# Przebieg
# =========================================================================
def due_for_check(site: dict, cfg: dict, entry: dict) -> bool:
    every = site.get("check_every_minutes", cfg.get("check_every_minutes", 15))
    last = parse_iso(entry.get("last_check"))
    if last is None:
        return True
    return (now_utc() - last) >= timedelta(minutes=every) - timedelta(seconds=20)


def run_pass(cfg: dict, sites: list, state: dict, dry_run: bool, force: bool) -> None:
    for site in sites:
        key = site["_key"]
        entry = state.setdefault(key, {})
        if not force and not due_for_check(site, cfg, entry):
            continue

        print(f"\n=== {site['name']} ===")
        entry["last_check"] = iso(now_utc())
        entry["url"] = site.get("url") or site.get("public_url", "")
        try:
            if site.get("type") == "flow":
                check_flow(site, cfg, entry, dry_run)
            else:
                check_page(site, cfg, entry, dry_run)
        except Exception as exc:
            print(f"[!] Błąd podczas sprawdzania: {type(exc).__name__}: {exc}")

        if not dry_run:
            save_state(state)


def send_test_alerts(with_sms: bool, dry_run: bool) -> None:
    channels = ["teams", "discord", "telegram"] + (["sms"] if with_sms else [])
    notify({
        "level": "info",
        "title": "Test powiadomień — monitor sklep.technica.pl",
        "url": "https://sklep.technica.pl",
        "body": "Jeśli widzisz tę wiadomość, konfiguracja kanału działa poprawnie.",
        "sms": "Test monitoringu sklep.technica.pl - kanal SMS dziala.",
    }, {"notify": channels, "sms_levels": ["info"], "name": "test"}, {}, {}, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitoring sklep.technica.pl")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true",
                        help="bez wysyłki powiadomień i bez zapisu stanu")
    parser.add_argument("--only", help="sprawdź tylko wpis o podanym 'id'")
    parser.add_argument("--loop-minutes", type=float, default=0,
                        help="pracuj w pętli przez tyle minut (przebieg co --pass-seconds)")
    parser.add_argument("--pass-seconds", type=int, default=60,
                        help="odstęp między przebiegami w trybie pętli (domyślnie 60 s)")
    parser.add_argument("--test-alerts", action="store_true", help="wyślij wiadomość testową")
    parser.add_argument("--with-sms", action="store_true",
                        help="przy --test-alerts wyślij też SMS (płatny)")
    args = parser.parse_args()

    if args.test_alerts:
        send_test_alerts(args.with_sms, args.dry_run)
        return 0

    cfg = load_config(args.config)
    state = load_state()

    sites = []
    for site in cfg["sites"]:
        site["_key"] = site.get("id") or slugify(site.get("name", ""))
        if site.get("enabled", True):
            sites.append(site)
        else:
            print(f"[i] Pominięto wyłączony wpis: {site['_key']}")

    if args.only:
        sites = [s for s in sites if s["_key"] == args.only]
        if not sites:
            raise SystemExit(f"Brak aktywnego wpisu o id '{args.only}' w konfiguracji.")

    deadline = time.monotonic() + args.loop_minutes * 60
    first = True
    while True:
        print(f"\n########## Przebieg {iso(now_utc())} UTC ##########")
        run_pass(cfg, sites, state, args.dry_run, force=bool(args.only) and first)
        first = False
        if args.loop_minutes <= 0 or time.monotonic() + args.pass_seconds > deadline:
            break
        time.sleep(args.pass_seconds)

    if not args.dry_run:
        save_state(state)
    print("\n=== Koniec pracy monitora ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
