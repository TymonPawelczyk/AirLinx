# AirLinx: copy landing page'a

Diagnoza: **A** (pusty brief, brak istniejącego tekstu strony). Kąt i fakty zbudowane z BRAND.md, README, ARCHITECTURE, BRIEF, kodu w `airlinx/` i `ui/` oraz pomiarów na Samsung S85F z 2026-11-10. Głos: „dobry panel diagnostyczny” z BRAND.md §9: najpierw stan, potem liczba, potem działanie. Pisze ktoś, kto zbudował to pod swój pulpit i swój telewizor, zna liczby i mówi, czego nie sprawdził.

Konwencje dla kolejnego agenta (HTML):
- Nazwa w tekście zawsze **AirLinx**. Logotyp `airlinx` tylko jako grafika (`docs/brand/final/logo/`).
- Liczby, jednostki i adresy składamy JetBrains Mono (BRAND.md §6), tekst ciągły Inter.
- W statusach i paskach liczb separator to kropka środkowa ` · `.
- Na stronie nie ma półpauz ani myślników jako ozdobników. Zostaw je tak.
- Fosfor (`#3DF2A0`) tylko na romb, stan „na żywo” i główny przycisk. Nie zalewać nim teł (BRAND.md §5).
- Bez fal Wi-Fi, trójkątów pod ekranem i łuków w rogu ekranu w ilustracjach (BRAND.md §10).
- Adres repozytorium: `https://github.com/TymonPawelczyk/AirLinx` (atrybut `data-repo-url` w `site/index.html`).

---

## 0. Meta

**Tytuł strony (`<title>`):**
AirLinx · ekran i filmy z Linuksa na telewizor

**Meta description (154 znaki):**
AirLinx wysyła ekran, dźwięk i pliki z Linuksa (GNOME, X11) na telewizor z Google Cast lub DLNA. Zmierzone na Samsung S85F: 1080p, 60 kl./s, 111 ms.

**Open Graph (opcjonalnie):**
- `og:title`: AirLinx · Obraz w locie.
- `og:description`: Pulpit, okno albo film z Linuksa na telewizorze w sieci domowej. Na telewizorze nic nie instalujesz.

---

## 1. Hero

**Asset:** `ui/shots/live.png` (stan „Na żywo” z liczbami) w ramce okna. Alternatywa: `ui/shots/live-details.png` przycięty do górnej połowy. Nad nagłówkiem znak `❯◆❮` w wersji `airlinx-symbol-dark.svg`.

**Eyebrow (mono, wersaliki +0,12em):**
OBRAZ W LOCIE.

**Nagłówek (H1):**
Ekran z Linuksa na telewizorze. Kabel HDMI zostaje w szufladzie.

**Podtytuł:**
AirLinx wysyła cały monitor albo jedno okno, razem z dźwiękiem, na telewizor z Google Cast w Twojej sieci domowej. Filmy, muzykę i zdjęcia z dysku odtwarza na telewizorze, a na samym telewizorze nic nie instalujesz.

**CTA główne:** Zainstaluj AirLinx → `#instalacja`
**CTA drugie:** Kod źródłowy → `https://github.com/TymonPawelczyk/AirLinx`

**Mikrocopy pod CTA:**
GNOME na X11 · instalacja do katalogu domowego, bez sudo · Google Cast, zapasowo DLNA

### Alternatywy nagłówka
- A (wybrany): „Ekran z Linuksa na telewizorze. Kabel HDMI zostaje w szufladzie.” Uzasadnienie: pierwsze zdanie mówi, co to jest i dla kogo (Linux). Drugie nazywa konkretną niedogodność, którą odbiorca zna, i jest jedynym żartem w hero.
- B: „Pulpit z Linuksa na telewizorze w 111 ms.” Uzasadnienie: zgodne z zasadą „liczby zamiast przymiotników”. Ryzyko: 111 ms bez kontekstu brzmi dla części graczy jak dużo, a wyjaśnienie (90 ms to sam TV) nie zmieści się w hero.
- C: „Telewizor jako drugi ekran Linuksa, po sieci domowej.” Uzasadnienie: najspokojniejszy wariant. Słabszy, bo „drugi ekran” sugeruje rozszerzenie pulpitu, a AirLinx robi kopię monitora lub okna.

### Alternatywy CTA
- „Zainstaluj AirLinx” (wybrany): mówi, co się stanie, i prowadzi do bloków kodu.
- „Przejdź do instalacji”: uczciwsze, bo przycisk nie instaluje niczego sam. Dobre, jeśli testy pokażą, że ludzie oczekują pobrania pliku.
- Drugie CTA zamiennie: „Jak to działa” → `#jak-to-dziala`, jeśli `data-repo-url` nie zawiera adresu http(s).

**Adnotacja:** nazwy protokołów (Google Cast, DLNA) są w podtytule i mikrocopy mimo zasady BRAND.md §9 („tylko w Zaawansowanych”). Ta zasada dotyczy UI. Na stronie odbiorca musi w 5 sekund sprawdzić, czy jego telewizor się nadaje, a „Google Cast” to jedyne słowo, które to rozstrzyga.

---

## 2. Pasek liczb

**Asset:** brak zrzutu. Cztery kafle z liczbą w mono, etykietą pod spodem. Romb Fosfor jako znacznik przy pierwszej liczbie.

| Liczba | Etykieta | Dopisek (mały, `text-2`) |
|---|---|---|
| **111 ms** | od ekranu do obrazu na TV | ok. 90 ms z tego to przetwarzanie w samym telewizorze |
| **60 kl./s** | stabilnie, w 1080p | 0 utraconych klatek w pomiarze |
| **12 Mb/s** | przepływność obrazu | RTT sieci ok. 3 ms |
| **240 ms** | zmiana jakości w trakcie | sesja trwa dalej, restartuje się tylko koder |

**Przypis pod paskiem:**
Zmierzone na Samsung S85F w sieci domowej, profil „Gra / praca”, dźwięk Opus 128 kb/s włączony.

**Adnotacja:** cztery liczby, wszystkie z pomiaru, każda z dopiskiem, który mówi, skąd się bierze. Dopisek przy 111 ms jest ważny: uprzedza zarzut „za wolno” i przerzuca go tam, gdzie leży (bufor odbiornika w TV), co zgadza się z panelem „Szczegóły” w aplikacji.

---

## 3. Jak to działa

**ID sekcji:** `jak-to-dziala`
**Nagłówek:** Trzy kroki od uruchomienia do obrazu

**Krok 1 · Znajdź telewizor**
AirLinx przeszukuje sieć lokalną i pokazuje telewizory z nazwą i modelem. Jeśli któryś się nie zgłasza, wpisujesz jego adres IP ręcznie.
*Asset:* `ui/shots/discovery.png`

**Krok 2 · Wybierz, co pokazać**
Cały monitor albo jedno okno, z miniaturami podglądu. Przy dwóch monitorach wybierasz konkretny.
*Asset:* `ui/shots/source-picker-windows.png`

**Krok 3 · Udostępnij ekran**
Jedno kliknięcie. Obraz pojawia się na telewizorze po 2,4–5,7 s, z czego większość to uruchamianie odbiornika w telewizorze. Przy pierwszym połączeniu telewizor może poprosić o zgodę na pilocie.
*Asset:* `ui/shots/connecting.png` albo animacja `docs/brand/final/motion/airlinx-connecting.svg`

**Adnotacja:** krok 3 podaje prawdziwy, dość długi czas startu zamiast obiecywać „natychmiast”. Na liście „Odrzucone” jest wyjaśnienie, dlaczego nie piszemy „w sekundę”.

---

## 4. Funkcje

Sekcje celowo mają różną długość. Ekran i multimedia to rdzeń produktu, zasobnik to dodatek.

### 4.1 Ekran (najdłuższy blok)
**Asset:** `ui/shots/live-details.png`

**Nagłówek:** Monitor albo jedno okno, z liczbami na żywo

AirLinx przechwytuje wybrany monitor albo okno i wysyła je do telewizora jako kopię ekranu Google Cast. Kursor myszy jest widoczny. Kiedy przesuniesz udostępniane okno, obraz na telewizorze podąża za nim. Źródło zmieniasz w trakcie, bez rozłączania.

Podczas transmisji okno pokazuje to samo, co widzi diagnostyka: opóźnienie, kl./s, przepływność, rozdzielczość i RTT sieci, odświeżane dwa razy na sekundę. Panel „Szczegóły” rozkłada opóźnienie na etapy: przechwytywanie, koder, sieć, telewizor. Kiedy obraz się spóźnia, widać, czyja to wina.

Gdy połączenie zerwie się w trakcie, AirLinx ponawia je trzy razy, po 1, 2 i 4 s.

### 4.2 Jakość
**Asset:** `ui/shots/settings-quality.png` (zrzut zaktualizowany: wersja 0.1.0, liczba opóźnienia tylko przy profilu Gra / praca, profile Balans i Kino oznaczone jako eksperymentalne)

**Nagłówek:** Trzy profile, zmiana bez zrywania sesji

| Profil | Obraz | Przepływność | Do czego |
|---|---|---|---|
| Gra / praca | 1080p · 60 kl./s | 12 Mb/s | pisanie, prezentacje, spokojne gry · **111 ms zmierzone** |
| Balans | 1440p · 60 kl./s | 18 Mb/s | Eksperymentalny. Opóźnienia na telewizorze jeszcze nie zmierzyliśmy. |
| Kino | 4K · 30 kl./s | do 40 Mb/s | Eksperymentalny. AirLinx prosi telewizor o bufor co najmniej 400 ms, więc opóźnienie jest wyraźnie wyższe. Nie zmierzyliśmy go. |

Profil przełączasz w panelu obok podglądu. AirLinx restartuje wtedy tylko koder wideo (240 ms w pomiarze), a połączenie z telewizorem zostaje. Przepływność możesz ustawić ręcznie od 6 do 40 Mb/s, co pomaga przy słabym Wi-Fi.

Obraz koduje karta NVIDIA przez NVENC. Bez niej AirLinx przełącza się na koder procesorowy (libx264).

### 4.3 Dźwięk
**Asset:** fragment `ui/shots/main.png` z przełącznikiem „Dźwięk na TV”

**Nagłówek:** Dźwięk komputera z głośników telewizora

AirLinx nagrywa to, co gra na wyjściu systemowym (PipeWire albo PulseAudio), i wysyła to jako Opus 128 kb/s w tym samym strumieniu co obraz. Jeden przełącznik „Dźwięk na TV” wyłącza dźwięk, gdy wolisz zostać przy słuchawkach.

### 4.4 Multimedia
**Asset:** `ui/shots/media.png` albo `ui/shots/media-subtitles.png` (uwaga na prawdziwe tytuły na zrzucie, patrz „Odrzucone”)

**Nagłówek:** Filmy z dysku w jakości pliku

Przeciągnij pliki na okno albo dodaj je przyciskiem. Wideo, muzyka i zdjęcia trafiają do kolejki i grają po kolei. Na telewizor wysyłasz też adres URL.

Zanim plik poleci, AirLinx sprawdza go przez ffprobe i wybiera najlżejszą drogę:
- telewizor zna format: plik idzie bez zmian, z przewijaniem;
- telewizor zna kodeki, ale nie kontener: AirLinx przepakowuje plik w locie, bez ponownego kodowania. Tak trafia na Google Cast plik MKV;
- telewizor nie zna kodeka: AirLinx koduje strumień na bieżąco (NVENC albo procesor).

Napisy tekstowe (SRT, ASS, WebVTT) działają z pliku obok filmu i ze ścieżek wewnątrz MKV. Ścieżkę wybierasz w trakcie odtwarzania. Pauza, przewijanie, głośność telewizora i następna pozycja są w oknie.

Sprawdzone na Samsung S85F: pliki MKV i MP4 przez Google Cast i przez DLNA.

### 4.5 Zasobnik
**Asset:** `ui/shots/tray-icons.png` albo ikony z `docs/brand/final/icons/hicolor/scalable/status/`

**Nagłówek:** Działa w tle, z ikoną w zasobniku

Zamknięcie okna chowa AirLinx do zasobnika, a udostępnianie trwa dalej. Z menu ikony zaczynasz i kończysz udostępnianie albo otwierasz okno. „Zakończ” zatrzymuje transmisję na telewizorze, zanim program się zamknie. AirLinx może startować razem z systemem, od razu schowany w zasobniku.

### 4.6 Bezpieczeństwo
**Asset:** `ui/shots/tv-busy.png` (pytanie przed przerwaniem tego, co leci na TV)

**Nagłówek:** Łączy się tylko z telewizorem, który potwierdzi tożsamość

Przy połączeniu AirLinx prosi telewizor o podpis certyfikatem urządzenia Cast i sprawdza, czy łańcuch certyfikatów prowadzi do głównego urzędu Google Cast. Tożsamość telewizora trafia do pliku `~/.config/airlinx/known_tvs.json` (uprawnienia 0600). Jeśli przy kolejnym połączeniu tożsamość się nie zgadza, AirLinx przerywa i pyta, czy zaufać temu telewizorowi ponownie. Na Samsung S85F weryfikacja przechodzi.

Kopia ekranu przez Google Cast jest szyfrowana AES-128, jak wymaga protokół Cast. Pliki i kopię ekranu przez DLNA telewizor pobiera z komputera zwykłym HTTP, bez szyfrowania, pod adresami z losowym tokenem, więc używaj AirLinx w sieci, której ufasz. Jeśli na telewizorze coś już leci, AirLinx najpierw pyta, czy to przerwać.

Konto nie jest potrzebne. Obraz, dźwięk i pliki z dysku idą wyłącznie między komputerem a telewizorem w Twojej sieci.

---

## 5. Stany znaku

**Asset:** trzy duże znaki obok siebie: `❯ ❮`, `❯◆❮`, `❯!❮` (pliki `airlinx-idle/streaming/error-symbolic.svg`), plus animacja `motion/airlinx-connecting.svg` przy pierwszym. Przy `prefers-reduced-motion` romb tylko pulsuje (BRAND.md §8).

**Nagłówek:** Ikona mówi, co się dzieje

Znak AirLinx to litera „x” rozcięta na dwa szewrony: komputer i telewizor. Romb między nimi to jedna klatka obrazu.

- `❯ ❮` **Gotowy.** Szczelina pusta, nic nie leci.
- `❯◆❮` **Na żywo.** Klatka jest w drodze.
- `❯!❮` **Błąd.** Coś stanęło w szczelinie. Okno mówi co, na przykład: „Utracono połączenie z telewizorem.”

Ta sama ikona siedzi w zasobniku GNOME, więc stan transmisji widać bez otwierania okna.

**Adnotacja:** to jedyne miejsce z lżejszym tonem („coś stanęło w szczelinie”), bo brand sam zbudował tę metaforę. Komunikat jest dosłownie z kodu (`mirror.py:761`), a nie z przykładu w BRAND.md („Ponawianie 2/5…”), bo kod ponawia 3 razy i takiego licznika nie pokazuje.

---

## 6. Wymagania i zgodność

**ID sekcji:** `wymagania`
**Nagłówek:** Na czym to działa

**Komputer**
| | Stan |
|---|---|
| System | Linux z GNOME w sesji **X11**. Testowane na Zorin OS 17.3 (baza Ubuntu 22.04). |
| Wayland | Nieobsługiwany. Kopia ekranu korzysta z narzędzi X11 (x11grab, xrandr, xprop). Na ekranie logowania wybierz sesję Xorg. W sesji Wayland AirLinx pokazuje ostrzeżenie. |
| Karta graficzna | Zalecana NVIDIA z NVENC (testowane: RTX 3080). Bez NVENC kopia ekranu, przez Google Cast i przez DLNA, koduje na procesorze (libx264), czego nie mierzyliśmy. Karty AMD i Intel trafiają na ten sam koder procesorowy. |
| Dźwięk | PipeWire albo PulseAudio z `pactl`. |
| Zasobnik | GNOME z obsługą AppIndicator / StatusNotifierItem (w Zorin OS i Ubuntu jest domyślnie). |
| Python | Systemowy Python 3 z PyGObject (GTK 3, WebKit2GTK 4.1). Testowane: 3.10. |

**Telewizor**
| | Stan |
|---|---|
| Google Cast | Kopia ekranu, dźwięk i multimedia. Sprawdzone na Samsung S85F. Inne urządzenia z Google Cast nie były jeszcze testowane. |
| DLNA (zapasowo) | Filmy, muzyka i zdjęcia. Kopia ekranu przez DLNA jest strumieniem MPEG-TS z wyraźnie większym opóźnieniem (niezmierzonym). Pliki przez DLNA sprawdzone na Samsung S85F. |
| AirPlay, Miracast | Nieobsługiwane. |
| Na telewizorze | Nic nie instalujesz. AirLinx używa odbiorników wbudowanych w telewizor. |

**Sieć**
Komputer i telewizor w tej samej sieci lokalnej (Wi-Fi albo kabel). Zapora musi przepuszczać wykrywanie urządzeń (mDNS, UDP 5353 i SSDP, UDP 1900) oraz połączenia od telewizora do komputera, bo telewizor sam pobiera pliki z AirLinx.

**Adnotacja:** ta sekcja świadomie mówi, dla kogo AirLinx nie jest (Wayland, AirPlay, Miracast). To buduje zaufanie do liczb wyżej i oszczędza zgłoszeń.

---

## 7. Instalacja

**ID sekcji:** `instalacja`
**Nagłówek:** Instalacja w trzech poleceniach

**Wstęp:**
Instalator kopiuje AirLinx do `~/.local`, tworzy własne środowisko Pythona w `~/.local/share/airlinx/venv` i dodaje program do menu aplikacji. Sam instalator nie potrzebuje sudo. Pakiety systemowe instalujesz raz, przez apt.

**1. Pakiety systemowe** (nazwy dla Zorin OS 17 i Ubuntu 22.04)
```bash
sudo apt install git ffmpeg python3-gi python3-venv gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  x11-xserver-utils x11-utils pulseaudio-utils
```

**2. AirLinx**
```bash
git clone https://github.com/TymonPawelczyk/AirLinx
cd AirLinx
./install.sh
```

**Opcjonalnie: start razem z systemem, schowany w zasobniku**
```bash
./install.sh --autostart
```

**Uruchomienie**
```bash
airlinx               # okno + ikona w zasobniku
airlinx --no-window   # tylko zasobnik
```
Albo z menu aplikacji: „AirLinx”.

**Odinstalowanie**
```bash
./install.sh --uninstall
```
Ustawienia zostają w `~/.config/airlinx`.

**Mikrocopy pod blokami:**
Instalator pobiera biblioteki Pythona z PyPI (pychromecast, zeroconf, aiohttp, async-upnp-client, cryptography, protobuf), więc przy instalacji potrzebny jest internet. Jeśli `~/.local/bin` nie jest w `PATH`, instalator o tym powie.

**Adnotacja:** nagłówek mówi „trzy polecenia”, bo blok 2 to trzy polecenia; pakiety apt są osobnym krokiem przygotowawczym. Jeśli to razi, alternatywa: „Instalacja”.

---

## 8. FAQ

**ID sekcji:** `faq`
**Nagłówek:** Pytania

**Czy AirLinx działa na Waylandzie?**
Kopia ekranu nie, bo Wayland nie jest obsługiwany. AirLinx przechwytuje obraz narzędziami X11, więc potrzebujesz sesji Xorg (wybierasz ją na ekranie logowania). W sesji Wayland AirLinx pokazuje o tym ostrzeżenie. Odtwarzanie plików nie korzysta z przechwytywania ekranu, ale na Waylandzie go nie testowaliśmy.

**Czy nada się do gier?**
Do spokojnych tak. W profilu „Gra / praca” od ekranu do telewizora mija ok. 111 ms, z czego ok. 90 ms to przetwarzanie w samym telewizorze. Strategia, gry przygodowe czy granie na padzie z kanapy wchodzą w grę. Strzelanki sieciowe i gry rytmiczne lepiej zostawić na kablu HDMI.

**Czy potrzebuję internetu?**
Do instalacji tak (git i biblioteki z PyPI). Potem AirLinx rozmawia tylko z telewizorem w sieci lokalnej. Nie sprawdzaliśmy, czy telewizor uruchomi swoje odbiorniki Cast bez dostępu do internetu. Wyjątek: gdy dodasz do kolejki adres pliku z internetu, ten plik zostanie pobrany z serwera, który wskazałeś.

**Co AirLinx wysyła poza moją sieć?**
Nic o Tobie. W kodzie nie ma telemetrii, kont użytkowników ani połączeń z serwerami autora. Z internetem AirLinx łączy się tylko wtedy, gdy dodasz do kolejki adres pliku z sieci. Kopia ekranu przez Google Cast jest szyfrowana, a pliki i kopia ekranu przez DLNA idą w sieci lokalnej zwykłym HTTP pod adresami z losowym tokenem. Sam telewizor po uruchomieniu odbiornika Cast może łączyć się z serwerami Google. Ustawienia i zapamiętane telewizory leżą w `~/.config/airlinx`.

**Mam iPhone'a w głowie. Czy to AirPlay dla Linuksa?**
Robi to samo, czego oczekujesz od AirPlay: ekran i filmy na telewizorze, po sieci. Mówi przy tym protokołami Google Cast i DLNA, więc AirPlay i Miracast nie wchodzą w grę. Telewizor z Google Cast albo DLNA to właściwy odbiornik. Apple TV nim nie jest.

**Nie mam karty NVIDIA. Zadziała?**
Tak. Bez NVENC AirLinx sam przełącza się na koder procesorowy libx264, przy kopii ekranu przez Google Cast i przy zapasowej kopii przez DLNA. Nie mierzyliśmy, ile to kosztuje procesor i opóźnienie. Którego kodera AirLinx używa, widać w ustawieniach. Pliki, które trzeba przekodować, też zejdą na procesor.

**Czy muszę coś instalować na telewizorze?**
Nie. AirLinx korzysta z odbiorników, które telewizor ma wbudowane: aplikacji kopii ekranu Cast, odtwarzacza multimediów Cast albo odbiornika DLNA.

**Adnotacja do pytania o AirPlay:** celowo bez listy marek telewizorów z Google Cast. To byłoby uogólnienie rynkowe bez pokrycia w testach (patrz „Odrzucone”, pkt 3).

---

## 9. Stopka

**Asset:** `airlinx-lockup-horizontal-dark.svg` (pole ochronne 2X).

- Linia marki: **Obraz w locie.**
- Linki: Instalacja (`#instalacja`) · Wymagania (`#wymagania`) · Pytania (`#faq`) · Kod źródłowy (`https://github.com/TymonPawelczyk/AirLinx`)
- Status (mono, mały): `❯◆❮ Zmierzone na Samsung S85F · 1080p · 60 kl./s · 111 ms`
- Zastrzeżenie: AirLinx jest niezależnym projektem i nie jest powiązany z Google ani z Samsungiem. Google Cast jest znakiem towarowym Google LLC.

---

## Odrzucone twierdzenia

Każde poniżej zostało celowo pominięte. Przy każdym: czego brakuje, żeby je odblokować.

1. **„Kino: około 150 ms”, „Balans: około 125 ms”** (wartości z UI: `ui/index.html:397–399`, `ui/app.js:38–43`). Zmierzony jest tylko profil Gra / praca (111 ms). Kod przeczy wartości dla Kina: `effective_options()` w `airlinx/core/cast/mirror.py:84` ustawia dla profilu `cinema` docelowe opóźnienie odbiornika co najmniej 400 ms. *Odblokowanie:* pomiar obu profili na S85F. Przy okazji warto poprawić teksty w UI, bo strona i aplikacja nie powinny się rozjeżdżać.
2. **„4K HDR, jeśli TV obsługuje”** (przykład z BRAND.md §9). Kod przepuszcza HEVC bez przekodowania (`probe.py:362`), ale nikt nie sprawdził HDR ani 4K z pliku na TV. *Odblokowanie:* test pliku 4K HDR (HEVC 10-bit) przez Cast i DLNA na S85F.
3. **„Działa z każdym telewizorem z Google Cast / Chromecastem”.** Sprawdzony jest jeden telewizor. *Odblokowanie:* test na Chromecaście z Google TV i na jednym telewizorze innej marki.
4. **„Działa z prawie każdym telewizorem” (DLNA)** (tekst z karty DLNA w ustawieniach). Listy formatów w `probe.py` są pisane pod Samsung DMR. *Odblokowanie:* test na DLNA innego producenta (LG, Sony).
5. **Opóźnienie kopii ekranu przez DLNA i bez NVENC (libx264).** Niezmierzone, więc na stronie są tylko słowa „wyraźnie większe” (DLNA, bo to MPEG-TS przez bufor odtwarzacza) i „nie mierzyliśmy” (libx264). *Odblokowanie:* pomiar obu ścieżek.
6. **„Okno udostępniane nawet wtedy, gdy jest zasłonięte”.** Kopia okna to wycinek ekranu (`x11grab` z geometrią okna, `capture.py:build_video_cmd`), więc gdy inne okno je zasłoni, na telewizorze widać to, co je zasłania. Na stronie jest tylko to, co prawdziwe: obraz podąża za przesuwanym oknem. *Odblokowanie:* przechwytywanie konkretnego okna (np. Composite/XComposite), czego dziś nie ma.
7. **„Natychmiast” / „w sekundę” przy starcie.** Zmierzone 2,4–5,7 s. Na stronie jest liczba.
8. **„Bez internetu”** jako pełna obietnica. AirLinx nie łączy się z internetem (sprawdzone w kodzie: discovery i serwer plików działają w LAN, `lan_ip_for` tylko pyta jądro o trasę), ale odbiorniki Cast w telewizorze mogą potrzebować sieci. *Odblokowanie:* test z telewizorem odciętym od internetu.
9. **Szyfrowanie plików multimedialnych.** Serwer plików mówi zwykłym HTTP w LAN, zabezpieczony tylko losowym tokenem w adresie. Strona mówi wprost, że szyfrowana jest tylko kopia ekranu przez Google Cast (AES-128-CTR, `rtp.py`), a pliki i kopia ekranu przez DLNA idą zwykłym HTTP.
10. **Napisy obrazkowe (PGS z Blu-ray, DVB).** Wykrywane, ale nie dostarczane (`probe.py:19`). Strona mówi tylko o SRT, ASS i WebVTT.
11. **Napisy przez DLNA.** Kod oferuje SRT przez rozszerzenie Samsunga „best effort”, bez potwierdzenia na TV. Strona nie obiecuje napisów przez DLNA.
12. **Wsparcie dystrybucji innych niż Zorin/Ubuntu 22.04, KDE, Xfce.** Nietestowane. Nazwy pakietów apt podane tylko dla Zorin OS 17 / Ubuntu 22.04.
13. **„Przeglądarka TV (WebRTC)”** jako czwarta metoda. W UI oznaczona „Wkrótce”, niedostępna w tej wersji. Pominięta.
14. **Liczba pobrań, gwiazdki, opinie, licencja, numer wersji.** Brak publicznego repo i wydania. Na stronie nie ma social proof. *Odblokowanie:* publiczne repo z licencją i pierwszym tagiem.

## Niepewności do sprawdzenia przed publikacją

- **GPU.** *Rozwiązane w 0.1.0:* kopia ekranu przez DLNA ma fallback na libx264, a ustawienia pokazują faktyczny koder. Stan sprzed poprawki: kopia ekranu przez Cast ma automatyczny fallback NVENC → libx264 (`mirror.py:481–488`, `capture.probe_encoders`). Kopia ekranu przez DLNA **nie ma** automatycznego fallbacku: koder bierze się ze zmiennej `AIRLINX_DLNA_ENCODER`, domyślnie `h264_nvenc` (`controller.py:97`), więc bez NVIDII ta ścieżka się wywali. Dodatkowo ustawienia UI pokazują na sztywno „NVENC, wybierany automatycznie” (`ui/index.html`, `#encoder-info`), także na komputerze bez NVIDII. Nie ma obsługi VAAPI (AMD/Intel). Strona mówi o tym uczciwie, ale prościej byłoby dodać fallback w `DlnaMirrorAdapter` i wtedy usunąć zdanie o zmiennej z FAQ.
- **Wayland.** *Rozwiązane w 0.1.0:* aplikacja wykrywa sesję Wayland i pokazuje ostrzeżenie. Stan sprzed poprawki: w kodzie nie ma żadnego wykrywania sesji Wayland (`grep -i wayland` w `airlinx/` nic nie zwraca). Na Waylandzie AirLinx uruchomi się, a kopia ekranu przez XWayland najpewniej pokaże czarny lub niepełny obraz, bez czytelnego komunikatu. Strona pisze „nieobsługiwany”, co jest prawdą, ale warto dodać w aplikacji ostrzeżenie przy `XDG_SESSION_TYPE=wayland`. Odtwarzanie plików na Waylandzie jest nietestowane.
- **Liczba ponowień w komunikacie błędu.** BRAND.md podaje przykład „Ponawianie 2/5…”, a kod ponawia 3 razy (`RECONNECT_BACKOFF = (1.0, 2.0, 4.0)`), UI pokazuje „Ponawiam…” bez licznika. Na stronie użyty jest komunikat z kodu. Rozjazd BRAND.md ↔ kod do wyjaśnienia poza stroną.
- **Zrzuty.** `settings-quality.png` pokazuje wersję 0.1.0 i opóźnienie „~110 ms” tylko przy profilu Gra / praca (profile Balans i Kino są eksperymentalne, bez liczby). `media-subtitles.png` i `media.png` mają od 0.1.0 neutralne, fikcyjne nazwy plików.
- **Nazwy pakietów apt.** Wyprowadzone z `install.sh` i importów (`gi.require_version` Gtk 3.0 / WebKit2 4.1, `shutil.which("ffmpeg"/"pactl")`, wywołania `xrandr`, `xprop`, `xwininfo`). Nie uruchomiłem tej linii na czystej maszynie. Do sprawdzenia na świeżym Zorin/Ubuntu 22.04.
- **Zapora i serwer plików.** Serwer multimediów słucha na losowym porcie (`MediaServer()` bez portu → `port=0`), więc nie da się podać konkretnego portu do otwarcia w ufw. Strona mówi ogólnie o „połączeniach od telewizora do komputera”.
