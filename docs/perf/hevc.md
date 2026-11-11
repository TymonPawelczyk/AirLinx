# H.264 i H.265 (HEVC) w AirLinx: pomiary i rekomendacja

Data pomiarów: 2026-11-11. Sprzęt: RTX 3080 (NVENC), ffmpeg 4.4.2 (Ubuntu 22.04), 16 rdzeni CPU. Skrypt: `scripts/bench_hevc.py` (pełna macierz, `--presets`, `--bursts`, `--software`). Wszystko lokalne: bez telewizora, bez sieci.

## Wniosek w trzech zdaniach

1. **Na NVENC z ustawieniami aplikacji (`p1`, `tune ull`, CBR, bez B-klatek, GOP 2 s) H.265 nie daje lepszej jakości przy tej samej przepływności.**
   Na łatwej treści różnica PSNR mieści się w ±0,7 dB (raz na korzyść jednego, raz drugiego kodeka). Na trudnej (każdy piksel zmienia się w każdej klatce) w 1440p i 4K H.264 jest lepszy o 1,5 do 2,4 dB,
   w 4K przy 12 Mb/s nawet o 7 dB; w 1080p wyniki są mieszane (H.265 lepszy o 0,3 do 1,1 dB przy 6 i 8 Mb/s, gorszy o 0,9 dB przy 12 Mb/s).
2. Czas kodowania klatki jest taki sam (około 2 ms w 1080p60, 3,4 ms w 1440p60, 8 ms w 4K30 na całym torze: wczytanie, wysłanie na GPU, skalowanie, kodowanie), a klatka kluczowa H.265 nie jest
   systematycznie mniejsza niż H.264 (od 7% mniejsza do ok. 25% większa), a największa klatka jest o 6 do 27% większa. H.265 nie zmniejsza więc ani obciążenia karty, ani impulsu, który na Wi-Fi może powodować zrywanie obrazu.
3. **Rekomendacja: domyślny kodek zostaje H.264 (`mirror.codec = "h264"`), a profile Balans i Kino zachowują dzisiejsze przepływności (18 i 28 Mb/s) także w H.265.** Nie ma pomiaru, który uzasadniałby niższą przepływność.
   Pomiar niczego nie mówi o tym, jak telewizor dekoduje H.265 ani jak zachowuje się jego Wi-Fi, więc ostateczna odpowiedź wymaga próby na S85F (sekcja „Test na telewizorze”).

## Metoda

- Treści syntetyczne ffmpeg (nie nagranie pulpitu: nagranie z ekranu użytkownika zawiera prywatne dane, a treści syntetyczne są powtarzalne), renderowane raz do surowego YUV w `/dev/shm`, 5 s każda:
  `testsrc2` (płaskie kolory, tekst, równomierny ruch: zbliżone do interfejsu) oraz `life` (automat komórkowy, w każdej klatce zmienia się cały obraz: najgorszy przypadek dla kodera ekranu).
- Kodowanie dokładnie tymi argumentami, które buduje `airlinx.core.capture.build_video_cmd` (podmieniane tylko wejście x11grab na plik i wyjście z potoku na plik): `h264_nvenc` / `hevc_nvenc`,
  `-preset p1 -tune ull -zerolatency 1 -delay 0 -rc cbr -bf 0 -g <2 s> -forced-idr 1`, filtr `hwupload_cuda,scale_cuda`, `-profile:v main`.
- „ms / klatkę” = (czas ścienny całego przebiegu − czas przebiegu jednej klatki, czyli inicjalizacja NVENC ok. 0,18 s) ÷ (liczba klatek − 1). Obejmuje cały tor, nie sam koder.
- Jakość: strumień jest dekodowany i porównywany z źródłem filtrami `psnr` i `ssim` ffmpeg (średnia po klatkach, wszystkie płaszczyzny). ffmpeg 4.4 z Ubuntu nie ma `libvmaf`, więc nie ma VMAF-a.
- „Rzeczywiście” = rozmiar pliku ÷ czas. Przy trudnej treści i niskim celu CBR przekracza cel (np. `life` 1080p cel 6 Mb/s: H.264 8,6 Mb/s, H.265 7,2 Mb/s), bo bufor VBV jest bardzo mały (pół sekundy).

## Wyniki: przepływności z zadania

Preset `p1`, jak w aplikacji. 1080p60 (12, 8, 6 Mb/s), 1440p60 (18, 12 Mb/s) oraz 4K30 dla profilu Kino (28, 18, 12 Mb/s).

**testsrc2** (UI-podobna)

| Rozdzielczość | Preset | Docelowo | Kodek | Rzeczywiście | ms / klatkę | PSNR (dB) | SSIM |
|---|---|---|---|---|---|---|---|
| 1080p60 | p1 | 12 Mb/s | H.264 | 12.1 Mb/s | 2.11 | 44.91 | 0.9956 |
| 1080p60 | p1 | 12 Mb/s | H.265 | 12.12 Mb/s | 2.05 | 44.36 | 0.9952 |
| 1080p60 | p1 | 8 Mb/s | H.264 | 8.03 Mb/s | 2.11 | 41.95 | 0.9917 |
| 1080p60 | p1 | 8 Mb/s | H.265 | 8.05 Mb/s | 2.09 | 41.43 | 0.9908 |
| 1080p60 | p1 | 6 Mb/s | H.264 | 6.01 Mb/s | 2.09 | 40.43 | 0.9873 |
| 1080p60 | p1 | 6 Mb/s | H.265 | 6.03 Mb/s | 2.01 | 39.96 | 0.9867 |
| 1440p60 | p1 | 18 Mb/s | H.264 | 18.08 Mb/s | 3.41 | 43.50 | 0.9943 |
| 1440p60 | p1 | 18 Mb/s | H.265 | 18.06 Mb/s | 3.48 | 43.31 | 0.9944 |
| 1440p60 | p1 | 12 Mb/s | H.264 | 12.02 Mb/s | 3.52 | 40.97 | 0.9893 |
| 1440p60 | p1 | 12 Mb/s | H.265 | 12.03 Mb/s | 3.35 | 40.78 | 0.9897 |
| 2160p30 | p1 | 28 Mb/s | H.264 | 28.48 Mb/s | 8.17 | 45.35 | 0.9964 |
| 2160p30 | p1 | 28 Mb/s | H.265 | 28.77 Mb/s | 7.77 | 46.05 | 0.9969 |
| 2160p30 | p1 | 18 Mb/s | H.264 | 18.53 Mb/s | 8.58 | 42.06 | 0.9925 |
| 2160p30 | p1 | 18 Mb/s | H.265 | 18.11 Mb/s | 7.79 | 42.18 | 0.9930 |
| 2160p30 | p1 | 12 Mb/s | H.264 | 12.21 Mb/s | 8.01 | 40.01 | 0.9867 |
| 2160p30 | p1 | 12 Mb/s | H.265 | 12.08 Mb/s | 7.44 | 40.07 | 0.9882 |

**life** (najgorszy przypadek)

| Rozdzielczość | Preset | Docelowo | Kodek | Rzeczywiście | ms / klatkę | PSNR (dB) | SSIM |
|---|---|---|---|---|---|---|---|
| 1080p60 | p1 | 12 Mb/s | H.264 | 12.17 Mb/s | 2.0 | 31.27 | 0.9246 |
| 1080p60 | p1 | 12 Mb/s | H.265 | 12.09 Mb/s | 2.37 | 30.40 | 0.9014 |
| 1080p60 | p1 | 8 Mb/s | H.264 | 8.65 Mb/s | 2.02 | 26.09 | 0.8132 |
| 1080p60 | p1 | 8 Mb/s | H.265 | 8.31 Mb/s | 2.04 | 27.17 | 0.8295 |
| 1080p60 | p1 | 6 Mb/s | H.264 | 8.6 Mb/s | 2.05 | 26.03 | 0.8122 |
| 1080p60 | p1 | 6 Mb/s | H.265 | 7.16 Mb/s | 1.96 | 26.32 | 0.8063 |
| 1440p60 | p1 | 18 Mb/s | H.264 | 18.12 Mb/s | 3.38 | 30.16 | 0.9229 |
| 1440p60 | p1 | 18 Mb/s | H.265 | 18.55 Mb/s | 3.73 | 27.81 | 0.8915 |
| 1440p60 | p1 | 12 Mb/s | H.264 | 12.12 Mb/s | 3.41 | 26.77 | 0.8645 |
| 1440p60 | p1 | 12 Mb/s | H.265 | 12.66 Mb/s | 3.53 | 25.31 | 0.8392 |
| 2160p30 | p1 | 28 Mb/s | H.264 | 29.07 Mb/s | 7.82 | 51.98 | 0.9969 |
| 2160p30 | p1 | 28 Mb/s | H.265 | 28.72 Mb/s | 8.58 | 49.99 | 0.9953 |
| 2160p30 | p1 | 18 Mb/s | H.264 | 18.92 Mb/s | 7.68 | 48.19 | 0.9930 |
| 2160p30 | p1 | 18 Mb/s | H.265 | 18.58 Mb/s | 8.72 | 46.27 | 0.9899 |
| 2160p30 | p1 | 12 Mb/s | H.264 | 12.69 Mb/s | 7.53 | 43.17 | 0.9826 |
| 2160p30 | p1 | 12 Mb/s | H.265 | 12.04 Mb/s | 8.19 | 35.87 | 0.9451 |

Jak czytać: różnica poniżej około 0,5 dB PSNR (albo 0,003 SSIM) jest niewidoczna gołym okiem. W żadnym wierszu H.265 przy niższej przepływności nie dorównuje H.264 przy wyższej: np. `testsrc2` 1440p
18 Mb/s H.264 = 43,5 dB, a H.265 przy 12 Mb/s = 40,8 dB (−2,7 dB); 4K30: H.265 przy 18 Mb/s = 42,2 dB wobec 45,3 dB H.264 przy 28 Mb/s (−3,1 dB).
Ta sama jakość wymaga więc tej samej przepływności w obu kodekach.

## Wolniejsze presety NVENC

Presety `p4` i `p7` (reszta jak wyżej) dają obu kodekom 0,2 do 1 dB, kosztem 1 do 5 ms na klatkę, a porównanie kodeków pozostaje mieszane: SSIM H.265 jest wyższy w 6 z 12 par (o 0,0002 do 0,010), w pozostałych 6 niższy (o 0,0006 do 0,025).
Nie ma presetu, przy którym H.265 wyraźnie wygrywa. Zostajemy przy `p1` (najkrótsze opóźnienie).

Wynik pomiaru `testsrc2` i `life`, 1080p60 6 Mb/s i 1440p60 12 Mb/s:

**testsrc2**

| Rozdzielczość | Preset | Docelowo | Kodek | Rzeczywiście | ms / klatkę | PSNR (dB) | SSIM |
|---|---|---|---|---|---|---|---|
| 1080p60 | p1 | 6 Mb/s | H.264 | 6.01 Mb/s | 2.03 | 40.43 | 0.9873 |
| 1080p60 | p1 | 6 Mb/s | H.265 | 6.03 Mb/s | 1.95 | 39.96 | 0.9867 |
| 1080p60 | p4 | 6 Mb/s | H.264 | 6.02 Mb/s | 3.12 | 40.59 | 0.9874 |
| 1080p60 | p4 | 6 Mb/s | H.265 | 6.03 Mb/s | 3.75 | 40.29 | 0.9878 |
| 1080p60 | p7 | 6 Mb/s | H.264 | 6.01 Mb/s | 4.56 | 40.68 | 0.9875 |
| 1080p60 | p7 | 6 Mb/s | H.265 | 6.03 Mb/s | 4.33 | 40.29 | 0.9877 |
| 1440p60 | p1 | 12 Mb/s | H.264 | 12.02 Mb/s | 3.53 | 40.97 | 0.9893 |
| 1440p60 | p1 | 12 Mb/s | H.265 | 12.03 Mb/s | 3.36 | 40.78 | 0.9897 |
| 1440p60 | p4 | 12 Mb/s | H.264 | 12.03 Mb/s | 4.97 | 41.17 | 0.9897 |
| 1440p60 | p4 | 12 Mb/s | H.265 | 12.01 Mb/s | 6.33 | 41.13 | 0.9908 |
| 1440p60 | p7 | 12 Mb/s | H.264 | 12.03 Mb/s | 7.92 | 41.31 | 0.9897 |
| 1440p60 | p7 | 12 Mb/s | H.265 | 12.01 Mb/s | 7.45 | 41.13 | 0.9909 |

**life**

| Rozdzielczość | Preset | Docelowo | Kodek | Rzeczywiście | ms / klatkę | PSNR (dB) | SSIM |
|---|---|---|---|---|---|---|---|
| 1080p60 | p1 | 6 Mb/s | H.264 | 8.15 Mb/s | 1.98 | 26.11 | 0.8157 |
| 1080p60 | p1 | 6 Mb/s | H.265 | 6.88 Mb/s | 1.98 | 26.43 | 0.8104 |
| 1080p60 | p4 | 6 Mb/s | H.264 | 7.89 Mb/s | 2.96 | 26.29 | 0.8204 |
| 1080p60 | p4 | 6 Mb/s | H.265 | 6.25 Mb/s | 3.82 | 26.82 | 0.8183 |
| 1080p60 | p7 | 6 Mb/s | H.264 | 7.68 Mb/s | 5.85 | 26.47 | 0.8250 |
| 1080p60 | p7 | 6 Mb/s | H.265 | 6.26 Mb/s | 4.43 | 26.85 | 0.8192 |
| 1440p60 | p1 | 12 Mb/s | H.264 | 12.08 Mb/s | 3.54 | 26.92 | 0.8681 |
| 1440p60 | p1 | 12 Mb/s | H.265 | 12.59 Mb/s | 3.59 | 25.43 | 0.8430 |
| 1440p60 | p4 | 12 Mb/s | H.264 | 12.04 Mb/s | 5.06 | 29.33 | 0.9049 |
| 1440p60 | p4 | 12 Mb/s | H.265 | 12.03 Mb/s | 6.35 | 29.51 | 0.9150 |
| 1440p60 | p7 | 12 Mb/s | H.264 | 12.07 Mb/s | 11.23 | 30.74 | 0.9212 |
| 1440p60 | p7 | 12 Mb/s | H.265 | 12.02 Mb/s | 7.51 | 29.56 | 0.9156 |

## Klatka kluczowa i największa klatka (źródło zrywania obrazu na Wi-Fi)

Rozmiary jednostek dostępu po kodowaniu (ffprobe), `p1`, przepływność profilu: 1440p60 18 Mb/s i 4K30 28 Mb/s, 5 s, GOP 2 s.

| Rozdzielczość | Treść | Kodek | Klatka kluczowa (średnio) | Zwykła klatka (średnio / p99) | Największa |
|---|---|---|---|---|---|
| 1440p60 | testsrc2 | H.264 | 64 kB | 37 / 54 kB | 73 kB |
| 1440p60 | testsrc2 | H.265 | 72 kB | 36 / 56 kB | 84 kB |
| 1440p60 | life | H.264 | 349 kB | 34 / 53 kB | 629 kB |
| 1440p60 | life | H.265 | 323 kB | 35 / 51 kB | 716 kB |
| 2160p30 | testsrc2 | H.264 | 165 kB | 115 / 172 kB | 186 kB |
| 2160p30 | testsrc2 | H.265 | 178 kB | 116 / 163 kB | 197 kB |
| 2160p30 | life | H.264 | 258 kB | 112 / 151 kB | 267 kB |
| 2160p30 | life | H.265 | 326 kB | 115 / 139 kB | 339 kB |

H.265 nie rozładowuje impulsu klatki kluczowej: największa klatka jest o 6 do 27% większa niż w H.264. Jeśli zrywanie w Balansie i Kinie wynika z impulsów na Wi-Fi telewizora, H.265 go nie usunie;
skuteczniejsze są pacing (`MIN_PACE_BPS`), dłuższy GOP albo intra-refresh (patrz `spikes/cast_streaming/PROTOCOL.md` 4.1), niezależnie od kodeka.

## Zapasowy koder programowy

`libx265 -preset ultrafast -tune zerolatency` koduje 1080p60 `testsrc2` przy 8 Mb/s z prędkością ok. 180 klatek/s na całym torze (`libx264 ultrafast`: ok. 430 klatek/s) na 16 rdzeniach,
więc w czasie rzeczywistym wystarcza, ale zajmuje procesor. Dla 1440p i 4K nie mierzono (treść łatwa, wynik zbyt optymistyczny dla ruchu na całym ekranie).

## Rekomendacja bitrate dla profili przy H.265

| Profil | H.264 (dziś) | H.265, rekomendacja | Uzasadnienie |
|---|---|---|---|
| Gra / praca, 1080p60 | 12 Mb/s | 12 Mb/s | brak zysku jakości w H.265; H.264 pozostaje domyślny |
| Balans, 1440p60 | 18 Mb/s | 18 Mb/s | j.w. (12 Mb/s w H.265 daje −2,7 dB względem 18 Mb/s) |
| Kino, 4K30 | 28 Mb/s | 28 Mb/s | j.w. (18 Mb/s w H.265 daje −3,1 dB względem 28 Mb/s) |

Do sprawdzenia dopiero na telewizorze, nie w tabeli: czy obraz w Balansie i Kinie przestaje się zrywać przy mniejszej przepływności (np. Balans 14, Kino 20 Mb/s) w H.265. Mniejsza przepływność
zmniejsza średni ruch w eterze o ok. 25%, ale pomiary wyżej mówią, że kosztuje jakość 1 do 3 dB. Takiej zmiany nie wpisujemy do profili bez potwierdzenia wzrokowego na S85F.

## Domyślny kodek w v0.2.0

**`"h264"`.** Argumenty: (1) pomiary nie pokazują korzyści z H.265 na NVENC w trybie niskiego opóźnienia; (2) H.265 nie był nigdy uruchomiony na prawdziwym telewizorze, a z samej telemetrii
nie da się potwierdzić dekodowania (ACK/NACK i `MEDIA_STATUS` wyglądają tak samo przy złym strumieniu, `PROTOCOL.md` pkt 3); (3) H.264 jest wymagany przez specyfikację odbiorników Cast,
H.265 jest opcjonalny; (4) powrót do H.264 działa, ale kosztuje do 8 s (czas oczekiwania na pierwsze ACK) i jedno mignięcie, więc nie chcemy go fundować każdemu użytkownikowi przy każdym starcie.
„Auto” i „H.265” są dostępne w Ustawieniach → Obraz → Kodek dla tych, którzy chcą spróbować. Zmiana domyślnej wartości na `"auto"` wymaga najpierw próby na S85F, a konfiguracje istniejących
użytkowników i tak są przypięte do `"h264"` przez migrację schematu v3.

## Test na telewizorze (ręcznie, gdy TV jest wolny)

Skrypt odmawia startu, gdy na telewizorze działa inna aplikacja, i zawsze wysyła STOP. **Nigdy bez wiedzy osoby, która może oglądać telewizor, i nigdy z `--force`.**

```bash
export AIRLINX_TV_IP=<adres-telewizora>
# 1) punkt odniesienia: dzisiejszy H.264
.venv/bin/python scripts/smoke_mirror.py --codec h264 --height 1080 --bitrate 12000000 --seconds 15
# 2) to samo w H.265: w logu szukaj "OFFER hevc", potem enc=hevc_nvenc w linii STATS
.venv/bin/python scripts/smoke_mirror.py --codec hevc --height 1080 --bitrate 12000000 --seconds 15
# 3) profile, które się zrywają (porównaj linie VIDEO: retx, nack_msgs, pli, lag)
.venv/bin/python scripts/smoke_mirror.py --codec h264 --height 1440 --bitrate 18000000 --seconds 20
.venv/bin/python scripts/smoke_mirror.py --codec hevc --height 1440 --bitrate 18000000 --seconds 20
.venv/bin/python scripts/smoke_mirror.py --codec hevc --height 2160 --fps 30 --bitrate 28000000 --seconds 20
```

Na co patrzeć:

- **Patrz na ekran telewizora.** ACK-i przychodzą także przy strumieniu, którego TV nie potrafi pokazać. Prawidłowo: obraz pulpitu, bez zielonych bloków i bez zamrożenia.
- W logu: `receiver did not accept hevc ... offering h264` = TV odrzucił H.265 w ANSWER (powrót automatyczny). Po ok. 8 s `HEVC failed (no feedback ...)` = TV przyjął OFFER, ale nie potwierdził ramek
  (powrót automatyczny przez reconnect). Oba przypadki wypisują też `notice: ...`. Dalej `enc=` w STATS mówi, co naprawdę płynie.
- Jeśli obraz jest, a w linii STATS jest `enc=hevc_nvenc`, a zrywanie w Balansie lub Kinie ustąpiło: warto przetestować niższą przepływność (14 i 20 Mb/s). Jeśli nie ustąpiło: nie jest to kwestia kodeka.
- Po teście: `pgrep -x ffmpeg` powinno być puste, a telewizor wraca do ekranu głównego.
- To samo w aplikacji: Ustawienia → Obraz → Kodek → H.265, potem „Udostępnij ekran”; w panelu po prawej widać kodek (`H.265 · NVENC`).

## Ograniczenia pomiaru

Powtórzenie pomiaru rozmiarów klatek dało wyniki różniące się o kilka procent (kodery i generator treści nie są w pełni deterministyczne), wnioski się nie zmieniają. Treści syntetyczne (nie nagranie pulpitu), PSNR i SSIM zamiast VMAF, jedna karta (RTX 3080, NVENC 7. generacji), brak pomiaru po stronie telewizora (dekodowanie, Wi-Fi, opóźnienie wyświetlenia).
Wyniki dla innych kart NVIDIA (np. Ada, gdzie koder HEVC jest znacznie lepszy) mogą być inne: skrypt `scripts/bench_hevc.py` powtarza pomiar w około 10 minut.
