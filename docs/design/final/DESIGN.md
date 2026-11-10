# AirLinx: design system interfejsu

Wersja 1.0 · baza UI: wariant A „OLED Cinema” · marka: C „Styk” (`docs/brand/final/`).
Makieta: `index.html` (wszystkie stany, panel „Stany makiety” w lewym dolnym rogu). Zrzuty: `shots/`.

## 1. Idea

Okno to wyłączony panel OLED: czysta czerń `#000`, na której unosi się treść. Bohaterem jest podgląd w
formie fizycznego telewizora (ramka + zawsze czarna matryca, także w motywie jasnym). Gdy obraz idzie na
żywo, kolory treści rozlewają się wokół ramki jak podświetlenie „bias light”.

Akcent marki, **Fosfor** (`#3DF2A0` / jasny `#007A47`), to kolor rombu ze znaku i stanu „na antenie”.
Oznacza transmisję, akcję główną i aktywny wybór. Nie zalewamy nim dużych powierzchni.

Zasady:
1. Jeden bohater na ekranie: matryca telewizora. Wszystko inne jest ciche.
2. Najpierw stan, potem liczba, potem działanie (głos marki).
3. Szczegóły techniczne są dostępne, ale domyślnie schowane (panel „Szczegóły”, ustawienia Zaawansowane).
4. Liczby zawsze w JetBrains Mono z cyframi stałej szerokości, żeby nie skakały przy odświeżaniu.

## 2. Pliki

| Plik | Zawartość |
|---|---|
| `brand-tokens.css` | kopia `docs/brand/final/tokens.css` (zmienne `--ax-*`). Nie edytować ręcznie, synchronizować z marką. |
| `tokens.css` | warstwa semantyczna UI: role kolorów, warstwy pochodne, typografia, odstępy, promienie, ruch, `@font-face` |
| `components.css` | komponenty: przyciski, segment, przełącznik, suwak, pole, lista, znaczniki, wskaźnik stanu, arkusz, menu, karta, wykres, ścieżka sygnału |
| `app.css` | układ ekranów i stany aplikacji (atrybuty `data-*` na `.app`) |
| `app.js` | logika makiety (stany, przepływy, klawiatura, telemetria, drag & drop) |
| `assets/brand/` | logo (lockup, znak), ikona aplikacji |
| `assets/tray/{16,22,24}/` | ikony AppIndicator `airlinx-{idle,streaming,error}-symbolic.svg` |
| `assets/motion/` | animacja „łączenie” (SVG z CSS) |
| `assets/fonts/` | Inter 400/500, JetBrains Mono 500/800 + licencje OFL 1.1 |
| `shoot.py` | zrzuty Playwright 1100×720 (`python shoot.py [nazwa]`) |

Kolejność ładowania: `brand-tokens.css → tokens.css → components.css → app.css`.
Nie ma zależności sieciowych (CDN, Google Fonts). Fonty mają `local()` w pierwszej kolejności, a dołączone
pliki `.ttf` służą jako zapas. Działa w WebKit2GTK 2.4x: grid, `:has` nie jest wymagany, `backdrop-filter` nieużywany.

## 3. Tokeny

### Kolor (role)

| Token | Ciemny | Jasny | Rola |
|---|---|---|---|
| `--bg` | `#000000` | `#F2F2F0` | tło okna |
| `--surface` | `#0D0D0D` | `#FFFFFF` | menu, arkusze |
| `--surface-2` | `#161616` | `#E9E9E6` | studzienki |
| `--border` | `#2A2A2A` | `#D6D6D2` | obrysy kontrolek |
| `--border-subtle` | biel 7% | czerń 8% | podziały w szynie, listach |
| `--text` | `#F3F2EF` | `#0E0E0D` | tekst podstawowy |
| `--text-2` | `#A3A29E` | `#575754` | etykiety, opisy |
| `--text-3` | `#7E7D7A` | `#5F5F5B`* | podpisy, osie, metadane |
| `--accent` | `#3DF2A0` | `#007A47` | Fosfor: tally, akcja główna, wybór |
| `--accent-hover` | `#6CF7B8` | `#00663B` | hover akcji |
| `--on-accent` | `#00160B` | `#FFFFFF` | tekst na Fosforze |
| `--accent-text` | `#3DF2A0` | `#00663B`* | Fosfor jako tekst |
| `--warning` | `#F5B544` | `#8A5300` | zgoda na TV, uwaga |
| `--error` / `--error-text` | `#FF7A6B` | `#C2392B` / `#A82E22`* | błąd, utrata połączenia |
| `--success` | `#86D69A` | `#1F8F43` | gotowy / połączony |
| `--focus` | `#6CF7B8` | `#007A47` | pierścień fokusu |
| `--fill`, `--fill-2`, `--fill-3` | biel 4,5 / 8 / 13% | czerń 4 / 6,5 / 10% | tła kontrolek: spoczynek, hover/wybór, aktywny |
| `--accent-soft`, `--error-soft`, `--warning-soft` | 13–14% koloru | 9–10% koloru | tła ikon i znaczników stanu |
| `--screen` | `#000000` | `#000000` | matryca TV, zawsze czarna |

\* Odcienie przyciemnione względem `brand/final`, żeby tekst spełniał AA także na tłach `fill-2`/`fill-3`
i `*-soft` (szczegóły w §7). Wartości marki dla innych zastosowań zostają bez zmian.

### Typografia

| Rola | Krój | Rozmiar / waga | Klasa |
|---|---|---|---|
| Tytuł ekranu | JetBrains Mono | 36 px / 500, tracking −0,035em | `.t-display` |
| Tytuł sekcji, komunikatu, filmu | JetBrains Mono | 22 px / 500, −0,025em | `.t-title` |
| Statystyki na żywo | JetBrains Mono | 32 px / 400, `tnum` | `.stat .v` |
| Liczby w tekście (czas, IP, ms) | JetBrains Mono | 11,5–12,5 px / 400–500 | `.num`, `.mono` |
| Tekst UI | Inter | 14 px / 400, interlinia 1,5 | `body` |
| Etykiety, opisy | Inter | 13 px / 500 lub 12,5 px / 400 | `.g-label`, `.r-d` |

Duże statystyki mają wagę 400, a nie 500 z marki, żeby zachować spokój kinowy przy 32 px. Etykiet nie piszemy
wersalikami. W statusach separatorem jest kropka środkowa ` · `.

### Odstępy, promienie, układ

- Skala 4 px: `--sp-1…--sp-10` (4, 8, 12, 16, 20, 24, 32, 40).
- Promienie: `--r-sm` 4 (znaczniki, miniatury), `--r-md` 8 (przyciski, pola, segmenty), `--r-lg` 12 (karty,
  akcja główna, menu), `--r-xl` 18 (arkusze, ramka TV), `--r-pill` (tylko przełączniki).
- Okno: domyślnie 1100×720, minimum 860×560. Pasek 52 px. Szyna po prawej 312 px (280 px poniżej 1000 px szerokości).
- Scena: szerokość telewizora liczona z wysokości okna `(100vh − pasek − rezerwa) · 16/9`, więc matryca zawsze
  mieści się z podpisem pod spodem.

## 4. Komponenty

| Komponent | Klasa | Uwagi |
|---|---|---|
| Przycisk | `.btn` (+ `.btn-sm`, `.btn-lg`, `.btn-quiet`) | 36 px, `--r-md`, naciśnięcie `scale(.97)` |
| Akcja główna | `.btn-primary` | 54 px, Fosfor, ikona w zagnieżdżonym kwadracie `.nest`. Najwyżej jedna na ekranie. |
| Akcja akcentowa | `.btn-accent` | mniejsza wersja akcji głównej (Szukaj ponownie, Udostępnij w arkuszu) |
| Zatrzymaj | `.btn-stop` | `--error-soft` + `--error-text`, pełna szerokość szyny |
| Ikona | `.icon-btn` | 34×34, zawsze z `aria-label` |
| Rozwijanie | `.disclosure` | `aria-expanded` + `aria-controls`, chevron obraca się o 180° |
| Segment | `.seg` + `role="radiogroup"` | strzałki zmieniają wybór (roving tabindex) |
| Przełącznik | `.switch` + `role="switch"` | `aria-checked`. Włączony = Fosfor, gałka `--on-accent`. |
| Suwak | `.range` (+ `.neutral`) | wypełnienie przez zmienną `--v`. Akcent tylko dla jakości obrazu. |
| Pole | `.field` | etykieta zawsze widoczna (nad polem lub `.sr`), fokus = obrys `--focus` |
| Lista wyboru | `.select` | natywny `<select>` ze strzałką SVG |
| Znacznik | `.badge` (+ `.hot`), `.tag` | JetBrains Mono 11 px. `.hot` = cecha premium (4K HDR10). |
| Wskaźnik stanu | `.dot[data-s]`, `.tally` | ok · busy · wait (bursztynowy pierścień) · err. Stan live to **romb** Fosforu, nieruchomy. |
| Arkusz | `.scrim > .sheet[role=dialog]` | pułapka fokusu, Esc zamyka, fokus wraca do wywołującego |
| Menu | `.menu[role=menu]` | strzałki góra/dół, Esc, `transform-origin` od wywołującego |
| Karta wyboru | `.card[role=radio]` | wybrana: tło `--accent-soft` + obrys Fosfor |
| Kafel źródła | `.tile[role=radio]` | miniatura 16:9, wybrany: obrys Fosfor + znacznik ✓ |
| Wykres | `.chart > svg.spark` | 60 próbek (1/s), linia Fosfor, pole `--chart-area`, linia celu przerywana |
| Ścieżka sygnału | `.hops > .hop` (+ `.major`) | etapy z czasem w ms. `.major` oznacza etap dominujący. Pasek budżetu `.budget-bar`. |
| Telewizor | `.tv > .glow + .tv-frame > .screen > .layer` | warstwy matrycy przełączane stanem, poświata za ramką |

## 5. Ekrany i stany

Stan aplikacji to atrybuty na `.app`:

| Atrybut | Wartości |
|---|---|
| `data-screen` | `discovery` · `notfound` · `work` · `settings` · `tray` |
| `data-disc` | `scanning` · `found` |
| `data-mode` | `ekran` · `media` |
| `data-session` | `idle` · `permission` · `connecting` · `live` · `lost` |
| `data-media` | `empty` · `drag` · `playing` (+ `data-paused`) |
| `data-details` | `open` · `closed` (telemetria na ekranie Na żywo) |

| Stan | Co widzi użytkownik |
|---|---|
| Odkrywanie | radar wokół znaku marki. Lista TV z modelem, IP i możliwościami. „Dodaj ręcznie (IP)”. |
| Nie widzę telewizora | znak `> ! <`, trzy rzeczy do sprawdzenia, „Szukaj ponownie”, pole IP z podpowiedzią |
| Ekran, gotowy | przygaszony, odbarwiony podgląd z etykietą „telewizor jeszcze nic nie pokazuje”. Szyna: źródło, tryb, dźwięk, akcja główna. |
| Wybór źródła | arkusz z kartami Monitory / Okna, miniatury, „Pokaż kursor myszy” |
| Zgoda na TV | na matrycy makieta okna zgody Tizen. Pod spodem bursztynowy komunikat z odliczaniem i „Anuluj”. |
| Łączenie | na matrycy animacja marki (romb przelatuje między szewronami) |
| Na żywo | matryca się „włącza”, wokół ramki poświata. Pod spodem cztery statystyki i protokół, a „Szczegóły” rozwijają telemetrię. |
| Na żywo: szczegóły | telewizor maleje, statystyki układają się 2×2 obok. Pod spodem wykresy opóźnienia i przepływności oraz ścieżka sygnału PC → NVENC → Wi-Fi → TV z czasami i paskiem budżetu. |
| Utracono połączenie | matryca zamrożona w szarości, animacja łączenia z bursztynowym rombem, „Spróbuj teraz” |
| Multimedia, pusto | matryca jako strefa upuszczania, adres URL pod spodem. W szynie „Ostatnio odtwarzane”. |
| Multimedia, upuszczanie | obrys strefy i poświata w Fosforze |
| Multimedia, odtwarzanie | kadr filmu z poświatą, tytuł, oś czasu, transport, napisy i ścieżka audio. W szynie kolejka. |
| Ustawienia | Ogólne · Obraz (profile) · Dźwięk · Zaawansowane (protokół: Automatycznie / Google Cast / DLNA / Przeglądarka TV) |
| Zasobnik | natywne menu AppIndicator (tekst), ikony stanów z marki w 16/22/24 px |

### Dane na żywo: natywny mirroring Google Cast

Opóźnienie od ekranu do obrazu na TV to suma etapów. Bufor odtwarzania odbiornika Cast w telewizorze dominuje
i nie zależy od AirLinx, dlatego UI mówi o nim wprost.

| Profil | Obraz | Kodek | Przechwyt. | NVENC | Wi-Fi | TV (bufor + dekod.) | Razem | Przepływność |
|---|---|---|---|---|---|---|---|---|
| Gra / praca | 1080p · 60 kl./s | H.264 | 6 ms | 5 ms | 9 ms | 88–95 ms | ~110 ms | ~11 Mb/s |
| Zrównoważony | 1440p · 60 kl./s | H.264 | 6 ms | 7 ms | 11 ms | 98–105 ms | ~125 ms | ~17 Mb/s |
| Kino | 4K · 30 kl./s | H.265 | 7 ms | 9 ms | 14 ms | 112–120 ms | ~150 ms | ~26 Mb/s |

Te wartości to szacunki projektowe do kalibracji w spike'u `spikes/cast_streaming`. Etykiety w UI mówią
„około” i „szacunkowo”.

## 6. Ruch

| Ruch | Czas / krzywa | Cel |
|---|---|---|
| Hover, naciśnięcie | 140 ms `--ease-out`, `scale(.97)` | informacja zwrotna |
| Przełącznik, segment | 220–240 ms `--ease-spring` | pokazanie zmiany stanu |
| Wskaźnik trybu Ekran/Multimedia | 320 ms sprężyna (transform + width) | ciągłość przestrzenna |
| Arkusz, menu | wejście 260–340 ms sprężyna. Wyjście 140–220 ms, krótsze i subtelniejsze. | orientacja |
| Radar odkrywania | pętla 3 s (4,5 s po znalezieniu TV), tylko na tym ekranie | aktywność w tle |
| Wejście znalezionych TV | 500 ms, opóźnienie 120 ms, opacity + 8 px + blur 4 px | pojawienie się nowej treści |
| Łączenie | animacja marki, 1,6 s `--ease-io` | jedyna pętla w trakcie połączenia |
| Włączenie matrycy | 900 ms: jasność 1,6 → 1, skala 1,012 → 1. Poświata wchodzi po 250 ms przez 1,1 s. | moment „telewizor gra” |
| Panel „Szczegóły” | 340 ms opacity + translateY | pokazanie nowej treści |

Animujemy wyłącznie `transform`, `opacity` i `filter`. Romb tally podczas transmisji stoi (BRAND.md §8).
Akcje z klawiatury nie mają dodatkowych animacji. Liczby na żywo zmieniają się bez animacji (cyfry stałej szerokości).

`prefers-reduced-motion: reduce`: wszystkie przejścia i animacje skracamy do 0. Radar staje się statycznymi
pierścieniami, a w animacji łączenia romb tylko powoli zmienia przezroczystość (2,4 s), zgodnie z marką.

## 7. Dostępność

- **Klawiatura:** pełna obsługa. Tab przechodzi w kolejności wizualnej. Strzałki działają w segmentach,
  kartach, zakładkach, kafelkach i menu. Esc zamyka menu, arkusz i ustawienia. Spacja to pauza w Multimediach.
  Arkusz ma pułapkę fokusu i oddaje fokus elementowi, który go otworzył.
- **Fokus:** `outline: 2px solid var(--focus)` z odstępem 2 px na każdej kontrolce. Pozycje menu mają wewnętrzny obrys.
- **ARIA:** `radiogroup`/`radio`, `switch`, `tablist`/`tab`/`tabpanel`, `dialog` + `aria-modal`, `menu`/`menuitemradio`,
  `aria-expanded`/`aria-controls` przy rozwijaniu. Komunikaty: `role="status"` (zgoda) i `role="alert"` (utrata połączenia).
  Wykresy mają `role="img"` z opisem. Ikony bez tekstu mają `aria-label`.
- **Kontrast (WCAG 2.x, sprawdzony skryptem dla obu motywów):**

| Para | Ciemny | Jasny |
|---|---|---|
| text / bg | 18,8 | 17,2 |
| text-2 / bg | 8,2 | 6,5 |
| text-2 / fill-2 | 7,3 | 5,6 |
| text-3 / bg | 5,1 | 5,7 |
| text-3 / fill-2 | 4,5 | 5,0 |
| on-accent / accent (akcja główna) | 12,9 | 5,4 |
| accent-text / accent-soft (znacznik „Zalecane”) | 11,8 | 5,5 |
| error-text / error-soft (Zatrzymaj) | 7,2 | 5,4 |
| warning / warning-soft | 9,6 | 4,9 |
| focus / bg (UI ≥ 3:1) | 15,6 | 4,8 |
| tekst na matrycy TV (biel 85% / 55% na czerni) | 13,3 / 5,7 | jak w ciemnym |

- Stan nigdy nie jest przekazywany samym kolorem: kropka/romb ma zawsze obok etykietę tekstową, a błędy mają ikonę i tytuł.
- Minimalny rozmiar celu to 28 px (ikony w liście TV), a typowo 32–54 px.

## 8. Głos i copy

Najpierw stan, potem liczba, potem działanie. Forma bezosobowa lub 1. osoba bez rodzaju gramatycznego
(„Szukam telewizorów…”, „Łączę ponownie…”, „Nie widzę telewizora”). Nazwy protokołów pojawiają się tylko
w linii protokołu, w panelu „Szczegóły” i w ustawieniach Zaawansowanych. Akcja ma tę samą nazwę w całym
przepływie: „Udostępnij ekran”, a później „Zatrzymaj”.

## 9. Wpięcie w aplikację (WebKit2GTK)

1. Skopiuj `brand-tokens.css`, `tokens.css`, `components.css`, `app.css` i `assets/` do zasobów aplikacji.
   Panel `.dev` i `shoot.py` służą tylko makiecie.
2. Motyw: ustaw `document.documentElement.dataset.theme` na `dark` lub `light` z `Gtk.Settings:gtk-application-prefer-dark-theme`
   lub z ustawienia „Wygląd”.
3. Pasek okna: makieta rysuje pasek CSD. W GTK przeciąganie okna trzeba wywołać z JS przez komunikat do hosta
   (`webkit.messageHandlers…` → `Gtk.Window.begin_move_drag`), bo `-webkit-app-region` nie działa w WebKit2GTK.
   Można też użyć natywnego `GtkHeaderBar` i ukryć `.bar`.
4. Telemetria: host wysyła co 1 s `{latency_ms, fps, bitrate_mbps, stages_ms[4]}`. Kod widoku przesuwa okno
   60 próbek i przerysowuje `svg.spark`, tak jak `renderDetails()` w `app.js`.
5. Tray: ikony z `assets/tray/` instaluje `docs/brand/final/icons/install-icons.sh`. Menu AppIndicator jest natywne, a nie HTML.
