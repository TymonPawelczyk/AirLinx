# AirLinx: zasady marki (kierunek C „Styk”)

> Wersja 1.0 · źródło wszystkich plików: `docs/brand/_build/` (`final.py` → `kit_render.py`).
> Podgląd całości: `final/preview.html` · `final/shots/kit-preview.png`.

## 1. Idea

Znak to litera „x” z AirLinx rozcięta na dwa szewrony zwrócone do siebie: komputer i telewizor.
W szczelinie między nimi przelatuje **romb**, czyli pojedyncza klatka obrazu. Z tej idei wynika cały system
stanów: `> <` bezczynność, `> ◆ <` transmisja, `> ! <` błąd. Hasło: **Obraz w locie.**

Nazwa produktu w tekście to **AirLinx** (wielkie A i L). Logotyp jest zapisany małymi literami (`airlinx`), ale
to wyłącznie forma graficzna, więc w zdaniach nie piszemy „airlinx”.

## 2. Pliki

| Co | Gdzie |
|---|---|
| Znak (czarny, biały, kolor na ciemne, kolor na jasne) | `logo/airlinx-symbol-{black,white,dark,light}.svg` |
| Znak do małych rozmiarów (32–48 px) | `logo/airlinx-symbol-small-black.svg` |
| Logotyp | `logo/airlinx-wordmark-{black,white,dark,light}.svg` |
| Wersja pozioma / pionowa | `logo/airlinx-lockup-{horizontal,vertical}-{black,white,dark,light}.svg` |
| Pole ochronne (konstrukcja) | `logo/construction-clearspace-{horizontal,vertical}.svg` |
| Ikona aplikacji | `icons/hicolor/scalable/apps/airlinx.svg` + `icons/hicolor/NxN/apps/airlinx.png` (16, 22, 24, 32, 48, 64, 128, 256, 512) |
| Ikony tray (symbolic) | `icons/hicolor/{16x16,22x22,24x24,scalable}/status/airlinx-{idle,streaming,error}-symbolic.svg` |
| Znak symbolic aplikacji | `icons/hicolor/symbolic/apps/airlinx-symbolic.svg` |
| Źródła ikon (master, small cut, wersje dopasowane do pikseli) | `icons/src/` |
| Instalacja ikon i przykład .desktop | `icons/install-icons.sh`, `icons/airlinx.desktop` |
| Ekran powitalny TV | `splash/tv-splash.html` (`?tv=Salon&state=connecting\|ready\|waiting-permission\|lost`) |
| Animacja „łączenie” | `motion/airlinx-connecting.svg` (ciemny), `motion/airlinx-connecting-light.svg`, demo `motion/connecting-demo.html` |
| Tokeny | `tokens.json`, `tokens.css` |
| Fonty (OFL 1.1) do paczki i TV | `fonts/` |

Wszystkie znaki są ścieżkami SVG: bez `<text>`, masek, filtrów i rastrów.

## 3. Znak: korekty optyczne (względem rundy 1)

- Romb jest powiększony o 3,5%, bo pełny kształt zakończony szpicami wygląda na mniejszy niż jego pudełko.
- Szpice szewronów są stępione o 6% grubości kreski, żeby nie wyglądały, jakby przebijały szczelinę, i pozostały czyste po antyaliasingu.
- Wersje jasne na ciemnym (`white`, `dark`) mają kreskę cieńszą o 3% (kompensacja irradiacji).
- W logotypie romby-kropki nad „i” mają ok. 1,2× pole oryginalnej kropki i siedzą w osi trzonu. Tracking wynosi −4%, a pary „rl” i „nx” są rozsunięte o 1%.
- Ikony 16/22/24 px są rysowane na siatce pikseli: skos 2:1 (63,4° zamiast 60°) daje ostre schodki, kreski mają 2 / 2,5 / 3 px w poziomie, a romb ma 3×6 px w 16 px.

## 4. Pole ochronne i minimalne rozmiary

**X = szerokość rombu.** Wokół każdej wersji logo trzymamy co najmniej **2X** wolnego miejsca (zob. `construction-clearspace-*.svg`).
Odstęp znak–logotyp wynosi 1,25X w wersji poziomej i 1,5X w pionowej. Tych proporcji nie zmieniamy.

| Wersja | Minimum (ekran) |
|---|---|
| Znak | 16 px (≤ 48 px: wersja small / dopasowana do pikseli) |
| Wersja pozioma | 20 px wysokości |
| Wersja pionowa | 56 px wysokości |
| Sam logotyp | 12 px wysokości x-height ≈ 7 px |

## 5. Kolor

Paleta „Fosfor” jest dark-first. Neutrale są wspólne z UI „OLED Cinema” (czysta czerń, ciepła biel `#F3F2EF`).

| Rola | Ciemny | Jasny | Zastosowanie |
|---|---|---|---|
| `bg` | `#000000` | `#F2F2F0` | tło, ekran TV |
| `surface` | `#0D0D0D` | `#FFFFFF` | arkusze, menu |
| `text` | `#F3F2EF` | `#0E0E0D` | tekst, szewrony znaku |
| `text-2` | `#A3A29E` | `#575754` | etykiety |
| `accent` (Fosfor) | `#3DF2A0` | `#007A47` | romb, transmisja, akcja główna |
| `accent-text` | `#3DF2A0` | `#007A47` | tekst w kolorze akcentu |
| `warning` (bursztyn) | `#F5B544` | `#8A5300` | uwaga, zgoda na TV |
| `error` | `#FF7A6B` | `#C2392B` | błąd, utrata sygnału |
| `success` | `#86D69A` | `#1F8F43` | połączono |

Wszystkie pary w `tokens.json → contrast` przechodzą WCAG 2.x: tekst ≥ 4,5:1, elementy UI ≥ 3:1.

**Kolory znaku:** szewrony w kolorze `text` (lub czarne/białe), romb **zawsze** w akcencie, chyba że cały znak
jest jednokolorowy. Fosfor jest wyłącznie kolorem rombu i aktywnego stanu. Nie zalewamy nim dużych powierzchni.

**Połączenie z UI:** `tokens.css` zawiera aliasy nazw z wariantu „OLED Cinema” (`--bg`, `--ink`, `--accent`, `--ok`,
`--danger`…). Bursztyn z UI zostaje jako `warning`, a akcentem marki jest Fosfor.

## 6. Typografia

| Krój | Rola | Licencja |
|---|---|---|
| **JetBrains Mono** ExtraBold 800 / Medium 500 | logotyp, tytuły na TV, wszystkie liczby (ms, kl./s, Mb/s, IP) | SIL OFL 1.1 |
| **Inter** Regular 400 / Medium 500 / SemiBold 600 | tekst interfejsu i opisy | SIL OFL 1.1 |

Oba kroje są zainstalowane w Zorin OS 17. Pliki do dołączenia (np. do odbiornika w przeglądarce TV) i teksty
licencji leżą w `fonts/`. Tracking: tytuły mono −0,03 do −0,04em, etykiety wersalikami +0,12em.

## 7. Ikony

- **Ikona aplikacji:** czarny kafel (zaokrąglenie ok. 22%), białe szewrony i romb Fosfor. Nazwa ikony `airlinx`.
- **Tray (AppIndicator):** `airlinx-idle-symbolic`, `airlinx-streaming-symbolic`, `airlinx-error-symbolic`.
  Jeden kolor `#2e3436`, przekolorowywany przez GNOME Shell. Stan rozpoznaje się po środku znaku, a szkielet się nie zmienia.
- Instalacja (użytkownik): `bash docs/brand/final/icons/install-icons.sh`, a w kodzie: `Icon=airlinx` w pliku `.desktop`
  oraz `indicator.set_icon_full("airlinx-streaming-symbolic", "Transmisja")`.

## 8. Ruch

- **Łączenie:** romb wylatuje z lewego szewronu i znika w prawym, a szewrony delikatnie „podają” klatkę.
  Pętla 1,6 s, `cubic-bezier(.65,0,.35,1)`.
- **`prefers-reduced-motion: reduce`:** nic się nie przesuwa, romb tylko powoli pulsuje (opacity, 2,4 s).
- Wejście ekranu TV: 0,9 s fade + 1,5 vh w górę, `cubic-bezier(.22,1,.36,1)`. Przy reduced motion bez animacji.
- Animacja jest tylko dla stanu „łączenie”. Podczas transmisji romb stoi.

## 9. Głos marki

Mówimy jak dobry panel diagnostyczny: **najpierw stan, potem liczba, potem działanie.** Zwięźle, ale nie szorstko.
Liczby zamiast przymiotników. Nie obiecujemy niczego, czego nie da się zmierzyć. W statusach używamy kropki
środkowej (·) zamiast przecinków. Unikamy form zależnych od rodzaju gramatycznego („Straciłem…”). Używamy czasu
teraźniejszego i bezosobowego („Łączę ponownie…”, „Utracono sygnał”). Nazw protokołów używamy tylko w „Zaawansowanych”.

Przykłady:
- „Skanowanie sieci… 2 urządzenia”
- „Na żywo · 34 ms · 60 kl./s · 18 Mb/s”
- „Wymagana zgoda na TV: Salon”
- „Utracono sygnał. Ponawianie 2/5…”
- „Upuść pliki. 4K HDR, jeśli TV obsługuje.”
- „Tryb gry: najniższe opóźnienie.”

## 10. Czego unikać

- Nie obracać, nie pochylać, nie rozciągać znaku i nie zmieniać kąta szewronów (poza siatką 16–24 px).
- Nie zamieniać rombu na kółko, kwadrat ani inny kształt. Nie zmieniać jego koloru na inny niż akcent (lub jednolity kolor znaku).
- Nie stawiać logo bez pola 2X ani na zdjęciu bez przyciemnienia.
- Nie dodawać cieni, poświaty, gradientów ani obrysu do znaku (gradient jest dozwolony tylko na kaflu ikony).
- Nie składać logotypu fontem na żywo, bo pliki logotypu to ścieżki z korektami.
- Nie używać fal Wi-Fi, trójkąta pod ekranem ani łuków w rogu ekranu, bo to dystans od AirPlay, Google Cast, Miracast i Samsunga.
- Nie pisać nazwy jako „Airlinx”, „AirLynx” ani „AIRLINX” w tekście ciągłym.

## 11. Zastrzeżenia

Przed publicznym wydaniem trzeba zrobić wyszukiwanie znaków towarowych (EUIPO/UPRP) i wyszukiwanie obrazem. Ten dokument
nie jest opinią prawną.
