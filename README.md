# Monitoring sklep.technica.pl — dostępność, kody HTTP, CTA i zmiany treści

Monitoring oparty o Pythona i GitHub Actions. Sprawdza dostępność stron, obecność
kluczowych przycisków, poprawność ścieżki zakupowej oraz zmiany treści.
Powiadamia na **Discordzie**, **Telegramie**, **Microsoft Teams** i **SMS-em (SMSAPI.pl)**.

## Monitorowane wpisy

| id | Adres / zakres | Sprawdzane co | Alarm po | Kanały |
|---|---|---|---|---|
| `strona-glowna` | https://sklep.technica.pl | 1 min | **5 min** | Teams, Discord, Telegram, **SMS** |
| `koszyk-pusty` | https://sklep.technica.pl/cart | 1 min | **10 min** | Teams, Discord, Telegram, **SMS** |
| `checkout-dostepnosc` | https://sklep.technica.pl/checkout | 1 min | **10 min** | Teams, Discord, Telegram, **SMS** |
| `sciezka-zakupowa` | produkt → koszyk → checkout, razem z CTA | 10 min | 10 min | Teams, Discord, Telegram, **SMS** |
| `kontakt` | https://sklep.technica.pl/kontakt | 15 min | 15 min | Teams, Discord, Telegram |
| `produkt-450021` | karta produktu (barowa witryna chłodnicza) | 15 min | 15 min | Teams, Discord, Telegram |
| `kategoria-szafy-chlodnicze` | https://sklep.technica.pl/szafy-chlodnicze | 15 min | 15 min | Teams, Discord, Telegram |

Wpis `sciezka-zakupowa` jest domyślnie **wyłączony** (`"enabled": false`) — wymaga
jednorazowego uzupełnienia adresu dodawania do koszyka, patrz sekcja „Scenariusz zakupowy”.

---

## Jak działa harmonogram i próg alarmu

To dwa niezależne ustawienia, obydwa **osobne dla każdego wpisu**:

```json
"check_every_minutes": 1,     ← jak często sprawdzać
"alert_after_minutes": 5      ← jak długo musi trwać awaria, zanim przyjdzie alarm
```

Monitor zapamiętuje moment pierwszego niepowodzenia (`down_since`) i alarmuje dopiero
wtedy, gdy awaria trwa nieprzerwanie dłużej niż `alert_after_minutes`. Chwilowy błąd,
który sam mija w minutę, nie budzi nikogo w nocy. Alarm o awarii przychodzi **raz**;
drugie powiadomienie to dopiero informacja o powrocie do działania, z podanym łącznym
czasem przestoju.

### Jak uzyskaliśmy rozdzielczość 1 minuty

GitHub Actions **nie pozwala planować zadań częściej niż co 5 minut**, a w praktyce
uruchamia je z opóźnieniem sięgającym kilkunastu minut. Sam cron nie wystarczy więc do
progu 5-minutowego.

Dlatego workflow startuje co 10 minut, ale skrypt **pracuje w pętli przez ~9 minut**,
wykonując przebieg co 60 sekund (`--loop-minutes 9 --pass-seconds 60`). W każdym przebiegu
sprawdzane są tylko te wpisy, którym minął ich własny `check_every_minutes`.

Uczciwie o ograniczeniach: między końcem jednego uruchomienia a startem następnego zostaje
przerwa (zwykle ~1 min, przy obciążeniu GitHuba dłuższa), a pojedyncze uruchomienie może
zostać pominięte. Realny czas wykrycia awarii to **około 5–8 minut** zamiast dokładnie 5.
Jeśli potrzebujesz twardej gwarancji, właściwym narzędziem jest zewnętrzny monitoring
uptime lub własny serwer z cronem — GitHub Actions to rozwiązanie „wystarczająco dobre
i darmowe", nie system o gwarantowanym czasie reakcji.

⚠️ **Ten harmonogram wymaga repozytorium publicznego.** Pętla zużywa ~9 minut co 10 minut,
czyli ~1300 minut dziennie. Darmowy limit dla repozytoriów prywatnych to 2000 minut
**miesięcznie** — wyczerpałby się w półtora dnia. W repozytoriach publicznych uruchomienia
są bezpłatne i bez limitu.

---

## Monitorowane kody odpowiedzi HTTP

Każdy kod inny niż oczekiwany wyzwala procedurę awarii. Powiadomienie zawiera znaczenie
kodu i podpowiedź, co sprawdzić.

### Błędy po stronie klienta (4xx)

| Kod | Znaczenie | Zachowanie |
|---|---|---|
| **403 Forbidden** | Dostęp zabroniony — problem z uprawnieniami albo blokada przez WAF | Alarm po progu czasowym. Podpowiedź: reguły WAF/Cloudflare, blokady IP, uprawnienia katalogów |
| **404 Not Found** | Adres nie istnieje na serwerze | Alarm. Dla produktu/kategorii zwykle usunięcie lub zmiana URL |
| **429 Too Many Requests** | Przekroczony limit zapytań (rate limiting) | Ponowienie z respektowaniem nagłówka `Retry-After` (maks. 60 s), potem alarm |

Rozpoznawane są też 400, 401, 405 i 410; pozostałe kody 4xx opisywane są ogólnie.

### Błędy po stronie serwera (5xx)

| Kod | Znaczenie | Podpowiedź w alarmie |
|---|---|---|
| **500 Internal Server Error** | Błąd aplikacji, najczęściej PHP lub baza | Sprawdź logi błędów PHP |
| **502 Bad Gateway** | Nginx/Cloudflare dostał złą odpowiedź od backendu | Sprawdź PHP-FPM, czy proces nie został ubity (OOM) |
| **503 Service Unavailable** | Przeciążenie lub tryb konserwacji | Sprawdź obciążenie i tryb maintenance |
| **504 Gateway Timeout** | Serwer nie odpowiedział w limicie czasu | Sprawdź czas zapytań SQL |

Wszystkie kody podlegają teraz progowi `alert_after_minutes`. Jeśli chcesz, by konkretne
kody alarmowały **natychmiast**, z pominięciem progu, dopisz je w `config.json`:

```json
"immediate_alert_codes": ["5xx"]
```

Alarm wyzwala też brak odpowiedzi serwera (DNS, timeout, wygasły certyfikat SSL) oraz
brak frazy kontrolnej lub wymaganego elementu mimo kodu 200.

---

## Scenariusz zakupowy — dlaczego zwykły GET nie wystarczy

Zweryfikowałem to na żywym sklepie: **`/cart` i `/checkout` otwarte bez sesji zwracają
HTTP 200, ale ich treść jest pusta.** Nie ma tam przycisku „Przejdź do kasy", „Zapytaj
o ofertę naszego handlowca" ani „Zamów i zapłać" — bo bot monitorujący nie ma koszyka.
Wpisanie tych fraz do zwykłego sprawdzenia strony dałoby alarm co minutę, przez całą dobę.

Dlatego przyciski CTA sprawdza osobny wpis typu `flow`: monitor otwiera kartę produktu,
**faktycznie dodaje produkt do koszyka**, a dopiero potem wchodzi na `/cart` i `/checkout`
w tej samej sesji (ciasteczka są zachowywane). Wtedy przyciski są widoczne i można
sprawdzić, czy istnieją.

Dwa niezależne poziomy kontroli:

- **`koszyk-pusty` i `checkout-dostepnosc`** — sprawdzają tylko, czy strony w ogóle
  odpowiadają (HTTP 200). Działają od razu, bez konfiguracji.
- **`sciezka-zakupowa`** — sprawdza, czy klient realnie może kupić. Wymaga uzupełnienia
  jednego adresu.

### Uzupełnienie adresu dodawania do koszyka (jednorazowo, ~5 minut)

Adresu i nazw pól formularza nie da się odgadnąć — są specyficzne dla wdrożenia AtomStore.
Odczytasz je z przeglądarki:

1. Otwórz kartę produktu w Chrome.
2. Naciśnij **F12** → zakładka **Network** (Sieć) → zaznacz filtr **Fetch/XHR**.
3. Kliknij na stronie **„Do koszyka"**.
4. Na liście pojawi się nowe żądanie — kliknij je.
5. Z zakładki **Headers** skopiuj **Request URL** (np. `https://sklep.technica.pl/cart/add`).
6. Z zakładki **Payload** (lub **Request** → **Form Data**) spisz nazwy i wartości pól,
   np. `product_id: 450021`, `quantity: 1`, czasem też token CSRF.

Następnie w `config.json`, we wpisie `sciezka-zakupowa`:

```json
{
  "name": "Dodanie produktu do koszyka",
  "method": "POST",
  "url": "TU_WKLEJ_REQUEST_URL",
  "form": { "product_id": "{product_id}", "quantity": "1" }
}
```

Jeśli w Form Data jest token CSRF, dodaj go do kroku pierwszego w sekcji `extract`
(wzorzec regex wyciągający wartość ze strony), a potem użyj jako `"{nazwa}"` w `form`.

Na koniec zmień `"enabled": false` na `"enabled": true` i przetestuj:

```bash
python monitor.py --only sciezka-zakupowa --dry-run
```

Zobaczysz każdy krok z osobna i dowiesz się, który nie przechodzi.

### Co konkretnie sprawdza scenariusz

| Krok | Warunek zaliczenia |
|---|---|
| Karta produktu | HTTP 200 + obecny przycisk „Do koszyka" |
| Dodanie do koszyka | HTTP 200 lub 302 |
| `/cart` | obecne: „Przejdź do kasy", „Zapytaj o ofertę", „Suma brutto"; **brak** tekstu „Twój koszyk jest pusty" |
| `/checkout` | obecne: „Zamów i zapłać", sekcja dostawy, sekcja płatności; **brak** „Twój koszyk jest pusty" |

Niepowodzenie dowolnego kroku daje alarm z nazwą kroku i brakującego elementu — na przykład
„Krok „Checkout z produktem": strona odpowiada poprawnie (HTTP 200), ale brakuje elementów:
przycisk Zamów i zapłać". To najcenniejszy sygnał w całym monitoringu: sklep formalnie
działa, a mimo to nikt nie może złożyć zamówienia.

⚠️ Scenariusz **nie składa zamówienia** — kończy się na wyświetleniu checkoutu. Nie generuje
żadnych zamówień testowych ani płatności.

---

## Powiadomienia SMS (SMSAPI.pl)

SMS-y kosztują, więc są traktowane inaczej niż pozostałe kanały:

- wysyłane tylko dla wpisów, które mają `"sms"` w polu `notify` (obecnie: strona główna,
  koszyk, checkout, ścieżka zakupowa),
- tylko dla poziomów z `sms_levels` — domyślnie awaria (`down`) i powrót (`up`);
  zmiany treści i cen **nie idą SMS-em**,
- z blokadą częstotliwości `sms_cooldown_minutes` (domyślnie 30 min na wpis),
- treść jest skracana i pozbawiana polskich znaków. To nie kosmetyka: jeden znak spoza
  GSM 7-bit skraca pojedynczą wiadomość ze 160 do 70 znaków, czyli podnosi koszt wysyłki.

Przykładowa treść: `AWARIA Strona glowna sklepu: HTTP 502 Bad Gateway (trwa 5 min)`

### Konfiguracja

1. Zaloguj się na https://ssl.smsapi.pl i wygeneruj **token OAuth**
   (Ustawienia → API / „Zarządzanie tokenami"). Zalecane jest ograniczenie uprawnień
   tokena wyłącznie do wysyłki oraz filtrowanie adresów IP.
2. Sprawdź swoje **pole nadawcy** (nazwa nadawcy) w panelu — musi być zatwierdzone.
   Bez tego parametru SMSAPI wyśle wiadomość jako tańszą „Eco" z generycznym nadawcą.
3. Dodaj sekrety w GitHubie (**Settings → Secrets and variables → Actions**):

| Sekret | Wartość |
|---|---|
| `SMSAPI_TOKEN` | token OAuth z panelu SMSAPI |
| `SMSAPI_TO` | numer odbiorcy, np. `48500100200`; kilka numerów po przecinku |
| `SMSAPI_FROM` | zatwierdzone pole nadawcy, np. `TECHNICA` (opcjonalne) |

#### Błąd 14 „Invalid from field"

Najczęstszy problem przy pierwszym uruchomieniu. Oznacza, że wartość `SMSAPI_FROM`
nie jest zatwierdzonym polem nadawcy na Twoim koncie. Samo wpisanie dowolnej nazwy
nie wystarczy — każde pole nadawcy SMSAPI weryfikuje ręcznie (pn–pt 8–17).

Dwie drogi wyjścia:

- **Szybka:** usuń sekret `SMSAPI_FROM`. Wiadomości pójdą wtedy od nadawcy domyślnego
  (SMS Eco) i dotrą od razu.
- **Docelowa:** w panelu SMSAPI wejdź w **Ustawienia → Pola nadawcy**, dodaj nazwę
  i poczekaj na akceptację. Limit to 11 znaków: `a-z A-Z 0-9`, kropka, myślnik, spacja —
  bez polskich znaków i bez numeru telefonu.

Monitor sam sobie z tym radzi: gdy SMSAPI odrzuci pole nadawcy, wysyłka jest ponawiana
bez tego pola, żeby alarm o awarii sklepu mimo wszystko dotarł. W logu zobaczysz wtedy
wyjaśnienie i przypomnienie o poprawieniu sekretu.

Inne kody błędów opisane w logu: 101 (zły token), 103 (brak punktów na koncie),
105 (blokada IP w ustawieniach tokena), 13 (zły numer odbiorcy).

4. Test (uwaga — wyśle prawdziwy, płatny SMS):

```bash
python monitor.py --test-alerts --with-sms
```

Bez flagi `--with-sms` test obejmuje tylko Teams, Discord i Telegram.

Monitor korzysta z `https://api.smsapi.pl/sms.do`, a przy niepowodzeniu automatycznie
ponawia próbę przez adres zapasowy `https://api2.smsapi.pl/sms.do`.

---

## Struktura plików

```
website-monitor/
├── .github/workflows/monitor.yml   # cron co 10 min + pętla 9 min w środku
├── state/
│   ├── state.json                  # status, down_since, hashe, pilnowane wartości
│   └── snapshots/                  # migawki treści (źródło diffów)
├── config.json                     # wpisy, harmonogramy, progi, kanały
├── monitor.py
├── requirements.txt
├── README.md
└── INSTRUKCJA.md                   # wdrożenie krok po kroku dla laika
```

## Poziomy powiadomień

| Ikona | Zdarzenie | SMS? |
|---|---|---|
| 🔴 | awaria — zły kod HTTP, brak odpowiedzi, brak frazy/elementu, przerwana ścieżka zakupowa | tak (dla wpisów z `sms`) |
| 🟢 | powrót do działania, z łącznym czasem przestoju | tak |
| 🟡 | zmiana treści strony — z fragmentem diffa | nie |
| 🟠 | zmiana pilnowanej wartości (cena, dostępność, kontakt) | nie |

## Przydatne polecenia

```bash
python monitor.py                              # jeden przebieg
python monitor.py --loop-minutes 9             # tryb pętli (jak w GitHub Actions)
python monitor.py --only checkout-dostepnosc --dry-run
python monitor.py --only sciezka-zakupowa --dry-run
python monitor.py --test-alerts                # test kanałów bez SMS
python monitor.py --test-alerts --with-sms     # test wraz z SMS (płatny)
```

## Co warto wiedzieć

- **Cron GitHuba bywa opóźniony** i pojedyncze uruchomienie może zostać pominięte —
  patrz sekcja o harmonogramie.
- **Repozytorium musi być publiczne** przy tym harmonogramie. Migawki treści w `state/`
  będą wtedy publicznie widoczne; tokeny i webhooki pozostają w sekretach.
- **403 z zapory**: monitor przedstawia się jako `TechnicaMonitorBot` — poproś
  administratora o wyjątek, jeśli WAF zacznie go blokować.
- **Zmiana szablonu sklepu** unieważni wzorce w `watch_fields` i `required_elements`.
  Wartości zmienią się na `BRAK` i dostaniesz o tym powiadomienie; wtedy popraw wzorce
  i sprawdź je przez `--only <id> --dry-run`.
- **Za dużo alarmów o zmianie treści?** Ustaw dla danego wpisu `"check_content": false`
  albo dopisz regułę do `ignore_patterns`.
