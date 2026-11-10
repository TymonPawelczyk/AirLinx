# Regulamin strony i oprogramowania AirLinx

> **Uwaga.** Wzór przygotowany z pomocą AI (Claude). Nie stanowi porady prawnej. Przed publikacją zalecana weryfikacja przez prawnika.
> Wersja 1.0, obowiązuje od 2026-11-10. Przygotowano 10 listopada 2026 r.

## § 1. Po co jest ten regulamin

1. Regulamin opisuje zasady korzystania ze strony AirLinx i z programu AirLinx.
2. AirLinx jest projektem prywatnym i niekomercyjnym. Za program i stronę nie płacisz, nie zakładasz konta i nie podajesz żadnych danych.
3. Prawa do kopiowania, zmieniania i rozpowszechniania programu wynikają z licencji GNU GPL w wersji 3 lub dowolnej późniejszej (§ 6). Regulamin nie zmienia tej licencji i jej nie ogranicza.

## § 2. Definicje

1. **Autor**: osoba wskazana w § 3, twórca programu i właściciel strony.
2. **Program** albo **AirLinx**: aplikacja na Linuksa, która wysyła obraz, dźwięk i pliki multimedialne z komputera na telewizor w sieci lokalnej, razem z kodem źródłowym i dokumentacją.
3. **Strona**: statyczna strona informacyjna o programie, hostowana w serwisie GitHub Pages, razem z podstronami (ten regulamin, polityka prywatności).
4. **Repozytorium**: publiczne repozytorium kodu programu w serwisie GitHub: `https://github.com/TymonPawelczyk/AirLinx`.
5. **Użytkownik** albo **Ty**: każda osoba, która odwiedza stronę albo pobiera, instaluje lub uruchamia program.
6. **Telewizor**: urządzenie z odbiornikiem Google Cast albo DLNA MediaRenderer, do którego program wysyła obraz, dźwięk lub pliki.
7. **Sieć lokalna**: sieć domowa lub firmowa (Wi-Fi albo kabel), w której są jednocześnie komputer i telewizor.
8. **Licencja**: GNU General Public License w wersji 3 albo, według Twojego wyboru, w dowolnej późniejszej wersji opublikowanej przez Free Software Foundation (oznaczenie SPDX: GPL-3.0-or-later). Tekst wersji 3 jest w pliku `LICENSE` w repozytorium.

## § 3. Kto udostępnia stronę i program

1. Stronę i program udostępnia TymonPawelczyk (GitHub), osoba fizyczna działająca prywatnie (profil: `https://github.com/TymonPawelczyk`).
2. Kontakt z Autorem jest możliwy wyłącznie przez serwis GitHub: zgłoszenie w zakładce Issues w repozytorium (`https://github.com/TymonPawelczyk/AirLinx/issues`), a w sprawach bezpieczeństwa prywatne zgłoszenie podatności (§ 9 ust. 5). Autor nie podaje adresu e-mail.
3. Autor nie prowadzi działalności gospodarczej związanej z AirLinx i nie pobiera za program opłat.

## § 4. Co obejmuje regulamin

1. Strona zawiera informacje o programie: opis funkcji, wymagania, instrukcję instalacji, odpowiedzi na pytania i linki do repozytorium.
2. Program pobierasz z repozytorium, czyli z serwisu GitHub. Pobieranie kodu, zgłaszanie błędów i inne działania w serwisie GitHub podlegają też regulaminowi GitHub (GitHub Terms of Service). Za działanie serwisu GitHub odpowiada GitHub, Inc.
3. Autor nie świadczy płatnych usług, nie prowadzi kont użytkowników, nie przyjmuje zamówień i nie zawiera umów sprzedaży.
4. Autor nie przechowuje treści, które wysyłasz na telewizor. Program działa na Twoim komputerze i przesyła dane bezpośrednio do Twojego telewizora.

## § 5. Wymagania techniczne

1. Do przeglądania strony wystarczy aktualna przeglądarka internetowa. JavaScript nie jest wymagany. Służy tylko do przełącznika motywu i kopiowania poleceń.
2. Do korzystania z programu potrzebujesz:
   1. komputera z Linuksem i środowiskiem GNOME w sesji X11 (Wayland nie jest obsługiwany);
   2. Pythona 3 z PyGObject (GTK 3, WebKit2GTK 4.1), programu ffmpeg i serwera dźwięku PipeWire albo PulseAudio z narzędziem `pactl`;
   3. telewizora z Google Cast (kopia ekranu, dźwięk, pliki) albo z DLNA (pliki, kopia ekranu z większym opóźnieniem);
   4. komputera i telewizora w tej samej sieci lokalnej oraz zapory, która przepuszcza wykrywanie urządzeń (mDNS UDP 5353, SSDP UDP 1900) i połączenia od telewizora do komputera;
   5. dostępu do internetu w czasie instalacji, bo instalator pobiera biblioteki z PyPI.
3. Aktualne wymagania i listę sprawdzonych urządzeń podaje strona i plik README. Program może nie działać z telewizorami i konfiguracjami, których Autor nie sprawdzał.

## § 6. Licencja

1. Program jest wolnym oprogramowaniem na licencji GNU GPL w wersji 3 albo, według Twojego wyboru, dowolnej późniejszej (GPL-3.0-or-later). Pełny, wiążący tekst wersji 3 (po angielsku) jest w pliku `LICENSE` w repozytorium oraz na stronie `https://www.gnu.org/licenses/gpl-3.0.html`.
2. Najważniejsze zasady licencji w skrócie (w razie rozbieżności rozstrzyga tekst licencji):
   1. możesz uruchamiać program w dowolnym celu, także komercyjnym;
   2. możesz badać i zmieniać kod;
   3. możesz rozpowszechniać kopie programu, oryginalne i zmienione, pod warunkiem że udostępnisz je na tej samej licencji, zachowasz informacje o prawach autorskich i zapewnisz odbiorcom dostęp do kodu źródłowego.
3. Program zawiera i wykorzystuje elementy osób trzecich (biblioteki, fonty, fragmenty kodu openscreen, certyfikaty główne Google Cast). Ich licencje i noty są w pliku `THIRD_PARTY_NOTICES.md` w repozytorium.
4. Do samego uruchamiania programu nie musisz akceptować Licencji (pkt 9 Licencji). Jej warunki stosujesz, gdy program zmieniasz lub rozpowszechniasz.

## § 7. Brak gwarancji i granice odpowiedzialności

1. Program jest udostępniany bezpłatnie i „w takim stanie, w jakim jest”. Autor nie daje gwarancji, że program będzie działał bez błędów, z każdym telewizorem, bez przerw albo z określoną jakością obrazu i opóźnieniem. Wyniki pomiarów na stronie dotyczą konkretnego sprzętu, na którym je zmierzono.
2. W zakresie, w jakim pozwalają na to przepisy, Autor nie odpowiada za szkody wynikłe z korzystania z programu lub strony albo z braku możliwości korzystania z nich, w szczególności za utratę danych, przerwy w transmisji ani nieprawidłowe działanie telewizora lub sieci.
3. Ograniczenie z ust. 2 **nie obejmuje**:
   1. szkody wyrządzonej przez Autora umyślnie lub przez rażące niedbalstwo;
   2. szkody na osobie (uszczerbku na zdrowiu, śmierci);
   3. innych przypadków, w których przepisy bezwzględnie obowiązujące nie pozwalają wyłączyć ani ograniczyć odpowiedzialności.
4. Ust. 1-3 opisują, jak pkt 15-17 Licencji stosuje się według prawa polskiego. Nie ograniczają uprawnień, które daje Ci Licencja.
5. Strona i repozytorium mogą być czasowo niedostępne, np. z powodu awarii lub prac w serwisie GitHub. Autor może zakończyć rozwijanie programu. Kopie programu, które już masz, nadal możesz używać na zasadach Licencji. Ty też możesz w każdej chwili przestać korzystać z programu i go odinstalować (`./install.sh --uninstall`).

## § 8. Treści, które pokazujesz na telewizorze

1. Ty decydujesz, co program wysyła na telewizor: cały ekran, okno, dźwięk systemu, mikrofon, dźwięk wybranej aplikacji albo pliki. Za te treści odpowiadasz Ty.
2. Pokazując filmy, muzykę i zdjęcia, przestrzegaj praw autorskich i praw do wizerunku. Pokaz w domu, dla rodziny i znajomych, zwykle mieści się w dozwolonym użytku osobistym. Pokaz publiczny (np. w lokalu, na wydarzeniu) może wymagać zgody uprawnionych lub licencji.
3. Program nie omija zabezpieczeń przed kopiowaniem (DRM). Regulaminy serwisów z filmami i muzyką mogą zabraniać przechwytywania obrazu ekranu. Sprawdź je, zanim udostępnisz taki serwis na telewizorze.
4. Kopia ekranu pokazuje wszystko, co widać na wybranym monitorze lub w oknie, także powiadomienia i wiadomości. Mikrofon może przesyłać rozmowy osób w pomieszczeniu. Nie używaj programu do podglądania ani podsłuchiwania innych osób bez ich wiedzy.
5. Nie używaj programu do wyświetlania treści niezgodnych z prawem.

## § 9. Bezpieczeństwo w sieci lokalnej

1. W czasie pracy program uruchamia na Twoim komputerze serwer HTTP, z którego telewizor pobiera pliki i strumień obrazu. Serwer działa na losowym porcie. Każdy plik ma adres z losowym, trudnym do odgadnięcia tokenem. Po zakończeniu odtwarzania adres przestaje działać.
2. Kopia ekranu przez Google Cast jest szyfrowana (AES-128). Pliki multimedialne i kopia ekranu przez DLNA są przesyłane bez szyfrowania (HTTP). Osoba, która ma dostęp do Twojej sieci i podsłuchuje ruch, może zobaczyć adres z tokenem i przesyłaną treść.
3. Dlatego:
   1. używaj programu w sieci, której ufasz, np. w domowej sieci zabezpieczonej hasłem;
   2. nie używaj go w sieciach publicznych i otwartych (hotel, kawiarnia, sieć dla gości);
   3. jeśli Twój komputer ma publiczny adres IP, korzystaj z zapory, która blokuje połączenia z internetu.
4. Przy pierwszym połączeniu program zapamiętuje tożsamość telewizora. Jeśli przy kolejnym połączeniu tożsamość się nie zgadza, program przerywa połączenie i pyta, czy zaufać telewizorowi ponownie. Potwierdzaj to tylko wtedy, gdy wiesz, że telewizor został wymieniony lub zresetowany.
5. Błędy bezpieczeństwa zgłaszaj Autorowi prywatnie, przez GitHub Private Vulnerability Reporting: w repozytorium zakładka Security, przycisk „Report a vulnerability” (`https://github.com/TymonPawelczyk/AirLinx/security/advisories/new`). Nie zgłaszaj ich w publicznych Issues, żeby można je było naprawić przed ujawnieniem.

## § 10. Prywatność

1. Program nie wysyła Autorowi żadnych danych: nie ma telemetrii, kont ani sprawdzania aktualizacji. Strona nie używa cookies ani analityki.
2. Szczegóły, w tym zapisy lokalne programu, logi serwera GitHub Pages i dane ze zgłoszeń w serwisie GitHub, opisuje Polityka prywatności.

## § 11. Znaki towarowe

1. Google Cast i Chromecast są znakami towarowymi Google LLC. Samsung jest znakiem towarowym Samsung Electronics Co., Ltd. DLNA jest znakiem Digital Living Network Alliance. AirPlay jest znakiem towarowym Apple Inc. Miracast jest znakiem towarowym Wi-Fi Alliance. Linux jest zarejestrowanym znakiem towarowym Linusa Torvaldsa. GNOME jest znakiem towarowym GNOME Foundation.
2. Nazwy te występują na stronie i w programie tylko po to, żeby opisać, z jakimi urządzeniami i systemami AirLinx współpracuje.
3. AirLinx jest niezależnym projektem. Nie jest powiązany z Google, Samsungiem, Apple, Wi-Fi Alliance ani DLNA, nie jest przez nie wspierany ani certyfikowany.

## § 12. Zgłoszenia, reklamacje i kontakt

1. Błędy programu, problemy ze stroną i zastrzeżenia do regulaminu zgłaszasz w zakładce Issues w repozytorium: `https://github.com/TymonPawelczyk/AirLinx/issues`. Potrzebujesz do tego konta w serwisie GitHub. Zgłoszenie jest publiczne. Błędy bezpieczeństwa zgłaszasz prywatnie (§ 9 ust. 5).
2. Opisz, czego dotyczy zgłoszenie, a przy błędach programu podaj wersję systemu, model telewizora i kroki, które prowadzą do błędu. Nie wklejaj do publicznych zgłoszeń danych osobowych ani pełnych logów z adresami z tokenami.
3. Autor odpowiada na zgłoszenia w tym samym zgłoszeniu w serwisie GitHub. Projekt jest rozwijany prywatnie, więc Autor nie gwarantuje terminu odpowiedzi ani naprawy błędu.
4. Złożenie zgłoszenia nie ogranicza Twoich praw wynikających z przepisów prawa.

## § 13. Zmiany regulaminu

1. Autor może zmienić regulamin tylko z ważnej przyczyny: zmiany przepisów, zmiany funkcji programu, zmiany sposobu udostępniania strony lub programu (np. hostingu), potrzeby poprawy bezpieczeństwa albo usunięcia niejasności w tekście.
2. Nowa wersja ma nową datę i numer wersji. Autor publikuje ją na stronie co najmniej 14 dni przed wejściem w życie, chyba że zmiana wynika z przepisów prawa albo z potrzeby pilnej poprawy bezpieczeństwa.
3. Poprzednie wersje są dostępne w historii repozytorium.
4. Zmiana regulaminu nie odbiera praw, które już masz na podstawie Licencji do pobranych kopii programu.

## § 14. Postanowienia końcowe

1. Regulamin podlega prawu polskiemu. Jeśli jesteś konsumentem i mieszkasz w innym państwie, zachowujesz ochronę, jaką dają Ci bezwzględnie obowiązujące przepisy tego państwa.
2. Spory rozstrzyga sąd właściwy według przepisów ogólnych.
3. Jeśli któreś postanowienie regulaminu okaże się nieważne, pozostałe nadal obowiązują.
4. Regulamin jest dostępny na stronie (podstrona „Regulamin”) i w repozytorium (`docs/legal/REGULAMIN.md`). Możesz go bezpłatnie pobrać, zapisać i wydrukować.
