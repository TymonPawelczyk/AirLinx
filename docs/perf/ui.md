# Wydajność okna UI (WebKitGTK)

Zgłoszenie: „okno aplikacji nie jest płynne”. Pomiary z 11 listopada 2026, zmiany na gałęzi `feature/perf-ui`.

## Podsumowanie

- Przyczyna: WebKitGTK maluje UI na CPU w głównym wątku procesu WWW, a każda zmiana tekstu pod telewizorem
  (liczby na żywo, zegar) przemalowuje obszar z drogimi prymitywami: `filter: blur(60px)` poświaty, rozmyty
  `box-shadow` ramki TV, `inset` box-shadow i gradient na całej ramce. Koszt rośnie z liczbą pikseli okna.
- Efekt na oknie 2048×1152 (to jest 4096×2304 px przy skali 2× na monitorze 4K), stan „Na żywo”:
  **1,9 → 58 kl./s**, proces WebKitWebProcess **99,5% → 9% rdzenia**, długie zatrzymania wątku (>50 ms) 13 → 7
  na 6 s, najdłuższa klatka 654 → 88 ms. W domyślnym oknie 1100×720: **43,9 → 62,4 kl./s**, zatrzymania 13 → 0.
- Poprawki są w CSS i JS (bez zmiany wyglądu). Ustawienia WebView (`app.py`) zostają bez zmian, bo nic nie dają
  (dane niżej).
- GTK4/libadwaita: nie warto (sekcja „Rekomendacja”).

## Środowisko i metoda

- PC: GNOME na X11, RTX 3080, sterownik NVIDIA 580.196.07 (`nvidia-drm modeset=1`), WebKitGTK 2.50.4 (`gir1.2-webkit2-4.1`),
  GTK 3.24.33. Monitory: 4096×2304 i 5120×2880. GDK podaje dla ekranu 4096×2304 skalę **2×** (logicznie 2048×1152); dla
  5120×2880 zakładam tę samą skalę (2560×1440 logicznie). Dlatego okno „2048×1152” to 4096×2304 pikseli
  urządzenia, a domyślne 1100×720 to 2200×1440.
- Maszyna była w trakcie pomiarów używana przez inne zadania (GPU 20–40%, jeden rdzeń zajęty), więc liczby mają
  rozrzut. Wnioski opierają się na różnicach rzędu wielkości i na powtórzeniach (każdy wariant 2–3 razy).
- **Narzędzie: `scripts/perf_ui_wkbench.py`.** Prawdziwy `Gtk.Window` + `WebKit2.WebView` z tymi samymi ustawieniami co
  `airlinx/app.py` (`HardwareAccelerationPolicy.ALWAYS`, tło czarne), UI z `ui/index.html?mock` (mock bridge, więc
  bez TV). W stronie: pętla `requestAnimationFrame` (czasy klatek), timer 10 ms (zatrzymania głównego wątku: dryf
  >50 ms), a z zewnątrz CPU procesów WebKit z `/proc` i obciążenie GPU z `nvidia-smi`. Scenariusze po 6 s:
  `idle` (ekran roboczy), `live` (po „Udostępnij ekran”, telemetria 2/s + zegar), `details` (live z panelem
  „Szczegóły” i wykresami) oraz `anim` (scenariusz: włączenie, arkusz źródeł, menu TV, ustawienia, zakładka).
- **Prawdziwej aplikacji (`python -m airlinx --debug`) nie uruchamiałem.** Na tym komputerze działa już zainstalowana
  instancja (`~/.local/share/airlinx`), a `Gtk.Application` jest jednoinstancyjna, więc drugi start aktywowałby
  okno użytkownika. Harness używa identycznego WebView i tego samego kodu UI, więc koszt malowania jest ten sam.
  Brakuje tylko prawdziwego mostu RPC; jego koszt zmierzyłem osobno (niżej).
- **Chromium/Playwright (CDP) nie odtwarza problemu.** Headless Chromium daje 60 kl./s i 0 długich zadań we
  wszystkich scenariuszach, także na 5120×2880, bo nie rastruje strony, dopóki nikt nie robi zrzutu. Z CDP
  (`Performance.getMetrics`) wynika natomiast, że **JS nie jest kosztem**: skrypt 0,1% czasu, layout 0,0–0,2%,
  style 0%. Decyzje oparłem więc na WebKitGTK.

## Wyniki przed i po

Wszystkie wartości z `scripts/perf_ui_wkbench.py`, 6 s na przypadek. „Zatrzymania” to dryf timera 10 ms >50 ms
(liczba / suma w ms). „CPU WebKit” to WebKitWebProcess w % jednego rdzenia. Surowe dane JSON: `docs/perf/ui-raw/`.

### Stan „Na żywo” (telemetria + zegar)

| Okno (px logiczne) | | kl./s | mediana klatki | p95 | p99 | najdłuższa | klatki >33 ms | zatrzymania | CPU WebKit |
|---|---|---|---|---|---|---|---|---|---|
| 1100×720 | przed | 43,9 | 16 ms | 42 ms | 173 ms | 187 ms | 14 | 13 / 2162 ms | 40,1% |
| 1100×720 | **po** | **62,4** | 16 ms | 17 ms | 29 ms | 30 ms | 0 | 0 / 0 ms | 3,2% |
| 2048×1152 | przed | 1,9 | 528 ms | 654 ms | 654 ms | 654 ms | 12 | 13 / 6859 ms | 99,5% |
| 2048×1152 | **po** | **58,3** | 16 ms | 17 ms | 87 ms | 88 ms | 7 | 7 / 548 ms | 9,1% |
| 2560×1440 | przed | 1,9 | 514 ms | 572 ms | 572 ms | 572 ms | 12 | 13 / 6631 ms | 99,6% |
| 2560×1440 | **po** | **58,1** | 16 ms | 17 ms | 88 ms | 88 ms | 7 | 7 / 554 ms | 9,1% |

### „Na żywo” z panelem „Szczegóły” (wykresy, ścieżka sygnału)

| Okno | | kl./s | p95 | p99 | najdłuższa | zatrzymania | CPU WebKit |
|---|---|---|---|---|---|---|---|
| 1100×720 | przed | 55,6 | 29 ms | 83 ms | 85 ms | 13 / 1030 ms | 23,5% |
| 1100×720 | **po** | **62,9** | 17 ms | 21 ms | 22 ms | 0 / 0 ms | 2,9% |
| 2048×1152 | przed | 29,1 | 296 ms | 320 ms | 348 ms | 13 / 3824 ms | 69,7% |
| 2048×1152 | **po** | **60,2** | 17 ms | 60 ms | 61 ms | 5 / 260 ms | 8,3% |
| 2560×1440 | przed | 32,5 | 271 ms | 279 ms | 315 ms | 13 / 3465 ms | 65,2% |
| 2560×1440 | **po** | **60,4** | 17 ms | 60 ms | 61 ms | 3 / 162 ms | 8,3% |

### Spoczynek (ekran roboczy, bez sesji)

Przed i po 62,3 kl./s, brak zatrzymań. CPU WebKit przy 2048×1152: 9,3% → 1,9%, przy 2560×1440: 8,0% → 2,1%.

### Animacje przejściowe (scenariusz `anim`)

kl./s w trakcie przejścia (najdłuższa klatka w nawiasie).

| Przejście | 1100×720 przed | 1100×720 po | 2048×1152 po |
|---|---|---|---|
| Start sesji (włączenie matrycy, poświata) | 18,7 (201 ms) | 42,4 (75 ms) | 24,3 (176 ms) |
| Otwarcie arkusza źródeł | 10,1 (335 ms) | 35,0 (132 ms) | 11,2 (216 ms) |
| Zamknięcie arkusza | 6,1 (265 ms) | 40,1 (105 ms) | 18,4 (215 ms) |
| Otwarcie menu TV | 15,1 (263 ms) | 55,4 (25 ms) | 56,0 (24 ms) |
| Zamknięcie menu | 13,4 (87 ms) | 58,7 (31 ms) | 58,8 (24 ms) |
| Otwarcie ustawień | 40,7 (278 ms) | 62,4 (17 ms) | 62,1 (31 ms) |
| Zmiana zakładki ustawień | 32,9 (286 ms) | 63,1 (20 ms) | 59,6 (38 ms) |

Na 2048×1152 przed zmianami scenariusz `anim` nie dobiegł końca w 120 s (UI praktycznie stało przy starcie
sesji), więc kolumny „przed” nie ma. Start sesji i arkusz źródeł na 4096×2304 px nadal mają kilkanaście klatek na
sekundę przez ok. 1–2 s; jest to jednorazowe przejście (patrz „Czego nie ruszałem”).

### Most Python ⇄ JS (`airlinx/bridge.py`)

Serializacja listy źródeł z miniaturami (14 okien, 320×180 PNG w base64) to 1,2 MB JSON, `json.dumps` trwa ok. 10 ms
(2 monitory: 1,3 ms). Do tej pory robiło to `_resolve` w wątku GTK; teraz skrypt jest budowany w wątku asyncio, a w
GTK zostaje tylko `evaluate_javascript`. `mirror.stats` jest ograniczone do 2/s już w kontrolerze
(`STATS_MIN_INTERVAL`), więc most nie był wąskim gardłem.

## Jak do tego doszedłem (bisekcja)

1. `live` bez timera statystyk (500 ms): 62 kl./s, CPU 4%. Z timerem: 43 kl./s, CPU 42%. Wystarczy sam zapis
   `#s-lat.textContent`, żeby powstawało zatrzymanie ~100–300 ms. Layout trwa <1 ms, więc koszt to malowanie.
2. Ukrycie `.tv` zeruje zatrzymania, ukrycie `.rail`, `.bar` albo `.foot` nic nie zmienia. Koszt siedzi w
   telewizorze, mimo że liczby są pod nim.
3. Wycinanie po jednym (okno 1100×720, WebKitGTK): bez poświaty CPU 42 → 21%; bez cienia ramki −6 pp; bez
   `box-shadow: inset` i gradientu ramki zatrzymania znikają (gradient sam ~50 ms, dwa `inset` ~100 ms na
   4096×2304 px). Strona testowa z kosztownymi prymitywami, ale bez tej struktury, zatrzymań nie daje, więc to
   nakładanie się wielu kosztów, a nie jeden element.
4. `will-change: transform` na `.tv`, `.glow`, `.foot`, `.stats`, `contain: paint`, `isolation: auto`: **bez
   efektu** (zmierzone 2–3 razy). Przemalowanie obejmuje to, co jest pod zmienionym tekstem, niezależnie od warstw.
5. Ustawienia WebView: `HardwareAccelerationPolicy.ALWAYS` i `NEVER` dają identyczne wyniki; także
   `enable-webgl=0`, `enable-accelerated-2d-canvas=0`, `enable-page-cache=0` oraz zmienne środowiskowe
   `WEBKIT_DISABLE_DMABUF_RENDERER`, `WEBKIT_DISABLE_COMPOSITING_MODE`, `WEBKIT_DMABUF_RENDERER_*`,
   `WEBKIT_SKIA_ENABLE_CPU_RENDERING`, `__NV_PRIME_RENDER_OFFLOAD`. Zostawiam `app.py` bez zmian.
   Obserwacja do dalszego zbadania: proces WWW ma otwarte `/dev/dri/renderD128` i `/dev/nvidia0`, ale w jego
   wątkach nie ma osobnego wątku kompozytora, a całe malowanie obciąża wątek główny. `webkit://gpu` raportuje
   `Policy: never`, mimo ustawienia `ALWAYS`. Nie udało mi się tego zmienić bez wyłączania piaskownicy, czego nie
   robiłem. Warto to sprawdzić na innej wersji WebKitGTK/sterownika, bo przy prawdziwym kompozycie warstw problem
   by nie wystąpił.

## Zmiany

| Zmiana | Pliki | Dlaczego |
|---|---|---|
| Poświata „bias light” wypiekana w bitmapie: miniatura → canvas 64×37 + margines → saturacja 1,7 i gaussian σ 4,8 w JS → mały PNG (`bakeGlow`), wyświetlany jako `<img>` bez `filter` | `ui/app.js`, `ui/app.css`, `ui/live.css` | `filter: blur(60px) saturate(1.7)` na elemencie 800×465 przeliczał się przy każdej zmianie tekstu pod nim. Największy pojedynczy koszt (1,9 → 29 kl./s na 2048×1152). Liczone raz na miniaturę (ok. 18 ms w WebKitGTK, 17 ms w Chromium). Różnica w pikselach względem starej poświaty ≤3 poziomy. |
| Cień pod TV jako `border-image` (9-slice) z bitmap `assets/shade/tv-{dark,light}.png` zamiast `box-shadow: 0 40px 90px -30px` | `ui/app.css`, `ui/tokens.css`, `ui/assets/shade/`, `scripts/gen_tv_shadow.py` | Rozmyty cień był malowany od nowa razem z tym, co pod zmienionym tekstem. W motywie jasnym piksele cienia są identyczne jak przed zmianą. |
| Obrys ramki TV i matrycy: pseudoelementy z ramką 1 px zamiast `box-shadow: inset`; ramka w jednolitym kolorze (`--bezel`) zamiast gradientu `#161616→#070707` (ciemny) i `#2a2a2a→#111` (jasny) | `ui/app.css`, `ui/tokens.css` | `inset` i gradient na całej ramce kosztowały ~150 ms na 4096×2304 px. Różnica wizualna: 5-pikselowy pasek ramki ma stały odcień (do 8 poziomów w ciemnym, do 12 w jasnym motywie). |
| Telemetria rysowana raz na sekundę razem z zegarem, zapis tekstu tylko przy zmianie (`setText`), próbki wykresu co 1 s, `drawSpark` i pasek budżetu bez przebudowy, gdy dane są te same | `ui/app.js` | Trzy przemalowania na sekundę (2× statystyki, 1× zegar) → jedno. Zgodne z DESIGN.md (60 próbek, 1/s). Pierwsza próbka po starcie jest pokazana od razu. |
| `@keyframes rise` bez `filter: blur(4px)` | `ui/app.css` | Animowany blur całego panelu (ustawienia, szczegóły) malował się co klatkę: ustawienia 41 → 62 kl./s, zakładki 33 → 63 kl./s. Wejście jest teraz tylko opacity + 8 px. |
| Cień arkusza `0 40px 100px -20px` → `0 20px 40px -16px` | `ui/tokens.css` | Arkusz animuje się (transform + opacity), a duży blur był malowany co klatkę: otwarcie arkusza 6,7 → 11 kl./s na 2048×1152. |
| `prefers-reduced-motion`: `transition-duration: 0s` zamiast `.01ms` | `ui/app.css` | Poprawka a11y (niżej), a przy okazji mniej pracy. |
| `Bridge._done`: JSON budowany w wątku asyncio, w GTK tylko `eval_js` | `airlinx/bridge.py` | Lista źródeł z miniaturami to >1 MB; 10 ms `json.dumps` nie powinno blokować wątku GTK. |

### Poprawki a11y (xfail strict)

Obie miały jedną przyczynę. Przy `prefers-reduced-motion` reguła `*` skracała przejścia do `.01ms`. Przejście
`visibility` (także dziedziczone przez elementy potomne menu i arkusza, bo `transition-property: all`) nie jest wtedy
natychmiastowe, tylko kończy się po następnej klatce, więc `showMenu()` i `openDialog()` wołały `focus()` na
elemencie, który jeszcze był `visibility: hidden`. Naprawa: `transition-duration: 0s` w bloku
`prefers-reduced-motion` (`ui/app.css`). Usunąłem oba `xfail(strict=True)` z `tests/e2e/test_ui.py`.

### Czego nie ruszałem

- Animacja włączenia matrycy (`power-on`, 900 ms, `filter: brightness/saturate`) i przejście `saturate` podglądu.
  Test wariantów: bez filtra start sesji ~29 kl./s zamiast ~25 na 4096×2304 px, więc zysk jest mały, a efekt jest
  „jedynym efektownym ruchem” w DESIGN.md. Jednorazowe, ~1 s.
- Poświata `.glow` nadal ma `opacity` animowane 1,1 s przy starcie sesji (przejście warstwy, tanie po wypieczeniu).
- Plik `docs/design/**` (kopie makiety) jest nietknięty; źródłem prawdy dla aplikacji jest `ui/`.

## Weryfikacja wyglądu

`ui/shoot.py` wykonany przed i po zmianach (`SHOTS_DIR` w katalogu tymczasowym, `ui/shots/` nietknięte), 37 zrzutów.
Ekrany statyczne (główny, multimedia, ustawienia, menu, arkusz, błędy, oba motywy): różnice to ≤15 pikseli powyżej
16 poziomów jasności (antyaliasing rogów ramki) i pasek ramki 5 px. Zrzuty „na żywo” różnią się losowymi liczbami
mocka i zegarem. Poświata w stanie „na żywo”: 1–3 poziomy różnicy na próbkach pikseli (lewy brzeg, nad i pod TV).

## Rekomendacja

1. Zostaje WebKitGTK. Po poprawkach okno 1100×720 trzyma 62 kl./s przy 3% CPU, a 4096×2304 px ok. 58 kl./s przy
   9%. Pozostałe zatrzymania (~80 ms raz na sekundę na największych oknach) pochodzą z przemalowania co sekundę;
   można je zbić tylko prawdziwym kompozytem warstw albo rezygnacją z sekundowego zegara w oknie.
2. **GTK4/libadwaita: nie warto, na podstawie danych.** (a) Koszt wziął się z konkretnych prymitywów CSS i został
   usunięty zmianami o kilkudziesięciu liniach, bez zmiany silnika. (b) Natywny GTK4 oznacza przepisanie całego UI
   (ok. 3000 linii HTML/CSS/JS design systemu „OLED Cinema” i marki „Styk”) na widgety, a libadwaita narzuca własny
   wygląd, z którym ten design się kłóci. (c) `WebKitGTK 6.0` (wariant GTK4) to ten sam silnik; w systemie nie ma
   nawet typelib `WebKit-6.0` (są tylko `WebKit2-4.0/4.1`, `Gtk-4.0`, `Adw-1`), więc nie mam żadnego pomiaru, że
   byłby szybszy. Gdyby po tych poprawkach problem wrócił, najpierw sprawdziłbym, dlaczego proces WWW nie ma wątku
   kompozytora (punkt 5 wyżej), a nie zmieniał toolkit.
3. Przy każdej zmianie CSS dotykającej telewizora lub statystyk: nie dodawać dużych `box-shadow` z rozmyciem ani
   `inset`, nie dodawać `filter: blur()`, mierzyć `scripts/perf_ui_wkbench.py` na oknie ≥2048×1152 (patrz DESIGN.md §10).
