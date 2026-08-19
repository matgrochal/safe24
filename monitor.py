#!/usr/bin/env python3
"""
Monitoring sklepu sklep.technica.pl:
  * dostępność (kod odpowiedzi HTTP wraz z interpretacją 4xx/5xx),
  * zmiany treści (hash znormalizowanego HTML + diff),
  * zmiany pilnowanych wartości (cena, kod produktu, dostępność, kontakty).

Powiadomienia: Discord (webhook), Telegram (bot API), Microsoft Teams
(webhook z aplikacji Workflows / Power Automate). Kanały są opcjonalne.

Uruchomienie:
    python monitor.py                  # normalne sprawdzenie
    python monitor.py --dry-run        # bez wysyłki i bez zapisu stanu
    python monitor.py --only kontakt   # tylko wybrana strona (po 'id')
    python monitor.py --test-alerts    # wiadomość testowa na wszystkie kanały
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
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # bs4 jest opcjonalne — potrzebne dla 'selector'/'ignore_selectors'
    BeautifulSoup = None

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "state.json"
SNAPSHOT_DIR = STATE_DIR / "snapshots"

MAX_SNAPSHOT_BYTES = 512_000      # limit rozmiaru zapisywanej migawki treści
MAX_DIFF_LINES = 20               # ile linii różnic pokazać w powiadomieniu
MAX_DIFF_LINE_LEN = 160
MAX_RETRY_AFTER = 60              # nie czekamy dłużej niż minutę na 429

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
# Interpretacja kodów HTTP — treść trafia wprost do powiadomienia
# =========================================================================
STATUS_INFO: dict[int, dict[str, str]] = {
    # --- 4xx: błędy po stronie klienta / żądania ---
    400: {"name": "Bad Request",
          "desc": "Nieprawidłowe żądanie — serwer nie potrafi go zinterpretować.",
          "hint": "Sprawdź poprawność adresu URL i parametrów zapytania."},
    401: {"name": "Unauthorized",
          "desc": "Wymagane uwierzytelnienie — strona żąda logowania.",
          "hint": "Czy strona nie została przypadkiem zamknięta hasłem (np. .htpasswd)?"},
    403: {"name": "Forbidden",
          "desc": "Dostęp zabroniony — serwer rozumie żądanie, ale odmawia autoryzacji. "
                  "Zwykle problem z uprawnieniami plików albo blokada przez zaporę WAF/Cloudflare.",
          "hint": "Sprawdź reguły WAF/Cloudflare, listę blokad IP oraz uprawnienia katalogów (755/644). "
                  "Częsta przyczyna: bot monitorujący uznany za niepożądany ruch."},
    404: {"name": "Not Found",
          "desc": "Nie znaleziono strony — podany adres nie istnieje na serwerze.",
          "hint": "Czy produkt/kategoria nie została usunięta lub czy nie zmienił się jej adres URL? "
                  "Jeśli URL zmieniono celowo — zaktualizuj config.json i dodaj przekierowanie 301."},
    405: {"name": "Method Not Allowed",
          "desc": "Metoda HTTP niedozwolona dla tego zasobu.",
          "hint": "Monitor wysyła zwykłe GET — blokada metody wskazuje na regułę serwera lub WAF."},
    410: {"name": "Gone",
          "desc": "Zasób trwale usunięty przez właściciela serwisu.",
          "hint": "Jeśli to celowe — usuń adres z config.json."},
    429: {"name": "Too Many Requests",
          "desc": "Zbyt wiele żądań — serwer blokuje ruch z powodu przekroczenia limitu zapytań "
                  "(rate limiting).",
          "hint": "Zmniejsz częstotliwość sprawdzeń (cron */30) lub dodaj adres IP monitoringu "
                  "do wyjątków w zaporze / rate limiterze."},
    # --- 5xx: błędy po stronie serwera — zawsze alarm ---
    500: {"name": "Internal Server Error",
          "desc": "Wewnętrzny błąd serwera lub aplikacji — najczęściej błąd w kodzie PHP "
                  "albo brak połączenia z bazą danych.",
          "hint": "Sprawdź logi błędów PHP i logi aplikacji sklepu z ostatnich minut."},
    502: {"name": "Bad Gateway",
          "desc": "Błędna brama — serwer pośredniczący (Nginx/Cloudflare) otrzymał nieprawidłową "
                  "odpowiedź od serwera głównego.",
          "hint": "Sprawdź, czy działa PHP-FPM / backend aplikacji i czy proces nie został ubity (OOM)."},
    503: {"name": "Service Unavailable",
          "desc": "Usługa niedostępna — serwer jest przeciążony albo trwa aktualizacja/konserwacja.",
          "hint": "Sprawdź obciążenie serwera oraz czy nie włączono trybu maintenance sklepu."},
    504: {"name": "Gateway Timeout",
          "desc": "Przekroczono czas oczekiwania bramy — serwer nie zdążył odpowiedzieć w limicie czasu. "
                  "Częsty objaw problemu z bazą danych lub długo działającego zapytania.",
          "hint": "Sprawdź czas wykonania zapytań SQL i obciążenie bazy danych."},
}

CLASS_INFO = {
    4: {"name": "Błąd żądania (4xx)",
        "desc": "Serwer odrzucił żądanie po stronie klienta.",
        "hint": "Sprawdź adres URL oraz reguły blokad na serwerze."},
    5: {"name": "Błąd serwera (5xx)",
        "desc": "Serwer nie był w stanie obsłużyć poprawnego żądania.",
        "hint": "Sprawdź logi serwera i aplikacji."},
}


def describe_status(code: int) -> dict[str, str]:
    """Zwraca opis kodu HTTP (dokładny albo klasowy 4xx/5xx)."""
    if code in STATUS_INFO:
        return STATUS_INFO[code]
    fallback = CLASS_INFO.get(code // 100)
    if fallback:
        return fallback
    return {"name": "Nieoczekiwany kod odpowiedzi", "desc": "", "hint": ""}


def status_line(code: int) -> str:
    info = describe_status(code)
    return f"HTTP {code} {info['name']} — {info['desc']}".strip(" —")


# =========================================================================
# Konfiguracja i stan
# =========================================================================
def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("sites"):
        raise SystemExit("Konfiguracja nie zawiera żadnych stron w polu 'sites'.")
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


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-").lower()
    return slug or "site"


def snapshot_path(site_key: str) -> Path:
    return SNAPSHOT_DIR / f"{site_key}.txt"


def read_snapshot(site_key: str) -> str | None:
    p = snapshot_path(site_key)
    return p.read_text(encoding="utf-8") if p.exists() else None


def write_snapshot(site_key: str, text: str) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")[:MAX_SNAPSHOT_BYTES].decode("utf-8", "ignore")
    snapshot_path(site_key).write_text(data, encoding="utf-8")


# =========================================================================
# Pobieranie
# =========================================================================
def fetch(url: str, timeout: int, retries: int, delay: int,
          user_agent: str, retry_on: list[int]):
    """Zwraca (response, error_text). Ponawia próbę przy błędzie sieci i kodach z retry_on."""
    last_error = None
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl,en;q=0.8",
    }
    for attempt in range(1, retries + 2):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)

            # sklep.technica.pl bywa serwowany bez charset w nagłówku — wymuszamy
            # poprawne kodowanie, inaczej polskie znaki rozjadą się przy porównaniu
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
# Normalizacja treści
# =========================================================================
def cut_between(text: str, start: str | None, end: str | None,
                occurrence: str = "first") -> str:
    """
    Obcina tekst do fragmentu między znacznikami. W sklepie AtomStore każda
    podstrona zawiera to samo ogromne menu kategorii (kilka tysięcy linków)
    i tę samą stopkę — bez tego cięcia hash zmieniałby się przy każdej
    modyfikacji dowolnej kategorii w całym sklepie.

    occurrence='first' (domyślnie) tnie na pierwszym wystąpieniu znacznika —
    bezpieczniejsze, gdy ten sam tekst może wystąpić też w treści strony.
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
    """Sprowadza HTML do postaci porównywalnej między uruchomieniami."""
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
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=0,
        fromfile="poprzednia", tofile="aktualna",
    )
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
    """
    Wyciąga pilnowane wartości (cena, kod produktu, dostępność…) za pomocą
    wyrażeń regularnych. Grupa 1 = wartość; brak dopasowania = 'BRAK'.
    """
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


# =========================================================================
# Powiadomienia
# =========================================================================
def _post(url: str, payload: dict, channel: str) -> None:
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code >= 300:
            print(f"[!] {channel}: HTTP {r.status_code} — {r.text[:300]}")
        else:
            print(f"[+] {channel}: wysłano powiadomienie.")
    except requests.RequestException as exc:
        print(f"[!] {channel}: błąd wysyłki — {exc}")


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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    _post(url, payload, "Discord")


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
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    _post(f"https://api.telegram.org/bot{token}/sendMessage", payload, "Telegram")


def notify_teams(event: dict) -> None:
    """
    Microsoft Teams przez webhook Power Automate Workflows (Adaptive Card).
    Stare webhooki Office 365 Connectors (webhook.office.com) Microsoft wyłączył
    w maju 2026 — trzeba użyć aplikacji "Workflows" w Teams.
    TEAMS_PAYLOAD=messagecard przełącza na starszy format MessageCard.
    """
    url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not url:
        return
    meta = LEVELS[event["level"]]
    style = os.environ.get("TEAMS_PAYLOAD", "adaptive").lower()

    if style == "messagecard":
        sections = [{"text": event["body"].replace("\n", "\n\n")}]
        if event.get("diff"):
            sections.append({"text": f"<pre>{event['diff']}</pre>"})
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": f"{meta['color']:06X}",
            "summary": event["title"],
            "title": f"{meta['icon']} {event['title']}",
            "sections": sections,
            "potentialAction": [{
                "@type": "OpenUri",
                "name": "Otwórz stronę",
                "targets": [{"os": "default", "uri": event["url"]}],
            }],
        }
        _post(url, payload, "Teams (MessageCard)")
        return

    body = [
        {"type": "TextBlock", "text": f"{meta['icon']} {event['title']}",
         "weight": "Bolder", "size": "Medium", "wrap": True, "color": meta["teams"]},
        {"type": "TextBlock", "text": event["body"], "wrap": True, "spacing": "Small"},
    ]
    if event.get("diff"):
        body.append({"type": "TextBlock", "text": event["diff"], "wrap": True,
                     "fontType": "Monospace", "size": "Small", "spacing": "Small"})

    actions = [{"type": "Action.OpenUrl", "title": "Otwórz stronę", "url": event["url"]}]
    if RUN_URL:
        actions.append({"type": "Action.OpenUrl", "title": "Log GitHub Actions", "url": RUN_URL})

    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
                "actions": actions,
            },
        }],
    }
    _post(url, payload, "Teams (Adaptive Card)")


def notify(event: dict, dry_run: bool = False) -> None:
    meta = LEVELS[event["level"]]
    print(f"\n{meta['icon']} [{meta['label']}] {event['title']}\n{event['body']}")
    if event.get("diff"):
        print(event["diff"])
    if dry_run:
        print("[dry-run] powiadomienia nie zostały wysłane.")
        return
    notify_discord(event)
    notify_telegram(event)
    notify_teams(event)


# =========================================================================
# Logika sprawdzania
# =========================================================================
def is_immediate(code: int | None, rules: list) -> bool:
    """Czy dany kod ma alarmować natychmiast, z pominięciem progu powtórzeń."""
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


def check_site(site: dict, cfg: dict, state: dict, dry_run: bool) -> bool:
    name = site.get("name") or site["url"]
    url = site["url"]
    key = site.get("id") or slugify(name)
    prev = state.get(key, {})

    expected = site.get("expected_status", cfg.get("expected_status", 200))
    timeout = site.get("timeout_seconds", cfg.get("timeout_seconds", 20))
    retries = site.get("retries", cfg.get("retries", 2))
    delay = site.get("retry_delay_seconds", cfg.get("retry_delay_seconds", 5))
    ua = site.get("user_agent", cfg.get("user_agent", DEFAULT_UA))
    threshold = site.get("failures_before_alert", cfg.get("failures_before_alert", 1))
    retry_on = site.get("retry_on_status", cfg.get("retry_on_status", [429, 500, 502, 503, 504]))
    immediate = site.get("immediate_alert_codes", cfg.get("immediate_alert_codes", ["5xx"]))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"\n=== {name} ({url}) ===")

    started = time.monotonic()
    resp, error = fetch(url, timeout, retries, delay, ua, retry_on)
    elapsed = round(time.monotonic() - started, 2)

    code = resp.status_code if resp is not None else None
    problem = None
    hint = ""

    if resp is None:
        problem = f"Brak odpowiedzi serwera (DNS / timeout / zerwane połączenie).\nSzczegóły: {error}"
        hint = "Sprawdź DNS, certyfikat SSL i dostępność serwera z zewnątrz."
    elif code != expected:
        info = describe_status(code)
        problem = f"{status_line(code)}\n(oczekiwano HTTP {expected})"
        hint = info["hint"]
    else:
        keyword = site.get("keyword_required")
        if keyword and keyword.lower() not in resp.text.lower():
            problem = (f"Strona odpowiada poprawnie (HTTP {code}), ale w treści brakuje "
                       f"kontrolnej frazy „{keyword}”.")
            hint = ("Typowy objaw pustej strony, przerwanego renderowania szablonu "
                    "albo podmiany treści mimo poprawnego kodu odpowiedzi.")

    new_state = dict(prev)
    new_state.update({"url": url, "last_check": now, "last_response_time": elapsed})
    if code is not None:
        new_state["last_status_code"] = code

    # --- awaria ---
    if problem:
        fails = prev.get("consecutive_failures", 0) + 1
        new_state["consecutive_failures"] = fails
        new_state["status"] = "down"
        print(f"[-] {problem} (nieudanych sprawdzeń z rzędu: {fails})")

        alert_now = fails >= threshold or is_immediate(code, immediate)
        if alert_now and prev.get("status") != "down":
            body = problem
            if hint:
                body += f"\n\nCo sprawdzić: {hint}"
            body += (f"\n\nCzas odpowiedzi: {elapsed}s\nSprawdzono: {now} UTC\n"
                     f"Nieudane próby z rzędu: {fails}")
            notify({"level": "down", "title": f"{name} — awaria", "url": url, "body": body}, dry_run)
        state[key] = new_state
        return False

    # --- działa ---
    new_state["consecutive_failures"] = 0
    new_state["status"] = "up"
    print(f"[+] HTTP {code} w {elapsed}s")

    if prev.get("status") == "down":
        notify({
            "level": "up",
            "title": f"{name} — działa ponownie",
            "url": url,
            "body": (f"HTTP {code} OK, czas odpowiedzi {elapsed}s.\n"
                     f"Poprzedni błąd: HTTP {prev.get('last_status_code', '—')}.\n"
                     f"Sprawdzono: {now} UTC"),
        }, dry_run)

    normalized = None

    # --- pilnowane wartości (cena, dostępność, dane kontaktowe) ---
    if site.get("watch_fields"):
        normalized = normalize_html(resp.text, site)
        fields = extract_fields(normalized, site["watch_fields"])
        old_fields = prev.get("fields") or {}
        new_state["fields"] = fields
        print(f"[i] Pilnowane wartości: {fields}")

        changes = [(k, old_fields[k], v) for k, v in fields.items()
                   if k in old_fields and old_fields[k] != v]
        if changes:
            lines = [f"• {k}: „{old}” → „{new}”" for k, old, new in changes]
            missing = [k for k, _, new in changes if new == "BRAK"]
            body = "Zmiana pilnowanych wartości na stronie:\n" + "\n".join(lines)
            if missing:
                body += ("\n\nUwaga: pole(a) " + ", ".join(missing) +
                         " zniknęły ze strony — może to oznaczać wycofanie produktu "
                         "albo błąd szablonu.")
            body += f"\n\nSprawdzono: {now} UTC"
            notify({"level": "field", "title": f"{name} — zmiana danych", "url": url,
                    "body": body}, dry_run)

    # --- porównanie całej treści ---
    if site.get("check_content", True):
        if normalized is None:
            normalized = normalize_html(resp.text, site)
        digest = content_hash(normalized)
        old_hash = prev.get("content_hash")
        new_state["content_hash"] = digest

        if old_hash is None:
            print("[i] Pierwsze sprawdzenie treści — zapisuję punkt odniesienia.")
            new_state["content_changed_at"] = now
        elif old_hash != digest:
            print("[!] Treść strony uległa zmianie.")
            new_state["content_changed_at"] = now
            old_text = read_snapshot(key) or ""
            notify({
                "level": "changed",
                "title": f"{name} — zmiana treści",
                "url": url,
                "body": (f"Wykryto zmianę zawartości strony.\nSprawdzono: {now} UTC\n"
                         f"Hash: {old_hash[:12]}… → {digest[:12]}…"),
                "diff": make_diff(old_text, normalized),
            }, dry_run)
        else:
            print("[=] Treść bez zmian.")

        if not dry_run:
            write_snapshot(key, normalized)

    state[key] = new_state
    return True


def send_test_alerts(dry_run: bool) -> None:
    notify({
        "level": "info",
        "title": "Test powiadomień — monitor sklep.technica.pl",
        "url": "https://sklep.technica.pl",
        "body": ("Jeśli widzisz tę wiadomość, konfiguracja kanału działa poprawnie.\n"
                 "Monitorowane adresy: strona główna, /kontakt, karta produktu 450021, "
                 "kategoria /szafy-chlodnicze."),
    }, dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitoring dostępności i zmian stron sklep.technica.pl")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true", help="bez wysyłki powiadomień i zapisu stanu")
    parser.add_argument("--only", help="sprawdź tylko stronę o podanym 'id' z config.json")
    parser.add_argument("--test-alerts", action="store_true", help="wyślij wiadomość testową i zakończ")
    args = parser.parse_args()

    if args.test_alerts:
        send_test_alerts(args.dry_run)
        return 0

    cfg = load_config(args.config)
    state = load_state()

    sites = cfg["sites"]
    if args.only:
        sites = [s for s in sites if (s.get("id") or slugify(s.get("name", ""))) == args.only]
        if not sites:
            raise SystemExit(f"Brak strony o id '{args.only}' w konfiguracji.")

    all_ok = True
    for site in sites:
        try:
            if not check_site(site, cfg, state, args.dry_run):
                all_ok = False
        except Exception as exc:  # pojedyncza strona nie może wywrócić całego biegu
            all_ok = False
            print(f"[!] Błąd podczas sprawdzania {site.get('url')}: {type(exc).__name__}: {exc}")

    if not args.dry_run:
        save_state(state)

    print("\n=== Podsumowanie:", "wszystko OK ===" if all_ok else "wykryto problemy ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
