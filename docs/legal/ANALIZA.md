# AirLinx: analiza prawna przed wydaniem v0.1.0

> **Uwaga.** Dokument przygotowany z pomocą AI (Claude). Nie stanowi porady prawnej. Przed publikacją zalecana weryfikacja przez prawnika (osobę wpisaną na listę adwokatów lub radców prawnych).
> Wersja 1.0, stan na 10 listopada 2026 r. Dotyczy kodu na gałęzi `develop` (commit `e14e3f0`).

Dokument wewnętrzny, ale trafia do publicznego repozytorium, dlatego nie zawiera danych osobowych autora. Autor występuje jako „TymonPawelczyk (GitHub)”. Projekt nie podaje adresu e-mail: kontakt idzie przez GitHub Issues, a zgłoszenia bezpieczeństwa przez GitHub Private Vulnerability Reporting.

---

## 1. Stan faktyczny (zweryfikowany w kodzie)

### 1.1. Projekt i dystrybucja

- AirLinx to darmowa aplikacja desktopowa na Linuksa (GNOME, X11). Autor jest osobą prywatną z Polski i działa niekomercyjnie: nie pobiera opłat, nie wyświetla reklam, nie zbiera danych.
- Dystrybucja: kod źródłowy w publicznym repozytorium `github.com/TymonPawelczyk/AirLinx`. Instalacja przez `git clone` i `./install.sh`, który tworzy lokalny venv i pobiera zależności z PyPI na komputerze użytkownika. Autor **nie rozpowszechnia** binarek ani paczek z zależnościami.
- Strona produktu (`site/`) jest statyczna, po polsku, ma być hostowana na GitHub Pages.
- **Ustalenie krytyczne:** w repozytorium nie ma pliku `LICENSE` ani `COPYING`. Nazwa licencji (GPL-3.0) nie pada w README, na stronie ani w nagłówkach plików. Zamiar licencjonowania na GPL-3.0 wynikał tylko z ustaleń zespołu. Zob. ryzyko R1. **Aktualizacja:** autor wybrał GPL-3.0-or-later. Plik `LICENSE` z niezmienionym tekstem GPL-3.0 (kopia `/usr/share/common-licenses/GPL-3`) i sekcja „License” w README zostały dodane na gałęzi `feature/legal`.

### 1.2. Co aplikacja zapisuje lokalnie

| Plik | Zawartość | Kod |
|---|---|---|
| `~/.config/airlinx/config.json` (lub `$XDG_CONFIG_HOME`) | ustawienia: protokół, ID ostatniego TV, ręcznie dodane adresy IP telewizorów, preferencje kopii ekranu, źródło dźwięku (nazwa mikrofonu, identyfikator i nazwa wybranej aplikacji), ostatnie źródło obrazu, głośność, preferencje UI | `airlinx/settings.py` |
| `~/.config/airlinx/config.json.bak` | kopia uszkodzonego pliku ustawień | `airlinx/settings.py` |
| `~/.config/airlinx/known_tvs.json` (0600, katalog 0700) | przypięte tożsamości telewizorów: temat i wystawca certyfikatu urządzenia Cast, skrót SHA-256 klucza, odcisk i daty certyfikatu TLS, data pierwszego połączenia | `airlinx/core/cast/castv2.py:473-580` |
| `$XDG_RUNTIME_DIR/airlinx/audio-routes.json` (bez `XDG_RUNTIME_DIR`: `~/.cache/airlinx/`) | dziennik przekierowań strumieni dźwięku w PulseAudio/PipeWire, żeby po awarii przywrócić dźwięk; usuwany po zakończeniu; `XDG_RUNTIME_DIR` czyści system przy wylogowaniu | `airlinx/core/audio.py:364-392` |
| `~/.config/autostart/io.github.airlinx.desktop` | tylko gdy użytkownik włączy autostart | `airlinx/app.py:399-409` |

Logi idą na standardowe wyjście błędów (`logging.basicConfig` bez pliku). Przy uruchomieniu z menu mogą trafić do dziennika systemu (journald). Tokeny w adresach URL są maskowane (`redact_url`, `scrub_urls`).

Aplikacja **nie zapisuje** na dysku obrazu ekranu, nagrań dźwięku ani kopii plików multimedialnych. Obraz i dźwięk są przechwytywane (ffmpeg, PulseAudio/PipeWire) tylko podczas transmisji i od razu wysyłane do TV.

### 1.3. Ruch sieciowy

W kodzie nie ma telemetrii, sprawdzania aktualizacji, raportowania błędów ani kont użytkowników. Ruch w sieci lokalnej:

- wykrywanie urządzeń: mDNS `_googlecast._tcp` (multicast 224.0.0.251:5353, biblioteka zeroconf) i SSDP (multicast 239.255.255.250:1900);
- zapytania HTTP/HTTPS do telewizorów: `:8008` i `:8443` (`/setup/eureka_info`), `:9197` (Samsung DMR), `:8009` (TLS, kanał CASTv2);
- kopia ekranu Cast: RTP/RTCP po UDP do TV, ramki szyfrowane AES-128-CTR z kluczem przekazanym kanałem TLS;
- kopia ekranu DLNA (fallback) i pliki multimedialne: lokalny serwer HTTP aiohttp, nasłuch na `0.0.0.0` (wszystkie interfejsy), losowy port, adresy z tokenem `secrets.token_urlsafe(16)` (128 bitów). **Transmisja nie jest szyfrowana** (zwykłe HTTP). Kto podsłuchuje sieć, widzi adres z tokenem i treść.

`get_lan_ip()` (`airlinx/core/media_server.py:108`) wywołuje `connect()` na gnieździe UDP do `8.8.8.8:9`, a potem `192.168.1.1`. Przy UDP `connect()` tylko wybiera trasę w jądrze. **Żaden pakiet nie jest wysyłany**, zapytań DNS też nie ma (adres IP jest literalny). Nie jest to połączenie z Google.

Ruch do internetu powstaje tylko w tych sytuacjach, i każdą uruchamia użytkownik:

1. użytkownik doda adres `http(s)://` do kolejki: ffprobe/ffmpeg pobiera plik z tego serwera albo telewizor pobiera go bezpośrednio (`media_server.py:296-305`);
2. kliknięcie linku zewnętrznego w UI otwiera go w przeglądarce systemowej (`app.py:236-249`);
3. `install.sh` pobiera zależności z PyPI (pip lub uv);
4. **poza kontrolą aplikacji:** po uruchomieniu odbiornika Google (Default Media Receiver `CC1AD845`, „Chrome Mirroring” `0F5096E8`) sam telewizor może łączyć się z serwerami Google. To ruch oprogramowania telewizora.

### 1.4. Kod i materiały osób trzecich

- `airlinx/core/cast/rtp.py`: według docstringu „port of openscreen cast/streaming/impl” (przeniesienie kodu C++ do Pythona).
- `airlinx/core/cast/castv2.py`: logika device-auth wzorowana na openscreen `cast/sender/channel/cast_auth_util.cc`; osadzone certyfikaty główne „Cast Root CA” i „Eureka Root CA” (Google Inc.), według komentarza z openscreen `cast/common/certificate/*-inc.h`.
- openscreen jest na licencji BSD-3-Clause, „Copyright 2018 The Chromium Authors” (tekst pobrany z `chromium.googlesource.com/openscreen/+/refs/heads/main/LICENSE`, 10.11.2026). **W plikach AirLinx nie ma noty BSD.**
- Fonty Inter i JetBrains Mono (`ui/assets/fonts`, `site/assets/fonts`, kopie w `docs/`), SIL OFL 1.1. Oba pliki `LICENSE-*-OFL-1.1.txt` są identyczne: to plik `debian/copyright` pakietu `zorin-os-fonts`. Zawierają wymagane noty copyright i pełny tekst OFL, ale wymieniają też fonty, których projekt nie używa.
- Zależności Pythona: pełna lista z licencjami w [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md).
- `samsungtvws` (LGPL-3.0) jest w `install.sh` (`DEPS`) i w treści landingu, ale **nie jest importowany** w `airlinx/`.

---

## 2. Compliance check (wg `legal:compliance-check`)

### Podsumowanie

**Można wydać pod warunkami.** Blokuje jedna rzecz: brak pliku licencji. Do tego trzeba dodać notę BSD dla kodu z openscreen i opublikować dokumenty z tego katalogu. Pozostałe obowiązki są lekkie albo w ogóle nie mają zastosowania, bo autor nie jest przedsiębiorcą i nie przetwarza danych użytkowników.

### Przepisy i ich zastosowanie

| Przepis | Czy dotyczy | Co z tego wynika |
|---|---|---|
| Ustawa o świadczeniu usług drogą elektroniczną (UŚUDE), art. 8 | Raczej **nie** (pkt 2.1) | Regulamin nie jest prawnie wymagany. Publikujemy go jako dobrą praktykę |
| RODO | **W wąskim zakresie** (pkt 2.2) | Polityka prywatności z informacjami z art. 13 dla korespondencji. Bez rejestru czynności (art. 30 ust. 5) i bez IOD |
| Prawo komunikacji elektronicznej, art. 399 | Dotyczy localStorage, ale **zgoda nie jest potrzebna** (pkt 2.3) | Informacja w polityce. Bez banera |
| Ustawa o prawach konsumenta, rozdz. 5b; dyrektywa 2019/770 | **Nie** (pkt 2.4) | Brak obowiązków konsumenckich. Regulamin i tak nie powinien zawierać klauzul abuzywnych |
| KC art. 471, 473 § 2, 415, 892 | **Tak** (pkt 2.5) | Wyłączenie odpowiedzialności z GPL działa tylko w granicach prawa polskiego |
| Dyrektywa 2024/2853 (odpowiedzialność za produkt) i CRA 2024/2847 | **Nie** przy obecnym modelu (pkt 2.5) | Do ponownej oceny przy komercjalizacji |
| Licencje (GPL-3.0, BSD-3, OFL, Apache-2.0, LGPL, MPL-2.0) | **Tak** (pkt 2.6) | Plik LICENSE, nota BSD dla openscreen, THIRD_PARTY_NOTICES |
| Znaki towarowe (rozp. 2017/1001, PWP) | **Tak** (pkt 2.7) | Użycie opisowe dozwolone. Nota o znakach, bez logotypów |
| Prawo autorskie, art. 74-75 (interoperacyjność) | **Tak** (pkt 2.8) | Ryzyko niskie. Udokumentować źródła wiedzy o protokole |
| Ustawa o języku polskim, art. 7 | Wątpliwe (pkt 2.6) | Polski regulamin streszcza GPL. Do oceny prawnika |

### 2.1. UŚUDE: czy regulamin jest wymagany

Art. 8 ust. 1 pkt 1 UŚUDE nakłada obowiązek regulaminu na **usługodawcę**. Według art. 2 pkt 6 usługodawca to osoba, która, „prowadząc, chociażby ubocznie, działalność zarobkową lub zawodową”, świadczy usługi drogą elektroniczną.

- Autor działa prywatnie i niekomercyjnie. Nie prowadzi działalności zarobkowej ani zawodowej, więc w świetle tej definicji raczej nie jest usługodawcą.
- Czy udostępnianie statycznej strony informacyjnej w ogóle jest „usługą świadczoną drogą elektroniczną” (art. 2 pkt 4), jest w doktrynie sporne. Ostrożne stanowisko mówi, że tak, bo treść jest przesyłana na indywidualne żądanie odbiorcy.
- Kod pobiera się z GitHuba. W tym zakresie usługę świadczy GitHub, Inc. na podstawie własnego regulaminu (GitHub Terms of Service), a nie autor.

**Wniosek:** regulamin z art. 8 UŚUDE najpewniej nie jest obowiązkowy. Publikujemy go, bo nic nie kosztuje, porządkuje zasady odpowiedzialności i zabezpiecza na wypadek, gdyby projekt stał się choćby ubocznie zarobkowy (np. przez darowizny z gratyfikacją, sponsoring, płatne wsparcie). Wtedy obowiązek powstanie. **Do weryfikacji przez prawnika**, łącznie ze zmianami UŚUDE wprowadzonymi przy wdrażaniu DSA.

### 2.2. RODO: czy autor przetwarza dane osobowe

**Aplikacja.** Wszystkie dane (pkt 1.2) zostają na komputerze użytkownika. Autor nie ma do nich dostępu, nie decyduje o celach ani sposobach ich przetwarzania na konkretnym komputerze i niczego nie odbiera. Autor nie jest więc administratorem (art. 4 pkt 7). Dostarczenie oprogramowania, które przetwarza dane lokalnie, nie czyni twórcy administratorem. Użytkownik przetwarza dane na własny, domowy użytek, a to wyłączenie z art. 2 ust. 2 lit. c RODO.

Zastrzeżenie: wybrany mikrofon może nagrać głos innych domowników, a kopia ekranu może pokazać cudze dane, np. powiadomienia. To nadal przetwarzanie przez użytkownika w ramach jego działań domowych. Regulamin przypomina o odpowiedzialności użytkownika.

**Strona na GitHub Pages.** Strona nie ma cookies, analityki, formularzy ani zasobów zewnętrznych. Fonty są serwowane z tej samej domeny (sprawdzone `grep` w `site/`). GitHub zapisuje adres IP odwiedzających w logach serwera ze względów bezpieczeństwa. Adres IP jest daną osobową (C-582/14 Breyer). GitHub opisuje te logi w GitHub General Privacy Statement i dokumentacji GitHub Pages jako przetwarzanie we własnym zakresie. Autor nie ma dostępu do logów i nie zlecił ich zbierania.

- Kwalifikacja jest niepewna: GitHub jako odrębny administrator, podmiot przetwarzający autora (jak przy typowym hostingu) albo współadministrator (por. C-40/17 Fashion ID). W polityce prywatności przyjęliśmy, że GitHub jest odrębnym administratorem, i odsyłamy do jego polityki. **Do oceny przez prawnika.**
- Hosting w USA: GitHub, Inc. deklaruje udział w EU-US Data Privacy Framework.

**Korespondencja i zgłoszenia.** Gdy ktoś napisze do autora przez GitHub Issues albo prywatne zgłoszenie podatności, autor staje się administratorem danych, które widzi w zgłoszeniu (nazwa konta, treść). Publiczny projekt nie mieści się w wyłączeniu domowym (C-101/01 Lindqvist). Obowiązki są wąskie: obowiązek informacyjny z art. 13 (realizuje go polityka prywatności), podstawa z art. 6 ust. 1 lit. f, rozsądny okres przechowywania i realizacja praw z art. 15-21 w zakresie korespondencji. Zgłoszenia w GitHub Issues to dane na platformie GitHub, gdzie zasady ustala GitHub.

**Nie dotyczy:** IOD (art. 37), DPIA (art. 35), rejestr czynności. Przy sporadycznej korespondencji bez danych szczególnych art. 30 ust. 5 zwalnia z rejestru.

### 2.3. PKE art. 399 (dawny art. 173 Prawa telekomunikacyjnego)

Od 10 listopada 2024 r. przechowywanie informacji i uzyskiwanie dostępu do informacji w urządzeniu końcowym reguluje art. 399 ustawy Prawo komunikacji elektronicznej. Strona zapisuje w `localStorage` jeden klucz, `airlinx-theme` (`light` lub `dark`). Robi to dopiero wtedy, gdy użytkownik kliknie przełącznik motywu.

- To zapis niezbędny do świadczenia usługi, której użytkownik wyraźnie zażądał (zapamiętanie wybranego wyglądu). Wytyczne EDPB 2/2023 i opinia WP29 4/2012 traktują takie zapisy („user-interface customisation”) jako zwolnione ze zgody.
- **Wniosek:** baner zgody nie jest potrzebny. W polityce prywatności informujemy, co jest zapisywane i jak to usunąć.
- Aplikacja desktopowa zapisuje pliki na komputerze użytkownika, który sam ją zainstalował i uruchomił. Nie jest to dostęp podmiotu zewnętrznego do urządzenia końcowego w rozumieniu art. 399.

### 2.4. Prawo konsumenckie i dyrektywa 2019/770

Rozdział 5b ustawy o prawach konsumenta (wdrożenie dyrektywy 2019/770) dotyczy umów między **przedsiębiorcą** a konsumentem o dostarczenie treści lub usługi cyfrowej, za które konsument płaci cenę albo podaje dane osobowe.

- Autor nie jest przedsiębiorcą (art. 43¹ KC).
- Użytkownik nie płaci i nie przekazuje autorowi danych.
- Dyrektywa wprost wyłącza wolne i otwarte oprogramowanie dostarczane bez ceny, gdy dane służą tylko bezpieczeństwu, kompatybilności i interoperacyjności (art. 3 ust. 5 lit. f). AirLinx nie zbiera żadnych danych, więc jest daleko od granicy.

**Wniosek:** rozdział 5b, rękojmia za treści cyfrowe i prawo odstąpienia nie mają zastosowania. Art. 385¹ KC (klauzule niedozwolone) formalnie dotyczy tylko umów przedsiębiorcy z konsumentem, ale regulamin i tak napisaliśmy bez klauzul z rejestru UOKiK. Gdyby projekt się skomercjalizował, ocena się zmieni.

Platforma ODR (rozporządzenie 524/2013) została zlikwidowana 20 lipca 2025 r. (rozporządzenie 2024/3228). Regulamin nie zawiera do niej linku.

### 2.5. Wyłączenie odpowiedzialności: GPL „AS IS” a prawo polskie

GPL-3.0 w pkt 15 i 16 wyłącza gwarancję i odpowiedzialność „w najszerszym zakresie dozwolonym przez prawo”. Pkt 17 nakazuje sądom stosować prawo lokalne najbliższe całkowitemu wyłączeniu. W Polsce:

- **art. 473 § 2 KC:** nieważne jest zastrzeżenie, że dłużnik nie odpowiada za szkodę wyrządzoną umyślnie. Część doktryny i orzecznictwa rozciąga ten zakaz na rażące niedbalstwo (*culpa lata dolo aequiparatur*), a w obrocie konsumenckim UOKiK uznaje to za standard;
- **odpowiedzialność deliktowa (art. 415 KC)** za szkodę na osobie nie może być wyłączona z góry. Wyłączenie odpowiedzialności za szkodę na osobie to klasyczna klauzula abuzywna (art. 385³ pkt 1 KC);
- licencja GPL jest nieodpłatna. Część doktryny stosuje do nieodpłatnych licencji przez analogię art. 892 KC, według którego darczyńca odpowiada tylko za winę umyślną i rażące niedbalstwo. Analogia jest sporna, ale wskazuje, że realne ryzyko autora ogranicza się do umyślności i rażącego niedbalstwa;
- **dyrektywa 2024/2853** (nowa odpowiedzialność za produkt, obejmuje oprogramowanie, termin wdrożenia 9 grudnia 2026 r.) nie dotyczy wolnego oprogramowania rozwijanego lub dostarczanego poza działalnością gospodarczą (art. 2 ust. 2). AirLinx się w tym mieści;
- **CRA (rozporządzenie 2024/2847)** obejmuje produkty udostępniane na rynku w ramach działalności handlowej. Niekomercyjne FOSS jest wyłączone (motywy 18-19).

**Wniosek:** w regulaminie zostawiamy wyłączenie gwarancji i odpowiedzialności, ale z wyraźnym zastrzeżeniem: nie obejmuje szkody wyrządzonej umyślnie lub przez rażące niedbalstwo, szkody na osobie ani przypadków, w których prawo nie pozwala wyłączyć odpowiedzialności. Tak sformułowana klauzula jest skuteczna w Polsce i nie jest abuzywna.

### 2.6. Licencje

**GPL-3.0 dla AirLinx.** Bez pliku licencji nikt nie ma prawa kopiować ani modyfikować kodu. Domyślnie obowiązuje pełna ochrona z ustawy o prawie autorskim, mimo publicznego repozytorium (GitHub ToS daje tylko prawo przeglądania i forkowania w serwisie). **Przed wydaniem trzeba:** dodać plik `LICENSE` z pełnym, niezmienionym tekstem GPL-3.0, dodać sekcję „Licencja” w README z informacją o prawach autorskich (`Copyright (C) 2026 <autor>`) i rozstrzygnąć, czy chodzi o „GPL-3.0-only”, czy „GPL-3.0-or-later”. **Stan:** wybrano GPL-3.0-or-later, `LICENSE` i sekcja w README są dodane. Nota `Copyright (C) 2026 TymonPawelczyk` jest w README. Zalecane: nagłówek SPDX w plikach źródłowych (`SPDX-License-Identifier: GPL-3.0-or-later`).

**Zgodność zależności z GPL-3.0-or-later.** Wszystkie zależności uruchomieniowe są zgodne (szczegóły w THIRD_PARTY_NOTICES):

- MIT, BSD-2/3, ISC, PSF-2.0, MIT-0: zgodne;
- Apache-2.0 (aiohttp, async-upnp-client, yarl, multidict, requests…): zgodna z GPL w wersji 3 i późniejszych, nie z GPL-2.0;
- LGPL-2.1-or-later (zeroconf), LGPL-3.0 (samsungtvws): zgodne, bo obie pozwalają na połączenie na zasadach GPL-3.0. Klauzula „or later” daje odbiorcom wybór wersji 3 albo późniejszej. Ewentualna przyszła GPL-4 dla całości zależałaby od jej treści i od licencji komponentów. To kolejny argument za usunięciem nieużywanego `samsungtvws` (R14);
- MPL-2.0 (certifi, pośrednio przez requests): zgodna dzięki klauzuli „Secondary License”. certifi nie ma oznaczenia „Incompatible With Secondary Licenses”;
- cryptography (Apache-2.0 OR BSD-3-Clause): zgodna.

**Niezgodności nie znaleziono.** Autor nie rozpowszechnia zależności, bo instaluje je pip na komputerze użytkownika, więc obowiązki atrybucji z tych licencji powstaną dopiero przy dystrybucji paczek (AppImage, Flatpak, .deb, wheel z vendoringiem). THIRD_PARTY_NOTICES publikujemy już teraz.

Systemowe biblioteki (PyGObject, GTK 3, WebKit2GTK, ffmpeg, PulseAudio/PipeWire) nie są dołączane, tylko wymagane. ffmpeg w buildach dystrybucji bywa na GPL. Nie zmienia to sytuacji AirLinx, bo AirLinx jest na GPL-3.0 i wywołuje ffmpeg jako osobny proces.

**Kod z openscreen (BSD-3-Clause).** Port `rtp.py` i logika `castv2.py` to opracowania (art. 2 ust. 1 pr. aut.) kodu objętego BSD-3. BSD-3 wymaga zachowania noty copyright, listy warunków i wyłączenia odpowiedzialności przy redystrybucji kodu źródłowego (warunek 1). Dziś tych elementów nie ma. **Przed wydaniem trzeba:** dodać notę w THIRD_PARTY_NOTICES (zrobione) i, zgodnie z warunkiem 1 i dobrą praktyką, krótki nagłówek w `rtp.py` i `castv2.py`, który odsyła do tej noty. Warunek 3 BSD zakazuje używania nazwy Google LLC do promowania produktu. Nota o znakach mówi wprost, że Google nie popiera projektu.

**Certyfikaty główne Cast i Eureka.** To dane techniczne: klucze publiczne i podpisy generowane algorytmicznie. Ich twórczy charakter (art. 1 ust. 1 pr. aut.) jest wątpliwy. Są publicznie dostępne i rozpowszechniane w openscreen na licencji BSD-3. Osadzenie służy wyłącznie weryfikacji tożsamości TV. Ryzyko niskie. Wymieniamy je w THIRD_PARTY_NOTICES.

**Fonty (OFL 1.1).** Dołączanie do oprogramowania jest dozwolone (OFL pkt 2), pod warunkiem dołączenia noty i licencji. Warunek jest spełniony, ale pliki licencji są kopią `debian/copyright` z Zorin OS. Zalecenie (bez pośpiechu): podmienić je na oryginalne `OFL.txt` z repozytoriów `rsms/inter` i `JetBrains/JetBrainsMono`. Konwersja do WOFF to dopuszczalna zmiana formatu. Żaden z fontów nie ma zastrzeżonej nazwy (Reserved Font Name), więc nazwy można zostawić.

**Język licencji (art. 7 ustawy o języku polskim).** Przepis wymaga języka polskiego w obrocie z konsumentami na terytorium RP. Nie jest jasne, czy obejmuje nieodpłatną licencję od osoby prywatnej. Oficjalny tekst GPL jest tylko po angielsku. FSF nie zatwierdza tłumaczeń, ale publikuje listę nieoficjalnych. Regulamin streszcza najważniejsze zasady po polsku i odsyła do LICENSE. **Do oceny przez prawnika.**

### 2.7. Znaki towarowe

W tekstach występują: Google Cast, Chromecast (Google LLC), Samsung (Samsung Electronics Co., Ltd.), DLNA (Digital Living Network Alliance), AirPlay (Apple Inc.), Miracast (Wi-Fi Alliance), Linux (Linus Torvalds), GNOME (GNOME Foundation).

- Użycie znaku, żeby wskazać przeznaczenie lub kompatybilność, jest dozwolone, jeśli odpowiada uczciwym praktykom (art. 14 ust. 1 lit. c rozporządzenia 2017/1001; analogicznie art. 156 PWP). Warunki: tylko słowne nazwy, żadnych logotypów, nic nie sugeruje powiązania, autoryzacji ani certyfikacji.
- Naruszenie znaku wymaga używania go w obrocie (w ramach działalności handlowej). Niekomercyjny charakter projektu dodatkowo obniża ryzyko.
- Stopka landingu zawiera już zastrzeżenie o Google i Samsungu. Dodajemy pełną notę `docs/legal/TRADEMARKS.md` i odpowiednie punkty w regulaminie.
- AirLinx nie ma certyfikacji DLNA ani Google Cast. Teksty nie mogą sugerować, że ją ma (np. „DLNA Certified”, „Chromecast built-in”).

**Nazwa „AirLinx”.** Wyszukiwanie z 10 listopada 2026 r. (wyszukiwarka internetowa, agregatory danych USPTO) dało:

- **USA: zarejestrowany znak słowny AIRLINX**, nr rej. 5291427 (zgł. 86526086), klasa 42, właściciel Hitachi Global Air Power US, LLC (dawniej Sullair). To system zdalnego monitoringu sprężarek. Według agregatora jest też nowsze zgłoszenie tego właściciela (99565126, sprężarki). Inne zgłoszenia AIRLINX w USA (m.in. w klasie 9) wygasły.
- **UE/Polska: nie udało się zweryfikować.** Bazy EUIPO eSearch, TMview i UPRP wymagają interaktywnego wyszukiwania. W wynikach wyszukiwarki nie znaleziono unijnego znaku AIRLINX.
- Nie znaleziono aplikacji ani firmy programistycznej o nazwie dokładnie „AirLinx”.

Ocena: towary i usługi Hitachi (sprężarki przemysłowe) są odległe od aplikacji do udostępniania ekranu, a projekt jest niekomercyjny i działa w UE. Ryzyko kolizji uznajemy za niskie do średniego. Prefiks „Air” kojarzy się też z Apple (AirPlay, AirDrop), ale samo „Air” jest słabe odróżniająco i powszechnie używane. **Zalecenie przed wydaniem:** ręczne sprawdzenie w TMview (wszystkie urzędy UE) dla „AIRLINX” i podobnych („AIRLINK”, „AIRLYNX”) w klasach 9, 38 i 42. Najlepiej przed promocją strony, póki zmiana nazwy jest tania.

Drobna uwaga: identyfikator aplikacji `io.github.airlinx` sugeruje konto `github.com/airlinx`, a repozytorium jest pod `TymonPawelczyk`. Prawnie to bez znaczenia. Przy publikacji na Flathub konwencja wymaga identyfikatora odpowiadającego kontrolowanej domenie, np. `io.github.TymonPawelczyk.AirLinx`.

### 2.8. Protokół Cast, certyfikaty i inżynieria odwrotna

- **Protokoły i interfejsy nie są chronione prawem autorskim.** Chroniony jest sposób wyrażenia programu, a nie idee i zasady, w tym zasady interfejsów (art. 1 ust. 2¹ i art. 74 ust. 2 pr. aut.; art. 1 ust. 2 dyrektywy 2009/24; C-406/10 SAS Institute: funkcjonalność, język programowania i formaty plików nie są utworem). Własna implementacja protokołu CASTv2 i Cast Streaming nie narusza praw do oprogramowania Google.
- **Źródła wiedzy o protokole:** otwarty kod openscreen (BSD-3, legalnie dostępny i licencjonowany) oraz obserwacja ruchu z własnym telewizorem (`spikes/cast_streaming/PROTOCOL.md`). Obserwowanie, badanie i testowanie programu przez uprawnionego użytkownika jest dozwolone (art. 75 ust. 2 pkt 2 pr. aut.) i nie da się tego wyłączyć umową (art. 75 ust. 3). Nic nie wskazuje na dekompilację oprogramowania Google. Gdyby do niej doszło, dozwolona jest w granicach koniecznych do interoperacyjności (art. 75 ust. 2 pkt 3), ale uzyskane informacje nie mogą trafić do osób trzecich poza tym celem. Zalecenie: udokumentować w `PROTOCOL.md`, że wiedza pochodzi z openscreen i własnych obserwacji.
- **Brak obchodzenia zabezpieczeń.** Device-auth w AirLinx działa w drugą stronę: nadawca sprawdza tożsamość telewizora. Nie omija żadnego zabezpieczenia technicznego (art. 79 ust. 6 i art. 118¹ pr. aut.). Szyfrowanie AES w Cast Streaming to szyfrowanie transportu z kluczem wybieranym przez nadawcę, a nie DRM. AirLinx nie odszyfrowuje treści chronionych DRM.
- **Warunki Google.** AirLinx nie używa Google Cast SDK, nie rejestruje własnego identyfikatora aplikacji i nie akceptuje Google Cast SDK Additional Developer Terms. Uruchamia publiczne odbiorniki Google (Default Media Receiver, Chrome Mirroring) tak jak inne niezależne nadawcy, np. pychromecast. Ryzyko jest raczej techniczne (Google może zmienić protokół lub zablokować nieznanych nadawców) niż prawne. Ryzyko patentowe przy niekomercyjnym FOSS jest odległe.

### 2.9. Bezpieczeństwo w sieci lokalnej

To nie jest obowiązek prawny wobec osób trzecich, ale wpływa na rzetelność komunikatów i ewentualną odpowiedzialność za rażące niedbalstwo:

- serwer HTTP nasłuchuje na wszystkich interfejsach. Jeśli komputer ma publiczny adres IP bez zapory, serwer jest osiągalny z internetu i chroni go tylko 128-bitowy token;
- pliki i kopia ekranu DLNA idą zwykłym HTTP, bez szyfrowania;
- **landing mówi w FAQ: „Obraz i dźwięk idą do telewizora zaszyfrowane”** i „Ruch idzie wyłącznie między komputerem a telewizorem”. Pierwsze zdanie jest prawdziwe tylko dla kopii ekranu Cast. Drugie nie uwzględnia adresów `http(s)://` dodanych przez użytkownika. Zalecenie: doprecyzować oba zdania (poza zakresem tej gałęzi, zgłoszone zespołowi landingu). Regulamin i polityka prywatności opisują stan faktyczny.

---

## 3. Macierz ryzyk (wg `legal:legal-risk-assessment`)

Skala: waga (W) 1-5 × prawdopodobieństwo (P) 1-5. Zielone 1-4, żółte 5-9, pomarańczowe 10-15, czerwone 16-25.

| ID | Ryzyko | Kategoria | W | P | Wynik | Poziom | Mitygacja |
|---|---|---|---|---|---|---|---|
| R1 | Brak pliku LICENSE: kod formalnie bez licencji, nikt nie może legalnie go używać, forkować ani rozwijać; deklaracja „GPL” bez pokrycia | IP | 3 | 1 | 3 | ZIELONY (przed zmianą: 15, pomarańczowy) | Zrobione: `LICENSE` (GPL-3.0, wariant GPL-3.0-or-later) i sekcja „License” w README. Opcjonalnie nagłówki SPDX |
| R2 | Kod z openscreen bez noty BSD-3: naruszenie warunku 1 licencji, więc korzystanie z kodu bez licencji | IP | 3 | 4 | **12** | POMARAŃCZOWY | Nota w THIRD_PARTY_NOTICES (zrobione) i nagłówki w `rtp.py`, `castv2.py`. **Przed wydaniem** |
| R3 | Kolizja nazwy „AirLinx” ze znakiem AIRLINX (Hitachi, USA) lub nieznanym znakiem w UE | Znaki | 3 | 2 | 6 | ŻÓŁTY | Wyszukiwanie w TMview/EUIPO/UPRP przed promocją; gotowość do zmiany nazwy |
| R4 | Nieprecyzyjne zdania na landingu o szyfrowaniu i braku ruchu zewnętrznego: zarzut wprowadzania w błąd, utrata zaufania | Reputacja | 2 | 4 | 8 | ŻÓŁTY | Poprawić FAQ i sekcję „bezpieczeństwo” |
| R5 | Roszczenie użytkownika o szkodę (np. utrata danych, wyciek treści w LAN) | Odpowiedzialność | 3 | 1 | 3 | ZIELONY | Wyłączenie z GPL w granicach art. 473 § 2 KC; regulamin; ostrzeżenia o sieci |
| R6 | Użycie znaków Google/Samsung/DLNA sugerujące powiązanie lub certyfikację | Znaki | 3 | 2 | 6 | ŻÓŁTY | Nota TRADEMARKS, brak logotypów, brak słów „certified”/„built-in” |
| R7 | Zarzut naruszenia praw Google przez implementację Cast lub osadzone certyfikaty | IP | 3 | 1 | 3 | ZIELONY | Udokumentować źródła (openscreen BSD, obserwacja); brak dekompilacji i DRM |
| R8 | Zablokowanie nieznanych nadawców przez Google: przestaje działać kopia ekranu | Operacyjne | 2 | 2 | 4 | ZIELONY | Ryzyko techniczne; fallback DLNA |
| R9 | Niewykonanie obowiązku informacyjnego RODO wobec osób piszących do autora | Prywatność | 2 | 2 | 4 | ZIELONY | Polityka prywatności z art. 13 (zrobione) |
| R10 | localStorage bez zgody: zarzut naruszenia art. 399 PKE | Prywatność | 1 | 1 | 1 | ZIELONY | Zapis niezbędny na żądanie użytkownika; informacja w polityce |
| R11 | Brak regulaminu UŚUDE, gdyby projekt stał się zarobkowy | Regulacyjne | 2 | 1 | 2 | ZIELONY | Regulamin opublikowany (zrobione) |
| R12 | Użytkownik wyświetla na TV treści bez praw (publiczne pokazy, DRM) albo nagrywa innych mikrofonem | Treści osób trzecich | 2 | 2 | 4 | ZIELONY | Regulamin przenosi odpowiedzialność na użytkownika; autor nie hostuje treści |
| R13 | Pliki licencji fontów to kopia `debian/copyright` z Zorin OS: nieczytelna atrybucja | IP | 1 | 2 | 2 | ZIELONY | Podmienić na oryginalne `OFL.txt` |
| R14 | Niewykorzystana zależność `samsungtvws` (LGPL-3.0) instalowana u użytkownika | IP/Higiena | 1 | 2 | 2 | ZIELONY | Zrobione w 0.1.0: usunięty z `install.sh`, `requirements-dev.txt`, landingu i THIRD_PARTY_NOTICES |

Rozkład przed zmianami: 2 pomarańczowe (R1, R2), 3 żółte (R3, R4, R6), reszta zielona. Czerwonych brak. R1 jest już zamknięte (dodano `LICENSE`). R2 spadnie do zielonego (W3 × P1) po dodaniu noty BSD w plikach źródłowych.

---

## 4. Rekomendacje

### Przed wydaniem v0.1.0 (obowiązkowo)

1. ~~**Dodać plik `LICENSE`**~~ **Zrobione:** `LICENSE` (pełny tekst GPL-3.0) i sekcja „License” w README, wariant GPL-3.0-or-later. Nota `Copyright (C) 2026 TymonPawelczyk` w README. [R1]
2. **Dodać notę BSD-3 dla openscreen:** w THIRD_PARTY_NOTICES (zrobione) i w nagłówkach `airlinx/core/cast/rtp.py` oraz `airlinx/core/cast/castv2.py`, np. `# Portions derived from openscreen, Copyright 2018 The Chromium Authors, BSD-3-Clause; see THIRD_PARTY_NOTICES.md`. [R2]
3. **Opublikować** REGULAMIN, POLITYKĘ PRYWATNOŚCI, THIRD_PARTY_NOTICES i TRADEMARKS, podlinkować je w stopce strony (zrobione na tej gałęzi) i w README.
4. ~~**Uzupełnić placeholdery** autora i adresu kontaktowego w regulaminie, polityce i na stronach.~~ **Zrobione:** autor „TymonPawelczyk (GitHub)”, kontakt przez GitHub Issues, sprawy bezpieczeństwa przez GitHub Private Vulnerability Reporting, sekcja o poczcie e-mail zastąpiona opisem kontaktu przez GitHub. Do weryfikacji przez prawnika: czy pseudonim z kontaktem przez GitHub wystarcza do art. 13 ust. 1 lit. a RODO i do danych usługodawcy z ustawy o świadczeniu usług drogą elektroniczną.
5. ~~**Poprawić dwa zdania na landingu**~~ **Zrobione** (sekcja bezpieczeństwa i dwa pytania FAQ, wg R4). (FAQ „Co AirLinx wysyła…”, sekcja bezpieczeństwa): szyfrowanie dotyczy kopii ekranu Cast; przy DLNA i plikach ruch w LAN jest nieszyfrowany; przy adresach `http(s)://` ruch wychodzi do wskazanego serwera. [R4]

### Wkrótce po wydaniu (zalecane)

6. Ręczne wyszukiwanie „AIRLINX” w TMview/EUIPO/UPRP (klasy 9, 38, 42) przed promocją strony. [R3]
7. ~~Usunąć `samsungtvws` z `install.sh` i landingu, jeśli nie jest używany.~~ **Zrobione w 0.1.0** (landing, `install.sh`, `requirements-dev.txt`, THIRD_PARTY_NOTICES). [R14]
8. Podmienić pliki licencji fontów na oryginalne `OFL.txt`. [R13]
9. Dopisać w `spikes/cast_streaming/PROTOCOL.md` źródła wiedzy o protokole. [R7]
10. Rozważyć domyślne bindowanie serwera HTTP do adresu interfejsu LAN zamiast `0.0.0.0`. To zmiana techniczna, poprawia bezpieczeństwo i rzetelność komunikatów.

### Może poczekać (przy zmianie modelu)

11. Przy jakiejkolwiek komercjalizacji (darowizny z gratyfikacją, sponsoring, płatne wsparcie, sklep z aplikacjami) trzeba ponownie ocenić: UŚUDE (regulamin staje się obowiązkowy), prawo konsumenckie (rozdz. 5b), dyrektywę 2024/2853, CRA i art. 385¹ KC.
12. Przy dystrybucji paczek binarnych (AppImage, Flatpak, .deb) trzeba dołączyć licencje i noty wszystkich dołączanych zależności, w tym pełne teksty Apache-2.0, LGPL i MPL-2.0. Przy GPL-3.0 trzeba też zapewnić „Corresponding Source”.
13. Gdyby pojawiła się analityka, formularz lub newsletter: aktualizacja polityki, ocena art. 399 PKE (baner zgody), rejestr czynności.

### Co powinien sprawdzić prawnik

- Czy autor jest „usługodawcą” w rozumieniu art. 2 pkt 6 UŚUDE po zmianach związanych z DSA.
- Kwalifikację GitHub (Pages) wobec autora: odrębny administrator, podmiot przetwarzający czy współadministrator.
- Skuteczność wyłączenia odpowiedzialności w regulaminie (art. 473 § 2 KC, rażące niedbalstwo, relacja do pkt 15-17 GPL) i to, czy regulamin może rozszerzać lub zawężać warunki GPL. Celowo tego nie robi.
- Art. 7 ustawy o języku polskim a anglojęzyczna GPL.
- Wynik wyszukiwania znaków towarowych dla „AirLinx”.
- Czy streszczenie licencji w regulaminie nie tworzy rozbieżności z tekstem GPL.
- Czy regulamin w ogóle wiąże osobę, która tylko przegląda stronę lub uruchamia program. Nie ma akceptacji (clickwrap), a GPL w pkt 9 mówi, że do uruchamiania programu nie trzeba akceptować licencji. Jeśli regulamin nie wiąże, ograniczenie odpowiedzialności działa tylko w zakresie GPL i przepisów ogólnych (art. 415 KC).
- Gdyby autor jednak był usługodawcą w rozumieniu UŚUDE, art. 5 ust. 2 wymaga podania imienia, nazwiska i adresu zamieszkania. Dla osoby prywatnej to realny problem prywatności. Trzeba rozważyć adres do doręczeń lub skrytkę.

---

## 5. Przegląd regulaminu (wg `legal:review-contract`)

Podstawa przeglądu: brak firmowego playbooka. Porównanie z ogólnymi standardami, rejestrem klauzul niedozwolonych UOKiK, art. 385¹-385³ i 473 § 2 KC oraz pkt 15-17 GPL-3.0. Strona przeglądu: autor (redagujący, licencjodawca). Druga strona: użytkownicy, w tym konsumenci.

| Obszar | Ocena | Ustalenie | Zmiana |
|---|---|---|---|
| Ograniczenie odpowiedzialności (§ 7) | ZIELONY po zmianie | Wyłączenia: umyślność, rażące niedbalstwo, szkoda na osobie, przepisy bezwzględnie obowiązujące. Ust. 2 bez zastrzeżenia mógł być czytany jako pełne wyłączenie odpowiedzialności za nienależyte wykonanie (art. 385³ pkt 2 KC) | Dodano „W zakresie, w jakim pozwalają na to przepisy” |
| Relacja do GPL (§ 7 ust. 4) | ZIELONY po zmianie | Zdanie „odpowiadają pkt 15-17” mogło sugerować zmianę licencji. GPL pkt 7 pozwala na inne wyłączenie gwarancji, ale nie na dodatkowe ograniczenia | Nowe brzmienie: ust. 1-3 opisują stosowanie pkt 15-17 według prawa polskiego i nie ograniczają uprawnień z Licencji |
| Streszczenie licencji (§ 6) | ZIELONY po zmianie | „razem z kodem źródłowym” było węższe niż GPL (pkt 6 dopuszcza też inne sposoby udostępnienia kodu) | „zapewnisz odbiorcom dostęp do kodu źródłowego”, dodano informację o pkt 9 GPL |
| Licencja: plik `LICENSE` (§ 6 ust. 1) | ZIELONY po zmianie | Regulamin odsyłał do pliku, którego nie było | `LICENSE` dodany; regulamin wskazuje wariant GPL-3.0-or-later (§ 2 pkt 8, § 6 ust. 1) |
| Zmiany regulaminu (§ 13) | ZIELONY po zmianie | Otwarty katalog przyczyn („np.”) to klasyczna klauzula z rejestru UOKiK (art. 385³ pkt 10 i 19 KC) | Zamknięty katalog ważnych przyczyn, 14 dni przed wejściem w życie |
| Rozwiązanie / zakończenie korzystania | ZIELONY po zmianie | Brakowało informacji o prawie użytkownika do zakończenia korzystania | Dodano odinstalowanie w § 7 ust. 5 |
| Odszkodowania od użytkownika (indemnification) | ZIELONY | Brak. Przy konsumentach tak powinno zostać | Bez zmian |
| Prawo właściwe i sąd (§ 14) | ZIELONY | Prawo polskie z zachowaniem ochrony konsumenta z państwa zamieszkania; sąd według przepisów ogólnych (bez klauzuli „sąd siedziby Autora”) | Bez zmian |
| Dane osobowe (§ 10) | ZIELONY | Odesłanie do polityki prywatności | Bez zmian |
| Odpowiedzialność za treści (§ 8) | ZIELONY | Jasny podział: treści, prawa autorskie, DRM, mikrofon. Bez obowiązku zwolnienia autora z roszczeń | Bez zmian |
| Reklamacje (§ 12) | ZIELONY | Odpowiedź w zgłoszeniu GitHub bez gwarantowanego terminu, zachowane prawa ustawowe, brak odesłania do zlikwidowanej platformy ODR | Bez zmian |
| Moc wiążąca regulaminu | ŻÓŁTY | Brak akceptacji przez użytkownika, a do uruchomienia programu akceptacja GPL nie jest potrzebna | Do oceny prawnika (pkt 4) |
| Klauzula salwatoryjna (§ 14 ust. 3) | ZIELONY | Standard | Bez zmian |

Ogólna ocena: regulamin jest wyważony i korzystny dla użytkownika. Nie zawiera klauzul z rejestru UOKiK, które dałoby się zastosować do tej sytuacji. Jedyny blokujący punkt (brak pliku `LICENSE`) został usunięty.

---

## 6. Źródła

- Wyszukiwanie znaków: [markinton.com: AIRLINX 86526086](https://markinton.com/trademark/airlinx-86526086), [markinton.com: AIRLINX 85774740](https://markinton.com/trademark/airlinx-85774740), [gleanmark.com: Hitachi Global Air Power US](https://gleanmark.com/owner/hitachiglobalairpowerusllc), [Plant Services: Sullair changes name to Hitachi Global Air Power](https://www.plantservices.com/industry-news/news/33003638/sullair-changes-name-to-hitachi-global-air-power). To agregatory danych USPTO, dane trzeba potwierdzić w USPTO TSDR.
- Licencja openscreen: `https://chromium.googlesource.com/openscreen/+/refs/heads/main/LICENSE`.
- Licencje zależności: metadane pakietów (`*.dist-info/METADATA`) w środowisku projektu, stan na 10.11.2026.
- GitHub General Privacy Statement: `https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement`.
