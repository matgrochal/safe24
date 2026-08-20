# Monitoring sklep.technica.pl

Monitoring dostępności, ścieżki zakupowej i zmian treści sklepu, oparty o Pythona
i GitHub Actions. Powiadomienia trafiają na **Microsoft Teams**, **Discord**,
**Telegram** i **SMS** (SMSAPI.pl).

Dokumentacja wdrożeniowa krok po kroku dla osoby nietechnicznej: [INSTRUKCJA.md](INSTRUKCJA.md).

---

## Spis treści

- [Co jest monitorowane](#co-jest-monitorowane)
- [Harmonogram i progi alarmów](#harmonogram-i-progi-alarmów)
- [Kody odpowiedzi HTTP](#kody-odpowiedzi-http)
- [Ścieżka zakupowa](#ścieżka-zakupowa)
- [Powiadomienia](#powiadomienia)
- [Struktura plików](#struktura-plików)
- [Konfiguracja](#konfiguracja)
- [Polecenia](#polecenia)
- [Rozwiązywanie problemów](#rozwiązywanie-problemów)
- [Znane ograniczenia](#znane-ograniczenia)

---

## Co jest monitorowane

| id | Zakres | Sprawdzane co | Alarm po | Kanały |
|---|---|---|---|---|
| `strona-glowna` | https://sklep.technica.pl | 1 min | **5 min** | Teams, Discord, Telegram, **SMS** |
| `koszyk-pusty` | https://sklep.technica.pl/cart | 1 min | 10 min | Teams, Discord, Telegram, **SMS** |
| `checkout-dostepnosc` | https://sklep.technica.pl/checkout | 1 min | 10 min | Teams, Discord, Telegram, **SMS** |
| `sciezka-zakupowa` | 5-krokowy test API zakupów | 22 min | 10 min | Teams, Discord, Telegram, **SMS** |
| `kontakt` | https://sklep.technica.pl/kontakt | 15 min | 15 min | Teams, Discord, Telegram |
| `produkt-450021` | karta barowej witryny chłodniczej | 15 min | 15 min | Teams, Discord, Telegram |
| `kategoria-szafy-chlodnicze` | https://sklep.technica.pl/szafy-chlodnicze | 15 min | 15 min | Teams, Discord, Telegram |

### Pilnowane wartości (`watch_fields`)

Poza samą dostępnością monitor śledzi konkretne dane i zgłasza ich zmianę
w czytelnej formie `cena_brutto: „1 915,00" → „1 799,00"`.

**Strona Kontakt:** `sklep@technica.pl`, `reklamacje@technica.pl`, numer infolinii,
godziny pracy (poniedziałek–piątek), adres siedziby.

**Karta produktu 450021:** kod produktu, cena brutto, termin wysyłki, obecność
przycisku „Do koszyka".

Zniknięcie pilnowanej wartości raportowane jest jako `„Do koszyka" → „BRAK"`
wraz z adnotacją o możliwym wycofaniu produktu.

---

## Harmonogram i progi alarmów

Dwa niezależne ustawienia, **osobne dla każdego wpisu**:

```json
"check_every_minutes": 1,     ← jak często sprawdzać
"alert_after_minutes": 5      ← ile musi trwać awaria, zanim przyjdzie alarm
```

Monitor zapamiętuje moment pierwszego niepowodzenia (`down_since`) i alarmuje dopiero
wtedy, gdy awaria trwa nieprzerwanie dłużej niż `alert_after_minutes`. Chwilowy błąd,
który sam mija w minutę, nie budzi nikogo w nocy.

Alarm o awarii przychodzi **raz**. Drugie powiadomienie to dopiero informacja o powrocie
do działania, z podanym łącznym czasem przestoju.

### Jak uzyskano rozdzielczość 1 minuty

GitHub Actions nie pozwala planować zadań częściej niż co 5 minut, a harmonogram działa
w trybie „best effort" — zadania planowane co 10 minut potrafią startować co 25–35 minut.

Zastosowane rozwiązanie:

- **cron na nierównych minutach** (`3,13,23,33,43,53`). Najwięcej zadań na GitHubie
  startuje o pełnych dziesiątkach, więc kolejka jest wtedy najdłuższa.
- **pętla wewnątrz uruchomienia** — skrypt pracuje 25 minut, wykonując przebieg co 60 s
  (`--loop-minutes 25 --pass-seconds 60`). W każdym przebiegu sprawdzane są tylko te wpisy,
  którym minął ich własny `check_every_minutes`.
- **pętla dłuższa niż odstęp crona** — kolejne uruchomienia czekają w kolejce
  (`concurrency`) i startują natychmiast po zakończeniu poprzedniego. Dzięki temu nawet
  przy 30-minutowym opóźnieniu GitHuba monitoring działa niemal bez przerw.

---

## Kody odpowiedzi HTTP

Każdy kod inny niż oczekiwany uruchamia procedurę awarii. Powiadomienie zawiera znaczenie
kodu i podpowiedź, co sprawdzić.

### Błędy po stronie klienta (4xx)

| Kod | Znaczenie | Podpowiedź w alarmie |
|---|---|---|
| **403 Forbidden** | Dostęp zabroniony — problem z uprawnieniami albo blokada przez WAF | Reguły WAF/Cloudflare, blokady IP, uprawnienia katalogów (755/644) |
| **404 Not Found** | Adres nie istnieje na serwerze | Czy produkt/kategoria nie została usunięta lub czy nie zmienił się URL |
| **429 Too Many Requests** | Przekroczony limit zapytań (rate limiting) | Ponowienie z respektowaniem `Retry-After` (maks. 60 s), potem alarm |

Rozpoznawane są też 400, 401, 405 i 410; pozostałe kody 4xx opisywane są ogólnie.

### Błędy po stronie serwera (5xx)

| Kod | Znaczenie | Podpowiedź w alarmie |
|---|---|---|
| **500 Internal Server Error** | Błąd aplikacji, najczęściej PHP lub baza danych | Sprawdź logi błędów PHP z ostatnich minut |
| **502 Bad Gateway** | Nginx/Cloudflare dostał złą odpowiedź od backendu | Sprawdź PHP-FPM, czy proces nie został ubity (OOM) |
| **503 Service Unavailable** | Przeciążenie lub tryb konserwacji | Sprawdź obciążenie i tryb maintenance |
| **504 Gateway Timeout** | Serwer nie odpowiedział w limicie czasu | Sprawdź czas wykonania zapytań SQL |

Kody 429 i 5xx są automatycznie ponawiane (`retry_on_status`) przed uznaniem za awarię.

Wszystkie kody podlegają progowi `alert_after_minutes`. Jeśli chcesz, by wybrane
alarmowały **natychmiast**, z pominięciem progu, dopisz je w `config.json`:

```json
"immediate_alert_codes": ["5xx"]
```

Alarm wyzwala też brak odpowiedzi serwera (DNS, timeout, wygasły certyfikat SSL) oraz
brak frazy kontrolnej lub wymaganego elementu mimo kodu 200.

---

## Ścieżka zakupowa

### Dlaczego nie da się tego sprawdzić zwykłym GET

Sklep działa jako **aplikacja PWA**. Podstrony `/cart` i `/checkout` nie zawierają treści
w kodzie HTML — buduje ją JavaScript, pobierając dane z wewnętrznego API pod adresami
`/pwaapi/...`. Szukanie napisu „Przejdź do kasy" w kodzie strony nigdy nie zadziała.

Monitor sprawdza więc **to samo API, z którego korzystają przyciski**. Jest to nawet
lepsze: gdy API koszyka przestanie działać, przyciski w przeglądarce też przestaną,
a monitor wykryje to bezpośrednio u źródła.

### Pięć kroków scenariusza

| # | Żądanie | Warunek zaliczenia |
|---|---|---|
| 1 | `GET` karta produktu | HTTP 200, pobranie ciasteczka sesji `TECHNICA_SID` |
| 2 | `PATCH /pwaapi/cart/me` | HTTP 200/201 + `"code": 200` w odpowiedzi |
| 3 | `GET /pwaapi/cart/me` | koszyk zawiera sku `127782` i niezerową ilość |
| 4 | `POST /pwaapi/cart/shipping-methods` | niepusta lista `shippment_methods`, obecna przesyłka kurierska, min. jedna metoda z `"disabled": false` |
| 5 | `GET /pwaapi/cart/payment-methods` | niepusta lista `payment_methods`, obecne płatność za pobraniem i internetowa, min. jedna aktywna |

Etapy widoczne dla klienta, które **nie wymagają odwzorowania**: popup „Przejdź do koszyka
/ Kontynuuj zakupy", popup „Kup jako zalogowany / bez logowania" oraz formularz danych
do faktury. To czysty frontend — nie wysyłają żądań do serwera.

### Czego scenariusz celowo nie robi

**Nie klika „Zamów i zapłać".** Ten przycisk składa prawdziwe zamówienie — z fakturą,
powiadomieniem dla obsługi i wpisem w systemie. Zamiast tego sprawdzane jest wszystko,
od czego ten przycisk zależy: koszyk ma produkty, są dostępne metody dostawy i płatności.
Jeśli którakolwiek z tych rzeczy padnie, przycisk i tak nie zadziała — a monitor to
wychwyci, nie kupując niczego.

### Ważne rozróżnienie: `sku` ≠ „Kod produktu"

Wartość widoczna na stronie jako **Kod produktu** (`450021`) to oznaczenie handlowe.
API koszyka używa innego, wewnętrznego identyfikatora — dla tego samego produktu jest to
`127782`. Wpisanie kodu produktu do ciała żądania PATCH sprawi, że dodanie do koszyka
się nie powiedzie.

Właściwe `sku` odczytasz zawsze tak samo: przy otwartym DevTools kliknij „Do koszyka"
i sprawdź pole `sku` w zakładce **Payload** żądania PATCH.

### Komunikaty o błędach

Każdy krok ma własny opis skutku (`impact`) i podpowiedź diagnostyczną (`hint`).
Przykład rzeczywistego alarmu:

> 🔴 **Ścieżka zakupowa — awaria**
> **Etap 2/5: Dodanie produktu do koszyka**
>
> HTTP 500 Internal Server Error — Wewnętrzny błąd serwera lub aplikacji…
> Żądanie: `PATCH https://sklep.technica.pl/pwaapi/cart/me`
> Oczekiwano kodu: 200, 201
>
> **Co to oznacza dla klienta:** Przycisk „Do koszyka" nie działa — klient nie może
> niczego kupić. To najpoważniejsza awaria sklepu.
>
> **Co sprawdzić:** Sprawdź API koszyka, sesję TECHNICA_SID i logi aplikacji.
> Jeśli sku 127782 przestało istnieć, produkt został wycofany — zaktualizuj config.json.
>
> Awaria trwa: 11 min · Próg alarmu: 10 min

### Wpływ na analitykę i bazę sklepu

**GA4 i Google Ads nie zobaczą monitoringu.** Skrypt pobiera surowe odpowiedzi HTTP
i nie uruchamia JavaScriptu, a gtag, Google Ads i Meta Pixel działają w całości
po stronie przeglądarki. Nie przybędzie sesji, użytkowników ani zdarzeń.

Wyjątek: przy tagowaniu po stronie serwera (server-side GTM, Measurement Protocol)
odsłony mogłyby być liczone — warto to potwierdzić u osoby od analityki.

**Co będzie widoczne:**

- logi serwera i statystyki Cloudflare — ruch z `TechnicaMonitorBot`,
- backend sklepu — scenariusz **tworzy prawdziwe koszyki**. Przy `check_every_minutes: 22`
  to około 65 koszyków dziennie, oznaczonych `item_list_name: "Monitoring dostepnosci"`.

E-maile do klientów nie pójdą (koszyki nie mają adresu), ale wskaźnik porzuceń koszyka
się przesunie. Opcje: zwiększyć `check_every_minutes`, albo poprosić administratora
o wykluczenie tych sesji z raportów po nazwie `User-Agent`.

Pozostałe wpisy niczego w sklepie nie zapisują — to zwykłe odczyty.

---

## Powiadomienia

| Ikona | Zdarzenie | SMS? |
|---|---|---|
| 🔴 | awaria — zły kod HTTP, brak odpowiedzi, brak elementu, przerwana ścieżka zakupowa | tak |
| 🟢 | powrót do działania, z łącznym czasem przestoju | tak |
| 🟡 | zmiana treści strony — z fragmentem diffa | nie |
| 🟠 | zmiana pilnowanej wartości (cena, dostępność, kontakt) | nie |

SMS-y wysyłane są tylko dla wpisów mających `"sms"` w polu `notify`, tylko dla poziomów
z `sms_levels`, z blokadą `sms_cooldown_minutes` (domyślnie 30 min na wpis). Treść jest
skracana i pozbawiana polskich znaków — jeden znak spoza GSM 7-bit skraca wiadomość
ze 160 do 70 znaków, czyli podnosi koszt.

Przykład: `AWARIA Strona glowna sklepu: HTTP 502 Bad Gateway (trwa 5 min)`

### Wielu odbiorców SMS

Sekret `SMSAPI_TO` przyjmuje dowolną liczbę numerów rozdzielonych przecinkiem:

```
48500100200,48600200300,48700300400
```

Akceptowane są też średniki i nowe linie, a plusy, spacje i myślniki wewnątrz numeru
są usuwane automatycznie — `+48 500 100 200, +48 600 200 300` zadziała tak samo.
Numer 9-cyfrowy dostaje automatycznie prefiks `48`. Duplikaty i numery o nieprawidłowej
długości są pomijane, z odpowiednim wpisem w logu.

⚠️ **Każdy odbiorca to osobna, płatna wiadomość.** Trzy numery = potrójny koszt każdego
alarmu. Blokada `sms_cooldown_minutes` działa na wpis, nie na numer, więc chroni
przed serią SMS-ów, ale nie zmniejsza kosztu pojedynczego alarmu.

### Microsoft Teams

Wymaga webhooka z aplikacji **Workflows** (Power Automate). Klasyczne webhooki
Office 365 Connectors (`webhook.office.com`) Microsoft wyłączył w maju 2026.
Monitor wysyła Adaptive Card; `TEAMS_PAYLOAD=messagecard` przełącza na starszy format.

### SMSAPI — błąd 14 „Invalid from field"

Najczęstszy problem przy pierwszym uruchomieniu. Oznacza, że `SMSAPI_FROM` nie jest
zatwierdzonym polem nadawcy. Domyślnym polem nowego konta jest „Test".

- **Szybko:** usuń sekret `SMSAPI_FROM` — wiadomości pójdą od nadawcy domyślnego.
- **Docelowo:** panel SMSAPI → **Wiadomości SMS → Pole nadawcy** → dodaj własną nazwę
  (maks. 11 znaków, `a-z A-Z 0-9`, kropka, myślnik, spacja, bez polskich znaków)
  i poczekaj na ręczną weryfikację przez SMSAPI (pn–pt).

Monitor radzi sobie z tym sam: gdy pole nadawcy zostanie odrzucone, ponawia wysyłkę
bez tego pola, żeby alarm mimo wszystko dotarł.

Inne kody: 101 (zły token), 103 (brak punktów), 105 (blokada IP), 13 (zły numer odbiorcy).

---

## Struktura plików

```
technica-monitor/
├── .github/workflows/
│   ├── monitor.yml          # harmonogram co 10 min + pętla 25 min w środku
│   └── test-alerts.yml      # ręczny test kanałów powiadomień
├── state/
│   ├── state.json           # status, down_since, hashe, pilnowane wartości
│   └── snapshots/           # migawki treści (źródło diffów)
├── config.json              # wpisy, harmonogramy, progi, kanały
├── monitor.py               # skrypt monitorujący
├── requirements.txt         # requests, beautifulsoup4
├── README.md
└── INSTRUKCJA.md            # wdrożenie krok po kroku
```

Katalog `state/` jest commitowany z powrotem do repozytorium przez workflow — to „pamięć"
monitora między uruchomieniami. Żadna baza danych nie jest potrzebna.

---

## Konfiguracja

### Ustawienia globalne (`config.json`)

| Pole | Domyślnie | Znaczenie |
|---|---|---|
| `timeout_seconds` | 20 | limit czasu pojedynczego żądania |
| `retries` | 2 | liczba ponowień przy błędzie sieci lub kodzie z `retry_on_status` |
| `retry_delay_seconds` | 5 | odstęp między ponowieniami |
| `retry_on_status` | `[429,500,502,503,504]` | kody wyzwalające ponowienie |
| `immediate_alert_codes` | `[]` | kody alarmujące z pominięciem progu czasowego |
| `check_every_minutes` | 15 | domyślna częstotliwość |
| `alert_after_minutes` | 15 | domyślny próg alarmu |
| `notify` | Teams, Discord, Telegram | domyślne kanały |
| `sms_levels` | `["down"]` | poziomy wysyłane SMS-em |
| `sms_cooldown_minutes` | 30 | minimalny odstęp między SMS-ami dla wpisu |
| `user_agent` | `TechnicaMonitorBot/1.0` | identyfikacja monitora |

Każde z tych pól można nadpisać osobno w dowolnym wpisie.

### Pola wpisu

| Pole | Znaczenie |
|---|---|
| `enabled` | `false` wyłącza wpis bez usuwania |
| `type` | `flow` dla scenariusza wielokrokowego; brak = zwykła strona |
| `expected_status` | oczekiwany kod HTTP |
| `keyword_required` | fraza, która musi być na stronie |
| `required_elements` | nazwane wzorce, które muszą wystąpić (przyciski CTA, pola JSON) |
| `check_content` | czy porównywać treść i zgłaszać zmiany |
| `content_between` | kotwice ograniczające porównanie do właściwej treści |
| `ignore_patterns` | wyrażenia regularne wycinane przed porównaniem |
| `watch_fields` | nazwane wartości do śledzenia (cena, kontakt) |
| `notify`, `sms_levels` | kanały i poziomy dla tego wpisu |

### Pola kroku w scenariuszu (`steps`)

| Pole | Znaczenie |
|---|---|
| `method` | GET, POST, PATCH… |
| `json` / `form` | ciało żądania (JSON albo formularz) |
| `headers` | dodatkowe nagłówki |
| `expected_status` | lista akceptowanych kodów |
| `required_elements` | wzorce, które muszą wystąpić w odpowiedzi |
| `forbidden_text` | wzorzec, który **nie może** wystąpić |
| `extract` | wartości do wykorzystania w kolejnych krokach jako `{nazwa}` |
| `impact` | opis skutku dla klienta, trafia do treści alarmu |
| `hint` | podpowiedź diagnostyczna, trafia do treści alarmu |

### Dlaczego treść porównywana jest tylko fragmentami

Sklep działa na AtomStore, gdzie każda podstrona zawiera to samo menu kategorii
(tysiące linków) i tę samą stopkę. Hashowanie całego HTML dawałoby alarm przy każdej
edycji dowolnej kategorii w sklepie. Dlatego wpisy mają:

```json
"content_between": { "start": "Wyprzedaż", "end": "Zapisz się do newslettera" }
```

Porównywana jest wyłącznie treść między końcem menu a stopką. Skrypt dodatkowo usuwa
`<script>`, `<style>`, komentarze, tokeny CSRF, `nonce`, cache-bustery i znaczniki czasu.

---

## Polecenia

```bash
python monitor.py                              # jeden przebieg
python monitor.py --loop-minutes 25            # tryb pętli (jak w GitHub Actions)
python monitor.py --only sciezka-zakupowa --dry-run   # jeden wpis, bez wysyłki
python monitor.py --only kontakt --dry-run     # podgląd pilnowanych wartości
python monitor.py --test-alerts                # test kanałów bez SMS
python monitor.py --test-alerts --with-sms     # test wraz z SMS (płatny)
```

`--dry-run` nie wysyła powiadomień i nie zapisuje stanu — bezpieczny do eksperymentów
z wzorcami.

### Sekrety w GitHub Actions

| Sekret | Kanał |
|---|---|
| `TEAMS_WEBHOOK_URL` | Microsoft Teams |
| `DISCORD_WEBHOOK_URL` | Discord |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram |
| `SMSAPI_TOKEN` + `SMSAPI_TO` + `SMSAPI_FROM` | SMS |

Każdy kanał jest opcjonalny — skrypt pomija ten, dla którego nie ma sekretu.

---

## Rozwiązywanie problemów

### Czerwony krzyżyk przy uruchomieniu

- `403` przy kroku „Zapisz stan" → **Settings → Actions → General → Workflow permissions**
  → **Read and write permissions**
- `No such file or directory: config.json` → plik nie został wgrany lub ma złą nazwę
- błąd przy instalacji zależności → brak `requirements.txt`

### Zakładka Actions pusta

Plik musi leżeć dokładnie w `.github/workflows/monitor.yml` — sprawdź, czy folder
nazywa się `.github` (z kropką).

### Uruchomienie zielone, ale cisza na Teams

1. W logu kroku „Monitoruj sklep" poszukaj linii `[+] Teams` albo `[!] Teams`.
2. `HTTP 400/401` → w Power Automate pole **„Kto może wyzwolić przepływ?"** musi być
   ustawione na **Każdy**.
3. Sprawdź, czy adres webhooka nie został ucięty przy kopiowaniu (ma ponad 200 znaków).
4. Power Automate → **Moje przepływy** → **Historia uruchomień** pokaże błędy po stronie
   przepływu.

### Fałszywe alarmy ze ścieżki zakupowej

Uruchom `python monitor.py --only sciezka-zakupowa --dry-run` — zobaczysz, który krok
nie przechodzi. Najczęstsze przyczyny: zmiana `sku` produktu, zmiana struktury odpowiedzi
API po aktualizacji AtomStore, wykluczenie produktu z metod dostawy w panelu sklepu.

### Za dużo powiadomień o zmianie treści

Ustaw dla danego wpisu `"check_content": false` albo dopisz regułę do `ignore_patterns`.
Listingi kategorii potrafią rotować kolejność produktów.

### Alarm HTTP 403 przy działającym sklepie

Zapora (WAF/Cloudflare) uznała monitor za bota. Poproś administratora o wyjątek dla
`TechnicaMonitorBot`.

---

## Znane ograniczenia

- **Harmonogram GitHuba nie jest gwarantowany.** Mimo zastosowanych obejść pojedyncze
  uruchomienia mogą zostać pominięte. Do twardej gwarancji „alarm w 5 minut" właściwym
  narzędziem jest zewnętrzna usługa uptime albo własny serwer z cronem.
- **Repozytorium musi być publiczne.** Pętla zużywa ~9 minut co 10 minut; darmowy limit
  2000 minut miesięcznie dla repozytoriów prywatnych wyczerpałby się w półtora dnia.
  Migawki treści w `state/` będą wtedy publicznie widoczne; tokeny i webhooki pozostają
  w sekretach.
- **Scenariusz zakupowy tworzy realne koszyki** — patrz sekcja o wpływie na bazę sklepu.
- **Zmiana szablonu lub API sklepu unieważni wzorce.** Monitor zgłosi to jako `BRAK`
  albo awarię kroku; wtedy trzeba poprawić `config.json`.

### Znana usterka sklepu (poza monitoringiem)

Endpoint `GET /pwaapi/product/landing/670/1` zwraca **HTTP 500** na stronie koszyka.
Prawdopodobnie dotyczy sekcji rekomendacji — skutek dla klienta jest kosmetyczny
(utracony cross-sell), zakup nie jest blokowany. **Celowo nie dodano go do monitoringu**,
bo jest zepsuty stale i alarmowałby przy każdym uruchomieniu, zagłuszając prawdziwe
awarie. Warto zgłosić do AtomStore i dopiero po naprawie objąć monitoringiem.
