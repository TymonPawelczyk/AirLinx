# Spike: przeglądarka TV (MSE / WebCodecs / WebRTC)

Serwer testowy dla wbudowanej przeglądarki „Internet" na Samsungu S85F (Tizen 9).
Jeden pipeline wideo dla wszystkich testów:
`ffmpeg x11grab (monitor primary) → scale_cuda 1920x1080 → h264_nvenc (p1/ull, bf0, g30, CBR 12M) → fMP4, 1 klatka = 1 fragment`.

| Test | Transport | Dekodowanie na TV |
|---|---|---|
| A `?test=mse` | WebSocket, fMP4 bez zmian | MediaSource + `<video>`, pogoń za krawędzią live (seek + playbackRate) |
| A `?test=mse&audio=1` | jw. + AAC z `*.monitor` (PipeWire) | jw. (`&acodec=opus`: Opus lowdelay) |
| B `?test=webcodecs` | WebSocket, próbki AVCC + avcC | `VideoDecoder` → canvas |
| C `?test=webrtc` | aiortc, RTP/SRTP, H.264 bez re-enkodowania | `RTCPeerConnection`, `jitterBufferTarget=0` |
| `?test=auto` | MSE → WebCodecs → WebRTC po 30 s (`&dur=`) | wyniki `summary-*.jsonl` |

## Co zrobić

1. Na PC: `./run.sh` (wypisze adresy, Ctrl+C kończy i zabija ffmpeg).
2. Na PC otwórz `http://localhost:8090/clock.html` **na monitorze primary (DP-4)** i kliknij, żeby przejść na pełny ekran.
   Pasek czarno-białych pól u góry to zakodowany czas, który TV odczytuje sam (`lat_barcode_ms`). Działa tylko wtedy, gdy zegar zajmuje cały przechwytywany monitor.
3. Na TV: aplikacja Internet → `http://<IP-komputera>:8090/`.
   Raport możliwości wysyła się sam (`results/caps-*.json`, treść leci też na konsolę serwera).
   Potem strzałki ↑↓ + OK albo cyfry 1–6. Najprościej od razu wejść na `http://<IP-komputera>:8090/?test=auto`.
   W teście: Back = menu, OK = włącz dźwięk (MSE+audio).
4. Pomiar zdjęciem: na TV na dole widać „zegar TV" (zsynchronizowany z PC), a w obrazie zegar z PC. Zrób telefonem zdjęcie samego TV. Różnica między dwoma zegarami to opóźnienie od ekranu PC do ekranu TV (łącznie z panelem TV).
5. Statystyki co 5 s trafiają do `results/stats-<sesja>.jsonl`, a na konsoli serwera pojawia się linia `[stats] …`.

Parametry URL: `fps=60`, `br=12` (Mbit/s), `res=1920x1080`, `gop=30`, `profile=high|main|baseline`
(WebRTC domyślnie `baseline`), `hw=prefer-hardware` (WebCodecs), `lo/hi/max` (progi bufora MSE w sekundach, domyślnie 0.07/0.15/0.5), `acodec=aac|opus`.
Zmienne środowiskowe serwera: `CAPTURE=WxH+X+Y` (domyślnie primary z xrandr, teraz `4096x2304+0+420`), `CAPTURE_DISPLAY`, `AUDIO_SOURCE`, `PORT`.

## Jak liczone są opóźnienia

- Zegar: NTP-style po WS `/ws/clock`. Bierzemy 25% próbek o najniższym RTT, z nich medianę offsetu, i synchronizujemy ponownie co 20 s.
- `capture_ms` każdej klatki to wall-clock przechwycenia z x11grab (`-copyts` + filtr `metadata=print`) i jedzie w nagłówku WS.
- MSE: `lat_rvfc_ms` = czas wyświetlenia (rVFC `expectedDisplayTime`) − czas przechwycenia klatki o danym `mediaTime`. `buffer_ahead_ms` = buffered.end − currentTime.
- WebCodecs: `lat_capture_to_decoded_ms` = wyjście z dekodera − przechwycenie.
- WebRTC: `jitter_buffer_ms` (Δ jitterBufferDelay/emitted), `decode_ms`, `processing_ms`, RTT, `srv_capture_to_send_ms` (z `/status`).
- Wszystkie testy: `lat_barcode_ms` = odczyt paska z clock.html w zdekodowanej klatce (wymaga drawImage z video/VideoFrame do canvasu).

## Wyniki lokalne (headless Google Chrome 157 na tym PC, localhost)

Wszystkie 4 ścieżki odbierają i dekodują 1080p60. `srv_capture_to_send` ≈ 30–34 ms we wszystkich (patrz uwagi).

| Test | fps | opóźnienie przechwycenie → wyświetlenie/dekodowanie |
|---|---|---|
| MSE | 60 odebrane / ~50 wyświetlane (headless) | ~85–100 ms (bufor ~50–70 ms) |
| MSE + AAC | 60 | ~210–330 ms (audio dławi bufor) |
| WebCodecs | ~45–55 (dekoder programowy headless, ~60 ms/klatkę) | ~100 ms |
| WebRTC | 60 | ~40 ms (jitter buffer ~1 ms, dekodowanie ~3 ms) |

Kodu paskowego nie dało się sprawdzić end-to-end lokalnie: przechwytywany jest prawdziwy ekran :1, a nie chcieliśmy go zasłaniać.
Przeszedł self-test kodowania i dekodowania (`barcodeSelfTest` w caps). Surowe pliki są w `results/local-headless-chrome/`.

## Znane ograniczenia

- Muxer mp4 z `frag_every_frame` wypuszcza fragment dopiero wtedy, gdy przyjdzie następna klatka, więc dochodzi +1 klatka (~17 ms) we wszystkich testach. W produkcie trzeba to obejść, np. NVENC przez API i własny framing.
- Gdy audio jest w tym samym SourceBuffer, video nie odtworzy się dalej niż kończy się zbuforowane audio (buffered to przecięcie zakresów), a AAC ma opóźnienie enkodera. Wniosek: audio powinno iść osobną ścieżką.
- aiortc ignoruje PLI przy gotowych pakietach. Odzyskiwanie po utracie pakietów następuje dopiero na najbliższej klatce IDR (co 0,5 s).
- Na TV `drawImage(video)` może zwracać czarny obraz, jeśli wideo idzie przez nakładkę sprzętową. Wtedy `lat_barcode_ms` = NaN i rośnie `barcode_bad`, a zostaje metoda ze zdjęciem.
