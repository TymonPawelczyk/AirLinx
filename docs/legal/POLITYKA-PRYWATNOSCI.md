# Polityka prywatności AirLinx

> **Uwaga.** Wzór przygotowany z pomocą AI (Claude). Nie stanowi porady prawnej. Przed publikacją zalecana weryfikacja przez prawnika.
> Wersja 1.0, obowiązuje od 2026-11-10. Przygotowano 10 listopada 2026 r.

## W skrócie

- Program AirLinx działa na Twoim komputerze. Nie wysyła Autorowi żadnych danych: nie ma telemetrii, kont, reklam ani sprawdzania aktualizacji.
- Ustawienia i zapamiętane telewizory zapisuje tylko na Twoim dysku.
- Obraz i dźwięk idą bezpośrednio z komputera do telewizora w Twojej sieci lokalnej. Nic nie jest nagrywane na dysk.
- Strona nie używa cookies ani analityki. Zapamiętuje tylko wybrany motyw (jasny lub ciemny) w Twojej przeglądarce.
- Stronę hostuje GitHub, który zapisuje adresy IP odwiedzających w logach serwera. Autor nie ma do nich dostępu.
- Autor przetwarza dane osobowe tylko wtedy, gdy napiszesz do niego przez GitHub. Zgłoszenia w Issues są publiczne, więc nie podawaj w nich danych osobowych.

## 1. Kto odpowiada za Twoje dane

1. Administratorem danych ze zgłoszeń, które obsługuje Autor (pkt 5), jest TymonPawelczyk (GitHub), osoba fizyczna działająca prywatnie, twórca AirLinx, dalej „Autor”. Kontakt: zgłoszenie w GitHub Issues (`https://github.com/TymonPawelczyk/AirLinx/issues`), a w sprawach bezpieczeństwa GitHub Private Vulnerability Reporting (pkt 5).
2. Autor nie powołał inspektora ochrony danych. Nie ma takiego obowiązku.
3. Za dane przetwarzane przez serwis GitHub (hosting strony, repozytorium, zgłoszenia Issues) odpowiada GitHub, Inc. na zasadach własnej polityki prywatności (pkt 4).
4. Za dane, które program przetwarza na Twoim komputerze (pkt 2 i 3), odpowiadasz Ty. Autor nie ma do nich dostępu.

## 2. Program: co zapisuje na Twoim komputerze

Program zapisuje pliki tylko na Twoim dysku, z uprawnieniami Twojego konta:

| Plik | Co zawiera | Po co |
|---|---|---|
| `~/.config/airlinx/config.json` | ustawienia: wybrany protokół, ostatnio używany telewizor, ręcznie dodane adresy IP telewizorów, ustawienia jakości obrazu, wybrane źródło dźwięku (nazwa mikrofonu lub aplikacji), ostatnio udostępniany ekran lub okno, głośność, preferencje wyglądu | żeby przy kolejnym uruchomieniu nie ustawiać wszystkiego od nowa |
| `~/.config/airlinx/config.json.bak` | kopia ustawień, gdy plik był uszkodzony | żeby nie stracić uszkodzonych ustawień bez śladu |
| `~/.config/airlinx/known_tvs.json` (dostęp tylko dla Ciebie, 0600) | tożsamość telewizorów, z którymi się łączyłeś: dane certyfikatu urządzenia i odcisk certyfikatu TLS, data pierwszego połączenia | żeby wykryć podszywanie się pod Twój telewizor |
| `$XDG_RUNTIME_DIR/airlinx/audio-routes.json` | tymczasowy zapis, które strumienie dźwięku program przekierował | żeby przywrócić dźwięk po awarii; plik jest usuwany po zakończeniu transmisji, a katalog czyści system przy wylogowaniu |
| `~/.config/autostart/io.github.airlinx.desktop` | polecenie uruchomienia programu | tylko gdy włączysz start razem z systemem |

Jeśli ustawisz zmienną `XDG_CONFIG_HOME`, pliki trafiają tam zamiast do `~/.config`.

Komunikaty diagnostyczne (logi) program wypisuje na standardowe wyjście. Gdy uruchamiasz go z menu, system może je zapisać w swoim dzienniku (journald) na Twoim komputerze. Adresy z tokenami są w logach maskowane.

**Jak usunąć dane:** zamknij program i usuń katalog `~/.config/airlinx`. `./install.sh --uninstall` usuwa program, ale celowo zostawia ten katalog.

## 3. Program: co wysyła i dokąd

### Czego program nie wysyła

Program nie wysyła Autorowi ani nikomu innemu: informacji o Tobie, o komputerze, o telewizorze, statystyk użycia, raportów błędów ani treści z ekranu. Nie łączy się z serwerami Autora, bo takich serwerów nie ma.

Funkcja ustalająca adres komputera w sieci lokalnej pyta system, jaką trasą poszedłby ruch do adresu 8.8.8.8. Nie wysyła przy tym żadnego pakietu ani zapytania do internetu.

### Ruch w sieci lokalnej

1. **Wykrywanie telewizorów:** program wysyła w sieci lokalnej zapytania mDNS i SSDP, a do znalezionych urządzeń zapytania o nazwę i model. Te zapytania widzą wszystkie urządzenia w tej sieci.
2. **Kopia ekranu przez Google Cast:** obraz i dźwięk idą bezpośrednio do telewizora, szyfrowane AES-128. Klucz jest przekazywany szyfrowanym kanałem TLS.
3. **Pliki i kopia ekranu przez DLNA:** program uruchamia na Twoim komputerze serwer HTTP na losowym porcie. Telewizor pobiera z niego plik albo strumień pod adresem z losowym tokenem. Po zakończeniu odtwarzania adres przestaje działać. **Ten ruch nie jest szyfrowany.** Osoba, która ma dostęp do Twojej sieci i podsłuchuje ruch, może zobaczyć adres z tokenem i przesyłaną treść. Dlatego używaj programu tylko w sieci, której ufasz.

### Mikrofon i dźwięk wybranej aplikacji

1. Mikrofon albo dźwięk wybranej aplikacji program przechwytuje tylko wtedy, gdy sam wybierzesz to źródło dźwięku, i tylko w czasie transmisji na telewizor.
2. Dźwięk idzie od razu do telewizora. Program go nie nagrywa, nie zapisuje na dysk i nie analizuje.
3. Przy trybie „wybrana aplikacja” program na czas transmisji przekierowuje dźwięk tej aplikacji w serwerze dźwięku (PipeWire/PulseAudio). Po zakończeniu przywraca poprzednie ustawienia.
4. W ustawieniach zapisuje tylko nazwę wybranego mikrofonu lub aplikacji (pkt 2).

### Kiedy program łączy się z internetem

Tylko wtedy, gdy sam tego chcesz albo gdy robi to telewizor:

1. gdy dodasz do kolejki adres pliku z internetu (`http://` lub `https://`), program (przez ffmpeg) albo telewizor pobiera ten plik z serwera, który wskazałeś. Ten serwer widzi adres IP Twojej sieci;
2. gdy klikniesz link zewnętrzny w programie, otwiera się on w Twojej przeglądarce;
3. instalator (`install.sh`) pobiera biblioteki z PyPI (Python Package Index). PyPI widzi adres IP Twojej sieci;
4. **telewizor:** gdy program uruchamia na telewizorze wbudowany odbiornik Google Cast, telewizor może łączyć się z serwerami Google według własnych ustawień. To działanie oprogramowania telewizora. Opisują je polityki prywatności producenta telewizora i Google.

## 4. Strona internetowa

1. Strona jest statyczna i hostowana w serwisie **GitHub Pages** (GitHub, Inc., USA). Fonty i obrazy są pobierane z tej samej domeny. Strona nie korzysta z zewnętrznych skryptów, fontów, map ani filmów.
2. **Logi serwera.** Przy każdej wizycie GitHub zapisuje adres IP odwiedzającego ze względów bezpieczeństwa, także gdy nie jesteś zalogowany w GitHub. Autor nie ma dostępu do tych logów, nie zlecał ich zbierania i nie otrzymuje statystyk odwiedzin. GitHub przetwarza te dane jako odrębny administrator na zasadach GitHub General Privacy Statement: `https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement`. GitHub deklaruje, że przekazuje dane z UE do USA w ramach EU-US Data Privacy Framework.
3. **Cookies.** Strona nie zapisuje cookies.
4. **localStorage.** Gdy klikniesz przełącznik motywu, przeglądarka zapisuje w pamięci lokalnej (`localStorage`) jeden wpis: `airlinx-theme` z wartością `light` albo `dark`. Wpis nie trafia do żadnego serwera, służy tylko do zapamiętania wybranego wyglądu i nie ma daty ważności. Jest niezbędny do funkcji, o którą prosisz, więc nie wymaga zgody (art. 399 ustawy Prawo komunikacji elektronicznej). Usuniesz go, czyszcząc dane witryny w ustawieniach przeglądarki. Bez niego strona pokazuje motyw zgodny z ustawieniami systemu.
5. **Brak analityki i formularzy.** Strona nie mierzy ruchu, nie ma formularzy, komentarzy ani newslettera.
6. **Linki do GitHub.** Linki do repozytorium prowadzą do serwisu GitHub, który ma własne zasady prywatności i cookies.

## 5. Kontakt z Autorem przez GitHub

1. Autor nie podaje adresu e-mail. Kontaktujesz się z nim wyłącznie przez serwis GitHub:
   1. błędy, pytania i zastrzeżenia zgłaszasz w zakładce Issues w repozytorium: `https://github.com/TymonPawelczyk/AirLinx/issues`;
   2. błędy bezpieczeństwa zgłaszasz prywatnie, przez GitHub Private Vulnerability Reporting: zakładka Security w repozytorium, przycisk „Report a vulnerability” (`https://github.com/TymonPawelczyk/AirLinx/security/advisories/new`).
2. Do obu potrzebujesz konta w serwisie GitHub. Konto, zgłoszenia i komentarze przetwarza GitHub, Inc. jako odrębny administrator, na zasadach GitHub General Privacy Statement (pkt 4). Autor nie decyduje, jakie dane GitHub zbiera ani jak długo je przechowuje.
3. **Zgłoszenia i komentarze w Issues są publiczne.** Widzi je każdy, mogą trafić do wyszukiwarek i do kopii repozytorium. Nie wpisuj w nich danych osobowych (imienia i nazwiska, adresu e-mail, adresu zamieszkania), haseł ani pełnych logów z adresami z tokenami. Prywatne zgłoszenie podatności widzą tylko Autor i osoby, które Autor zaprosi do pomocy przy naprawie.
4. Autor przetwarza tylko to, co widzi w Twoim zgłoszeniu: nazwę Twojego konta GitHub, treść zgłoszenia i komentarzy oraz to, co do nich dołączysz.
5. Cel: odpowiedź na zgłoszenie, naprawa błędu, obsługa zastrzeżenia, a w razie sporu obrona przed roszczeniami.
6. Podstawa prawna: prawnie uzasadniony interes Autora polegający na obsłudze zgłoszeń dotyczących projektu (art. 6 ust. 1 lit. f RODO).
7. Podanie danych jest dobrowolne. Bez konta GitHub nie złożysz zgłoszenia.
8. Odbiorcy: treść publicznego zgłoszenia może przeczytać każdy. Prywatne zgłoszenie podatności widzą osoby z ust. 3. Po naprawie Autor może opublikować opis podatności (GitHub Security Advisory), w którym może być wskazana nazwa konta zgłaszającego. Autor nie przekazuje danych ze zgłoszeń poza serwis GitHub.
9. Okres przechowywania: zgłoszenia zostają w repozytorium jako część historii projektu, dopóki repozytorium istnieje. Swoje komentarze możesz w każdej chwili sam edytować albo usunąć.
10. Autor nie podejmuje wobec Ciebie decyzji w sposób zautomatyzowany i nie profiluje.

## 6. Twoje prawa

W zakresie danych, które przetwarza Autor (pkt 5), masz prawo:

1. dostępu do danych i otrzymania ich kopii (art. 15 RODO);
2. sprostowania danych (art. 16);
3. usunięcia danych (art. 17);
4. ograniczenia przetwarzania (art. 18);
5. sprzeciwu wobec przetwarzania opartego na prawnie uzasadnionym interesie (art. 21);
6. wniesienia skargi do Prezesa Urzędu Ochrony Danych Osobowych (ul. Stawki 2, 00-193 Warszawa, `https://uodo.gov.pl`) albo do organu nadzorczego w państwie, w którym mieszkasz lub pracujesz.

Prawo do przenoszenia danych (art. 20) nie ma zastosowania, bo przetwarzanie nie opiera się na zgodzie ani umowie.

Żeby skorzystać z praw, załóż zgłoszenie w GitHub Issues i wskaż, którego zgłoszenia lub komentarza dotyczy prośba. Nie podawaj w nim innych danych niż nazwa Twojego konta GitHub. Autor odpowiada w ciągu miesiąca.

Do danych zapisanych przez program na Twoim komputerze (pkt 2) i do logów GitHub (pkt 4) Autor nie ma dostępu, więc nie może ich udostępnić ani usunąć. Pliki programu usuniesz sam. Wnioski dotyczące danych GitHub kieruj do GitHub.

## 7. Zmiany polityki

1. Autor zmieni politykę, gdy zmieni się sposób działania programu lub strony, np. gdyby pojawiła się analityka albo formularz. Taka zmiana zostanie opisana tutaj, zanim zacznie obowiązywać.
2. Każda wersja ma numer i datę. Poprzednie wersje są w historii repozytorium (`docs/legal/POLITYKA-PRYWATNOSCI.md`).
