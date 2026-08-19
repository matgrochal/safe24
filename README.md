# Monitoring sklep.technica.pl — dostępność, kody HTTP i zmiany treści

Darmowy monitoring oparty o Pythona i GitHub Actions. Co 15 minut sprawdza cztery
adresy sklepu: czy odpowiadają kodem HTTP 200, czy nie zwracają błędu 4xx/5xx
i czy nie zmieniła się ich zawartość (cena, dostępność, dane kontaktowe, lista produktów).
O każdym zdarzeniu informuje na **Discordzie**, **Telegramie** i **Microsoft Teams**.

## Monitorowane strony

| id w config.json | Adres | Co jest sprawdzane |
|---|---|---|
| `strona-glowna` | https://sklep.technica.pl | HTTP 200, obecność stopki z newsletterem, zmiany treści strony głównej (bannery, promocje) |
| `kontakt` | https://sklep.technica.pl/kontakt | HTTP 200, obecność `sklep@technica.pl`, zmiany treści oraz pilnowane wartości: e-mail sklepu, e-mail reklamacji, numer infolinii, godziny pracy, adres |
| `produkt-450021` | https://sklep.technica.pl/barowa-witryna-chlodnicza-do-butelek-2-drzwiowa-drzwi-przesuwane-210-l-920x515x855-mm-technica-cold-line | HTTP 200, obecność frazy „Kod produktu”, zmiany treści oraz pilnowane wartości: kod produktu (450021), cena brutto, termin wysyłki, obecność przycisku „Do koszyka” |
| `kategoria-szafy-chlodnicze` | https://sklep.technica.pl/szafy-chlodnicze | HTTP 200, obecność nagłówka „Szafy chłodnicze”, zmiany listingu — nowe/usunięte produkty, zmiany cen na liście |

Cztery adresy × 96 uruchomień dziennie ≈ 384 zapytania na dobę do sklepu — ruch pomijalny,
ale wart uwagi przy konfiguracji rate limitera (patrz kod 429 niżej).

## Monitorowane kody odpowiedzi HTTP

Każdy kod inny niż oczekiwany (`expected_status`, domyślnie 200) wyzwala alarm.
Powiadomienie zawiera nie tylko numer kodu, lecz także jego znaczenie i podpowiedź, co sprawdzić.

### Błędy po stronie klienta / żądania (4xx)

| Kod | Znaczenie | Zachowanie monitora |
|---|---|---|
| **403 Forbidden** | Dostęp zabroniony — serwer rozumie żądanie, ale odmawia autoryzacji. Problem z uprawnieniami albo blokada przez zaporę WAF | Alarm. W podpowiedzi: sprawdź reguły WAF/Cloudflare, blokady IP i uprawnienia katalogów. **Najczęstsza przyczyna fałszywego alarmu: WAF uznaje bota monitorującego za niepożądany ruch** |
| **404 Not Found** | Nie znaleziono strony — podany adres nie istnieje na serwerze | Alarm. Dla karty produktu i kategorii oznacza zwykle usunięcie lub zmianę adresu URL |
| **429 Too Many Requests** | Zbyt wiele żądań — blokada z powodu przekroczenia limitu zapytań (rate limiting) | Ponowienie z respektowaniem nagłówka `Retry-After` (maks. 60 s), a jeśli dalej trwa — alarm |

Monitor rozpoznaje też 400, 401, 405 i 410, a każdy inny kod 4xx opisuje ogólnie jako błąd żądania.

### Błędy po stronie serwera (5xx — zawsze wyzwalają alarm)

| Kod | Znaczenie | Zachowanie monitora |
|---|---|---|
| **500 Internal Server Error** | Wewnętrzny błąd serwera lub aplikacji, np. błąd w skryptach PHP | 2 ponowienia, potem natychmiastowy alarm. Podpowiedź: sprawdź logi PHP |
| **502 Bad Gateway** | Błędna brama — serwer pośredniczący (Nginx/Cloudflare) otrzymał nieprawidłową odpowiedź od serwera głównego | j.w. Podpowiedź: sprawdź PHP-FPM / backend, czy proces nie został ubity (OOM) |
| **503 Service Unavailable** | Usługa niedostępna — serwer przeciążony albo trwa aktualizacja/konserwacja | j.w. Podpowiedź: sprawdź obciążenie i tryb maintenance |
| **504 Gateway Timeout** | Przekroczono czas oczekiwania bramy — serwer nie odpowiedział w limicie czasu, często problem z bazą danych | j.w. Podpowiedź: sprawdź czas zapytań SQL |

Kody 5xx są w `config.json` wpisane na listę `immediate_alert_codes: ["5xx"]` — alarmują
**natychmiast**, nawet gdy podniesiesz `failures_before_alert`. Błędy 4xx podlegają zwykłemu
progowi powtórzeń, bo częściej wynikają z celowej zmiany po stronie sklepu.

Poza kodami HTTP alarm wyzwala także brak odpowiedzi serwera (błąd DNS, timeout,
zerwane połączenie, wygasły certyfikat SSL) oraz brak frazy kontrolnej mimo kodu 200 —
to typowy objaw pustej strony lub przerwanego renderowania szablonu.

## Struktura plików

```
website-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml        # harmonogram co 15 min + zapis stanu do repo
├── state/
│   ├── state.json             # hash treści, status, pilnowane wartości (tworzone automatycznie)
│   └── snapshots/             # migawki treści czterech stron — z nich powstaje diff
├── config.json                # cztery monitorowane adresy sklepu
├── monitor.py                 # cały skrypt monitorujący
├── requirements.txt
└── README.md
```

Katalog `state/` jest commitowany z powrotem do repozytorium przez workflow — to jest
„pamięć” monitora między uruchomieniami. Żadna baza danych nie jest potrzebna.

---

## Krok 1. Utwórz repozytorium

1. GitHub → **New repository** → nazwa np. `technica-monitor`.
2. Wgraj pliki z tego pakietu — przez WWW (*Add file → Upload files*) albo z konsoli:

```bash
git init
git add .
git commit -m "feat: monitoring sklep.technica.pl"
git branch -M main
git remote add origin https://github.com/UZYTKOWNIK/technica-monitor.git
git push -u origin main
```

> Przy wgrywaniu przez przeglądarkę GitHub pomija puste katalogi — pliki
> `state/.gitkeep` i `state/snapshots/.gitkeep` są w pakiecie właśnie po to,
> żeby katalogi się utworzyły.

## Krok 2. Skonfiguruj powiadomienia

Wystarczy jeden kanał, ale możesz włączyć wszystkie trzy naraz. Każdy jest opcjonalny —
skrypt pomija kanał, dla którego nie ustawiono sekretu.

### Discord
1. Serwer → *Ustawienia kanału* → **Integracje → Webhooki → Nowy webhook**.
2. Skopiuj URL → sekret `DISCORD_WEBHOOK_URL`.

### Telegram
1. Napisz do **@BotFather** → `/newbot` → otrzymasz token → sekret `TELEGRAM_BOT_TOKEN`.
2. Napisz cokolwiek do swojego bota (bot nie może zacząć rozmowy pierwszy).
3. Otwórz `https://api.telegram.org/bot<TOKEN>/getUpdates` i odczytaj `chat.id`
   → sekret `TELEGRAM_CHAT_ID`. Dla grupy: dodaj bota do grupy i użyj jej ujemnego ID.

### Microsoft Teams
Klasyczne webhooki *Office 365 Connectors* (adresy `webhook.office.com`) Microsoft
wyłączył ostatecznie w maju 2026 — działa wyłącznie aplikacja **Workflows** (Power Automate):

1. W Teams otwórz kanał → **⋯ → Workflows** (albo aplikacja *Workflows* z lewego paska).
2. Wybierz szablon **„Post to a channel when a webhook request is received”**.
3. Zaloguj się, wskaż zespół i kanał → **Utwórz / Add workflow**.
4. Skopiuj wygenerowany adres URL (domena `logic.azure.com` / `powerautomate.com`)
   → sekret `TEAMS_WEBHOOK_URL`.

Skrypt wysyła Adaptive Card (kolor karty zależy od typu zdarzenia). Jeśli Twój flow
oczekuje starszego formatu, ustaw w `monitor.yml` `TEAMS_PAYLOAD: messagecard` —
Workflows obsługuje oba, ale MessageCard nie renderuje przycisków.

### Dodanie sekretów w GitHub
Repozytorium → **Settings → Secrets and variables → Actions → New repository secret**:

| Nazwa sekretu | Kanał |
|---|---|
| `DISCORD_WEBHOOK_URL` | Discord |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |
| `TEAMS_WEBHOOK_URL` | Teams |

## Krok 3. Nadaj workflow prawo zapisu

**Settings → Actions → General → Workflow permissions** → **Read and write permissions** → *Save*.
Bez tego krok zapisujący `state/` zwróci błąd 403 i monitor nie zapamięta hashy —
przy każdym uruchomieniu zgłaszałby zmianę treści od nowa.

## Krok 4. Uruchom i przetestuj

1. Zakładka **Actions** → *Website monitor* → **Run workflow**. Pierwsze uruchomienie
   tylko zapisuje punkt odniesienia dla czterech stron — brak powiadomień jest wtedy poprawny.
2. Test samych powiadomień (lokalnie):

```bash
pip install -r requirements.txt
export TEAMS_WEBHOOK_URL="https://..."      # PowerShell: $env:TEAMS_WEBHOOK_URL="..."
python monitor.py --test-alerts
```

3. Test pojedynczej strony bez skutków ubocznych:

```bash
python monitor.py --only produkt-450021 --dry-run
python monitor.py --only kontakt --dry-run
```

W trybie `--dry-run` skrypt wypisze wykryte wartości, np.
`{'kod_produktu': '450021', 'cena_brutto': '1 915,00', 'dostepnosc': 'Wysyłka w ciągu 2 dni roboczych!', 'przycisk_koszyka': 'Do koszyka'}`.
To najszybszy sposób, by sprawdzić, czy wzorce w `watch_fields` nadal pasują do szablonu sklepu.

---

## Jak działa porównywanie treści w tym sklepie

Sklep działa na AtomStore, gdzie **każda podstrona zawiera to samo rozbudowane menu
kategorii (kilka tysięcy linków) i tę samą stopkę**. Hashowanie całego HTML byłoby
bezużyteczne — alarm przychodziłby przy każdej zmianie dowolnej kategorii w sklepie.
Dlatego w `config.json` każda strona ma:

```json
"content_between": { "start": "Wyprzedaż", "end": "Zapisz się do newslettera" }
```

Porównywana jest wyłącznie treść **między ostatnią pozycją menu a stopką**, czyli
faktyczna zawartość strony. Dodatkowo skrypt usuwa `<script>`, `<style>`, komentarze,
tokeny CSRF, `nonce`, cache-bustery i znaczniki czasu, a wzorzec
`\d+ szt\. - [\d\s,]+ zł` wycina stan koszyka z nagłówka.

Drugi mechanizm to `watch_fields` — nazwane wyrażenia regularne wyciągające konkretne
wartości. Zmiana ceny przychodzi wtedy jako czytelny komunikat
`cena_brutto: „1 915,00” → „1 799,00”`, a zniknięcie przycisku „Do koszyka” jako
`przycisk_koszyka: „Do koszyka” → „BRAK”` z adnotacją o możliwym wycofaniu produktu.

### Poziomy powiadomień

| Ikona | Zdarzenie |
|---|---|
| 🔴 | awaria — błędny kod HTTP, brak odpowiedzi, brak frazy kontrolnej |
| 🟢 | powrót do działania (wysyłane raz, po ustaniu awarii) |
| 🟡 | zmiana treści strony — z fragmentem diffa |
| 🟠 | zmiana pilnowanej wartości — cena, dostępność, dane kontaktowe |

Alarmy wysyłane są **przy zmianie stanu**, nie w kółko: trwająca awaria nie generuje
powiadomienia co 15 minut.

## Co warto wiedzieć

- **Cron w GitHub Actions bywa opóźniony.** Na darmowym planie zadanie `*/15` potrafi
  wystartować kilka–kilkanaście minut później przy dużym obciążeniu platformy, a przy
  bardzo dużym pojedynczy bieg może zostać pominięty. Do wykrycia awarii w kilkanaście
  minut to wystarcza; do SLA liczonego w sekundach — nie.
- **Zużycie limitu.** Repo prywatne: ~96 biegów dziennie × ~1 min ≈ 2900 min/mies.,
  a darmowy limit to 2000 min/mies. Ustaw repo jako **publiczne** (biegi darmowe bez limitu)
  albo zmień cron na `*/30`. Uwaga: w repo publicznym widoczne są migawki treści w `state/snapshots/`.
- **Uśpienie harmonogramu.** GitHub wyłącza cron w repo bez aktywności przez 60 dni —
  tutaj problem nie występuje, bo workflow sam commituje zmiany w `state/`.
- **Kod 403 z WAF.** Jeśli monitor zacznie dostawać 403 mimo działającego sklepu,
  zapora uznała bota za niepożądany ruch. Rozwiązania: dodać `User-Agent` monitora
  (pole `user_agent` w `config.json`) do wyjątków albo odblokować zakresy IP GitHub Actions.
- **Kod 429.** Przy czterech stronach co 15 minut limit zapytań nie powinien reagować;
  jeśli jednak zacznie — zmniejsz częstotliwość crona lub dodaj wyjątek w rate limiterze.
- **Zmiana szablonu sklepu** unieważni wzorce `watch_fields` — wartości zmienią się na `BRAK`
  i dostaniesz o tym powiadomienie. Wtedy wystarczy poprawić wyrażenia w `config.json`
  i sprawdzić je poleceniem `--only <id> --dry-run`.
- **Listing kategorii** potrafi zmieniać kolejność produktów (sortowanie, rotacja promocji).
  Jeśli alarmy z `kategoria-szafy-chlodnicze` okażą się zbyt częste, ustaw dla tej strony
  `"check_content": false` i dodaj `watch_fields` z licznikiem produktów.
