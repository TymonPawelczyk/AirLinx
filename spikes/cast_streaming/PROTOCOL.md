# Cast Streaming (Chrome Mirroring) – notatki protokołu

Źródła: openscreen `cast/streaming/impl/{rtp_defines.h, rtp_packetizer.cc, frame_crypto.cc,
rtcp_common.cc, compound_rtcp_parser.cc, sender_report_builder.cc, sender_impl.cc, receiver_impl.cc}`,
`cast/streaming/public/{offer_messages.cc, answer_messages.cc, constants.h}`, `message_fields.h`.
Wszystko poniżej zweryfikowane na żywo z Samsung S85F (cast 1.68), chyba że zaznaczono inaczej.

## 1. Kontrola (CASTv2, TLS :8009)
Ramka = `uint32 BE długość` + protobuf `CastMessage` (STRING payload, JSON). Certyfikat TV self-signed
(uwierzytelnienie urządzenia `deviceauth` jest opcjonalne dla sendera – pominięte).

1. `tp.connection` CONNECT → `receiver-0`; heartbeat `tp.heartbeat` PING/PONG co 5 s.
2. `receiver` `LAUNCH {"appId":"0F5096E8"}` → najpierw `LAUNCH_STATUS {"status":"USER_ALLOWED"}`
   (status jest tu stringiem!), po ~1.7–2.7 s `RECEIVER_STATUS` z app "Chrome Mirroring",
   `transportId == sessionId`, namespaces: `webrtc`, `remoting`, `media`.
3. CONNECT do `transportId`.
4. `urn:x-cast:com.google.cast.webrtc` → OFFER:
```json
{"type":"OFFER","seqNum":4,"offer":{"castMode":"mirroring","receiverGetStatus":true,
 "supportedStreams":[{"index":0,"type":"video_source","codecName":"h264","rtpProfile":"cast",
  "rtpPayloadType":96,"ssrc":<S>,"maxFrameRate":"30000/1000","timeBase":"1/90000",
  "maxBitRate":6000000,"profile":"main","level":"4","targetDelay":100,
  "aesKey":"<32 hex>","aesIvMask":"<32 hex>","receiverRtcpEventLog":true,"channels":1,
  "resolutions":[{"width":1920,"height":1080}]}]}}
```
   Audio (zaimplementowane i zweryfikowane, run8): drugi element `supportedStreams`:
   `{"index":1,"type":"audio_source","codecName":"opus","rtpProfile":"cast","rtpPayloadType":127,
   "ssrc":<A>,"targetDelay":100,"aesKey":..,"aesIvMask":..,"bitRate":128000,"sampleRate":48000,
   "timeBase":"1/48000","channels":2,"receiverRtcpEventLog":true}` – osobny klucz i maska IV.
5. ANSWER (rzeczywisty z TV, < 50 ms po OFFER):
```json
{"answer":{"display":{"dimensions":{"frameRate":"60","height":2160,"width":3840},"scaling":"sender"},
 "sendIndexes":[0],"ssrcs":[<S+1>],"udpPort":59121},"result":"ok","seqNum":4,"type":"ANSWER"}
```
   Brak `constraints` i brak `receiverRtcpEventLog` w odpowiedzi → TV **nie wysyła** logów zdarzeń
   (FrameDecoded/PlayedOut). SSRC odbiornika = SSRC sendera + 1. `udpPort` losowy per sesja.
6. Diagnostyka: `remoting` `GET_CAPABILITIES` → (odpowiedź przychodzi na `webrtc`!)
   `{"capabilities":{"mediaCaps":["aac","opus","vp9","h264","hevc","vp8","audio"],"remoting":2}}`.
   `media` `GET_STATUS` → `MEDIA_STATUS playerState:"PLAYING", contentType:"video/webm", currentTime:0`
   (stałe, niezależne od tego czy obraz się dekoduje). `webrtc` `GET_STATUS` – brak odpowiedzi.
7. Koniec: `receiver` `STOP {"sessionId":...}` → app znika, TV wraca do ekranu głównego.

## 2. Media: UDP, jeden socket na RTP i RTCP
Sender wysyła RTP i RTCP z tego samego portu na `tv:udpPort`; odbiornik odsyła RTCP na port źródłowy.

### Szyfrowanie ramki (frame_crypto.cc)
AES-128-CTR na całej ramce (Annex-B), licznik startowy:
`nonce = 0x00*8 || BE32(frame_id) || 0x00*4`, potem `nonce ^= aesIvMask`; inkrementacja 128-bit BE.

### Pakiet RTP (rtp_packetizer.cc), max 1472 B (1500-20-8)
```
0x80 | M(ostatni pakiet ramki)<<7 | PT | seq16 | rtp_ts32 (90 kHz) | SSRC
B12: K(keyframe)<<7 | 1<<6 (zawsze "jest ref frame id") | liczba rozszerzeń (6 bit)
FID8 (frame_id & 0xff) | PID16 | MaxPID16 | RefFID8 (key: =FID, delta: FID-1)
[opcj. rozszerzenie Adaptive Latency, tylko w pakiecie 0: u16 (1<<10 | 2), u16 playout_delay_ms]
payload: kolejne kawałki zaszyfrowanej ramki, rozmiar = 1472 - 23 (stały, także gdy brak rozszerzenia)
```
frame_id startuje od 0, rośnie o 1; okno niepotwierdzonych ramek ≤ 120 (kMaxUnackedFrames).
H.264: Annex-B, SPS/PPS przed każdym IDR, bez B-frames.

### RTCP sender → receiver
Sender Report (PT 200, RC=0) co ≤ 500 ms (u nas 250 ms) oraz **przed pierwszym RTP** (odbiornik
odrzuca pakiet 0 każdej ramki, dopóki nie dostanie SR): `SSRC, NTP64, RTP ts odpowiadający NTP,
packet count, octet count`. Odbiornik liczy offset zegara = przyjście_SR − NTP_SR (bez korekty RTT),
czas odtworzenia ramki = capture(z mapowania SR+RTP) + offset + targetDelay.

### RTCP receiver → sender (zaobserwowane ~1 pakiet compound na ramkę)
`RR(201, RC=1, report block: LSR/DLSR → RTT)` + `XR(207) RRTR` + `PSFB(206) FMT=15`:
```
receiver SSRC | sender SSRC | 'CAST' | ckpt FID8 | #loss | playout delay ms (u16)
loss[i]: FID8 | PID16 (0xFFFF = cała ramka) | bitvec8 (kolejne 8 PID)
'CST2' | feedback count | #octets | ACK bitvec (bit0 = ckpt+2)
```
PLI = `PSFB FMT=1` (SSRC odbiornika, SSRC sendera) → sender powinien wymusić IDR.
APP(204) FMT=2 'CAST' = receiver frame log – ten TV go nie wysyła.

### Audio (Opus)
- 1 pakiet Opus = 1 ramka Cast, każda jest keyframe (K=1, RefFID = FID), 10 ms (480 próbek) → 100 ramek/s,
  zwykle 1 pakiet RTP. RTP ts = licznik próbek 48 kHz.
- Ten sam socket UDP i ten sam `udpPort` co wideo; odbiornik rozróżnia po SSRC, sender rozdziela
  przychodzące RTCP po SSRC odbiornika (pierwsze słowo po nagłówku pierwszego pakietu compound).
- Osobny Sender Report dla każdego SSRC (ten sam zegar NTP).
- **Synchronizacja / wspólne zero RTP (krytyczne):** RTP ts obu strumieni MUSZĄ liczyć się od
  wspólnego początku sesji (jak openscreen: `capture_time - start_time`), bez losowej bazy.
  Run7 z losowymi bazami: audio ACK 995/995, a wideo stanęło na checkpoint 120 (= kMaxUnackedFrames)
  – renderer TV ciągnął tylko audio, bo traktuje RTP ts jako timestamp mediów. Run8 ze wspólnym zerem:
  wideo 292/292, audio 995/995, lag 0. Wniosek diagnostyczny: `lag` rosnący do ~120 = konsument w TV stoi.
- Czas referencyjny audio = chwila przyjścia pakietu z ffmpeg − 20 ms (szacunek pulse fragment + Opus),
  potem liczony z próbek; przekotwiczenie gdy dryf > 40 ms. Lip-sync **niezmierzony** (wymaga testu
  wzrokowo-słuchowego, np. filmik z "clapperem").

## 3. Zaobserwowane zachowanie TV
- Akceptuje `targetDelay` 50 i 100 ms (echo w polu "playout delay" feedbacku).
- ACK-uje 100% ramek (checkpoint == ostatnia ramka, lag 0) przy 30 fps; NACK tylko przy burstach
  keyframe bez pacingu (Wi-Fi); z pacingiem 80 Mb/s retransmisje = 0.
- **Uwaga:** ACK/NACK/PLI i MEDIA_STATUS wyglądają identycznie przy złym kluczu AES (kontrola
  negatywna `--bad-key`) → z samej telemetrii nie da się potwierdzić, że obraz się dekoduje.

## 4. Decyzje do produkcji

### 4.1 Wymuszanie IDR (odpowiedź na PLI) – rekomendacja: libav w procesie (PyAV) zamiast subprocessu
- Teraz: `ffmpeg` CLI, GOP 120 (4 s). Na PLI nie da się wymusić IDR → po utracie ramki obraz stoi do 4 s.
- **PyAV + h264_nvenc**: `frame.pict_type = I` (dla NVENC z `forced-idr=1` daje IDR) na żądanie,
  zmiana bitrate w locie przez reinit/`av_opt_set` (NVENC obsługuje rekonfigurację bez restartu
  sesji w nowszym FFmpeg), brak parsowania FLV i niejasnych pts. Ryzyko: koła PyAV mają własne
  FFmpeg **bez NVENC** → trzeba zbudować PyAV z systemowym FFmpeg (≥ 5.x/6.x) albo dostarczyć własny.
- Alternatywa niższego poziomu: NVENC SDK bezpośrednio (np. przez `PyNvVideoCodec`) – wymusza IDR,
  intra-refresh i reconfigure bitrate bez kopiowania klatki do CPU (zero-copy z CUDA/NvFBC).
- Rozważyć **intra-refresh** (brak dużych IDR → brak burstów na Wi-Fi, równe opóźnienie); TV
  wymaga jednak przynajmniej jednego keyframe na start i po PLI.

### 4.2 Przechwytywanie szybsze niż x11grab 4K
- Pomiar: x11grab 4096×2304 + skalowanie CPU ≈ 12–36 ms i maks. ~35 fps (przy `--fps 60`).
- Kolejność preferencji:
  1. **X11: NvFBC** (NVIDIA, GPU→GPU, zero-copy do NVENC; na GeForce wymaga sterownikowego "unlock"/
     flagi – sprawdzić licencję) lub **XShm + XDamage** z kopiowaniem tylko zmienionych regionów.
  2. **Wayland/X11 z PipeWire: xdg-desktop-portal ScreenCast** → strumień DMA-BUF → import do CUDA
     (EGL/`cuGraphicsEGLRegisterImage`) → skalowanie `scale_cuda`/NPP → NVENC. Jedyny przyszłościowy
     wariant (GNOME/Wayland), wymaga zgody użytkownika w portalu.
  3. `kmsgrab` (FFmpeg) – szybki, ale wymaga CAP_SYS_ADMIN → odpada dla aplikacji użytkownika.
- Skalowanie na GPU (`scale_cuda` już działa w lokalnym FFmpeg 4.4) zamiast `swscale`.

### 4.3 Adaptacyjny bitrate (ABR)
- Sygnały dostępne z RTCP TV (logów zdarzeń brak): RTT (RR LSR/DLSR), NACK-i i retransmisje,
  czas send→ACK ramek, `fraction_lost`/`cum_lost` z report blocka, rosnący `lag` (ramki w locie).
- Proponowany algorytm (jak openscreen `bandwidth_estimator.cc`): estymata przepustowości z bajtów
  potwierdzonych w oknie ~1 s ÷ czas; cel = 0.7–0.8 × estymata; szybki spadek (×0.7) przy NACK-ach
  lub send→ACK p90 > ½ targetDelay; wolny wzrost (+5 %/s) gdy czysto. Zakres 2–20 Mb/s.
- Zmiana bitrate na żywo wymaga 4.1 (libav/NVENC reconfigure); z subprocessem tylko restart.
- Zmiana **rozdzielczości**: nowy OFFER w tej samej sesji (Chrome tak robi przy zmianie źródła) lub
  zmiana rozdzielczości w strumieniu H.264 przy IDR z nowym SPS – do sprawdzenia, czy TV przyjmie
  zmianę SPS bez renegocjacji.

### 4.4 Pozostałe
- Wszystkie strumienie: jeden socket, pacing (≥ 80 Mb/s dla dużych ramek) – bez niego Wi-Fi TV gubi
  burst keyframe'a.
- Kickstart (powtórka ostatniego pakietu ostatniej ramki, gdy brak feedbacku) – potrzebne przy
  statycznym ekranie, jeśli enkoder przestanie produkować ramki (przy capture z XDamage).
- Przed LAUNCH: GET_STATUS i odmowa, jeśli działa inna aplikacja (zaimplementowane, `--force`).
- TLS bez weryfikacji: dla produkcji dodać `deviceauth` (weryfikacja certyfikatu urządzenia Cast).
