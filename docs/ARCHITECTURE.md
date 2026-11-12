# AirLinx — architektura i kontrakt modułów

Źródło prawdy dla agentów budujących aplikację. Zmiana kontraktu = najpierw zmiana tego pliku (przez team-leada).

## Fakty środowiska
- Zorin OS 17.3 (Ubuntu 22.04), GNOME **X11**, Python systemowy 3.10 + PyGObject (Gtk 3, WebKit2 4.1, GLib/Gio). Venv: `.venv` (uv, `--system-site-packages`) — zeroconf, aiohttp, async-upnp-client, cryptography.
- Monitory: `DP-4` 4096×2304+0+420 (główny), `DP-2.8` 5120×2880+4096+0. GPU RTX 3080: ffmpeg `h264_nvenc`/`hevc_nvenc`. GStreamer bez plugins-bad — **nie używać**.
- Tray: GNOME z `zorin-appindicator` → **StatusNotifierItem + com.canonical.dbusmenu przez Gio D-Bus** (czysty Python, bez libappindicator).
- TV: Samsung S85F (adres w `AIRLINX_TV_IP`) — Google Cast (mirroring app `0F5096E8` ✅ zweryfikowany, Default Media Receiver `CC1AD845`), DLNA DMR `http://<tv-ip>:9197/dmr`.
- Działający prototyp mirroringu: `spikes/cast_streaming/` (+ `PROTOCOL.md` — przeczytać!).

## Układ pakietu
```
airlinx/
  __main__.py            # python -m airlinx  [--debug] [--no-window]
  app.py                 # Gtk.Application, okno, cykl życia        (shell)
  bridge.py              # JS ⇄ Python RPC + eventy                 (shell)
  tray.py                # SNI + dbusmenu                            (shell)
  settings.py            # ~/.config/airlinx/config.json (schemat v3)  (shell)
  runtime.py             # wątek asyncio + helpery GLib<->asyncio    (shell)
  controller.py          # orkiestrator: stan aplikacji, API dla UI  (shell)
  core/
    models.py            # dataclasses współdzielone (poniżej)        (shell — tworzy jako pierwszy)
    discovery.py         # mDNS _googlecast + SSDP MediaRenderer      (media)
    sources.py           # monitory (xrandr) i okna (wmctrl/xprop/Xlib) + miniatury   (cast)
    capture.py           # ffmpeg: x11grab→h264_nvenc | hevc_nvenc (libx264/libx265 zapasowo), pulse (1 źródło lub miks 2)→opus  (cast)
    audio.py             # źródło dźwięku: system / mikrofon / system+mikrofon / aplikacja (pactl)
    cast/castv2.py       # TLS CASTv2 kanał (z spike)                 (cast)
    cast/rtp.py          # AES-CTR, pakietyzacja, RTCP (z spike)      (cast)
    cast/mirror.py       # MirrorSession                              (cast)
    cast/media.py        # CastMediaPlayer (Default Media Receiver) na zweryfikowanym castv2.CastChannel   (media)
    dlna.py              # DlnaPlayer (AVTransport) + DlnaMirror fallback (MPEG-TS)  (media)
    media_server.py      # aiohttp: pliki z Range+CORS, transkodowanie NVENC, live TS (media)
    probe.py             # ffprobe → decyzja: direct / remux / transcode  (media)
ui/                      # HTML/CSS/JS z docs/design/final, podpięte do bridge     (ui)
data/                    # airlinx.desktop, ikony hicolor, autostart               (shell)
tests/                   # pytest; testy offline bez TV
install.sh               # instalacja per-user (~/.local)                          (shell)
```
Właściciele: **shell**, **cast**, **media**, **ui** — każdy agent edytuje tylko swoje pliki. `core/models.py` tworzy shell; inni mogą prosić o zmiany przez team-leada (lub dopisać pola na końcu dataclass, bez łamania).

## Współbieżność
- GTK na wątku głównym. Cała sieć/ffmpeg w **jednej pętli asyncio w wątku tła** (`runtime.py`: `run_coro(coro) -> concurrent.futures.Future`, `call_in_gtk(fn, *a)` = `GLib.idle_add`).
- Moduły core są czysto asyncio, nie importują Gtk.

## Modele (`core/models.py`)
```python
@dataclass class Device:      id: str; name: str; model: str; ip: str
                              cast_port: int|None; dlna_location: str|None
                              caps: set[str]   # {"cast_mirror","cast_media","dlna"}
                              online: bool = True
@dataclass class Source:      id: str; kind: Literal["monitor","window"]; name: str
                              geometry: tuple[int,int,int,int]  # x,y,w,h
                              xid: int|None = None; thumbnail_png: bytes|None = None
@dataclass class MirrorOptions: preset: Literal["game","cinema"]="game"
                              max_height: int = 1080; fps: int = 60; bitrate: int = 12_000_000
                              audio: bool = True; target_delay_ms: int = 100
                              codec: Literal["auto","h264","hevc"] = "h264"   # pkt 13
                              audio_source: str = "system"   # "system"|"mic"|"system_mic"|"app"
                              audio_mic: str = "default"; audio_app: str|None = None
                              audio_mic_name: str|None = None; audio_app_name: str|None = None
@dataclass class Stats:       latency_ms: float|None; fps: float; bitrate: float; width: int; height: int
                              rtt_ms: float|None; loss: float; protocol: str; encoder: str
                              stages: dict[str,float]  # capture/encode/network/tv (ms, szacunki)
@dataclass class MediaItem:   id: str; path_or_url: str; title: str; duration: float|None
                              kind: Literal["video","audio","image"]; thumbnail_png: bytes|None = None
                              subtitles: list[str] = field(default_factory=list)
@dataclass class PlaybackState: state: Literal["idle","loading","playing","paused","buffering","ended","error"]
                              position: float = 0; duration: float|None = None; muted: bool=False
                              volume: float|None = 1.0   # None = player nie zna głośności (np. błąd DLNA, Samsung GetVolume=0);
                                                         #        controller zostawia wtedy głośność użytkownika
                              item_id: str|None = None; error: str|None = None
```
`AppState` (event `state`) ma też `version`, `encoder` i `wayland`: patrz „Rozszerzenia kontraktu”, pkt 12.

## Interfejsy core (async)
```python
# discovery.py (media)
class Discovery:
    def __init__(self, on_change: Callable[[list[Device]], None]): ...
    async def start(self) -> None; async def stop(self) -> None
    async def rescan(self) -> None
    async def add_manual(self, ip: str) -> Device | None
    devices: list[Device]

# sources.py (cast)
async def list_sources(with_thumbnails: bool = True) -> list[Source]

# cast/mirror.py (cast)
class MirrorSession:
    def __init__(self, device: Device, source: Source, opts: MirrorOptions,
                 on_stats: Callable[[Stats], None], on_state: Callable[[str, str|None], None]): ...
        # on_state(state, detail): "connecting"|"waiting_tv"|"live"|"reconnecting"|"stopped"|"error"
    async def start(self) -> None          # rzuca TvBusyError, gdy na TV działa inna aplikacja i nie force
    async def stop(self) -> None           # ZAWSZE STOP aplikacji na TV
    async def update(self, opts: MirrorOptions) -> None   # bitrate/audio/preset w locie (restart enkodera ok)
    async def switch_source(self, source: Source) -> None

# cast/media.py (media)  i  dlna.py (media) — ten sam protokół:
class MediaPlayer(Protocol):
    async def load(self, item: MediaItem, url: str, mime: str, start: float = 0) -> None
    async def play(self) -> None; async def pause(self) -> None; async def stop(self) -> None
    async def seek(self, seconds: float) -> None; async def set_volume(self, v: float) -> None
    on_state: Callable[[PlaybackState], None]

# media_server.py (media)
class MediaServer:
    async def start(self, host: str = "0.0.0.0", port: int = 0) -> None   # port w self.port
    def publish(self, item: MediaItem, target: Literal["cast","dlna"]) -> tuple[str, str]  # (url, mime); decyzja z probe.py
    async def stop(self) -> None
```
Błędy: `core/models.py` definiuje `AirlinxError(code, message_pl)`, `TvBusyError`, `TvNotFoundError`, `PermissionPendingError`.

## Bridge JS ⇄ Python (`bridge.py`, kontrakt dla ui)
- JS → Py: `window.webkit.messageHandlers.airlinx.postMessage({id, method, params})`.
- Py → JS: `window.airlinx._resolve(id, result|null, error|null)` oraz `window.airlinx._emit(event, payload)`.
- `ui/bridge.js` udostępnia `airlinx.call(method, params) -> Promise` i `airlinx.on(event, cb)`. Poza WebKit (zwykła przeglądarka) używa **mocka** z fałszywymi danymi — UI musi działać standalone do developmentu.

Metody (controller.py): `app.getState`, `devices.list`, `devices.rescan`, `devices.addManual{ip}`, `devices.select{id}`,
`sources.list`, `mirror.start{sourceId, options}`, `mirror.stop`, `mirror.update{options}`, `mirror.switchSource{sourceId}`,
`media.add{paths|urls}`, `media.pickFiles` (natywny Gtk.FileChooser), `media.remove{id}`, `media.reorder{ids}`, `media.play{id}`,
`media.pause`, `media.resume`, `media.seek{seconds}`, `media.volume{value}`, `media.next`, `media.prev`, `media.stop`, `media.subtitles{id, track}`,
`settings.get`, `settings.set{patch}`, `audio.list`, `window.minimizeToTray`, `app.quit`.

Eventy: `state` (pełny AppState JSON), `devices`, `mirror.state`, `mirror.stats` (≤2/s), `media.queue`, `media.playback`, `error {code, message}`, `toast {message}`.
Wszystkie payloady to JSON z dataclass (`dataclasses.asdict`, bytes → `data:image/png;base64,...`).

## Zasady
- Teksty dla użytkownika po polsku z diakrytykami. Logi po angielsku.
- Przed `LAUNCH` na TV: `GET_STATUS`; jeśli działa inna aplikacja → `TvBusyError` (UI pyta „Przerwać to, co leci na TV?”) → `start(force=True)`.
- Zamknięcie okna = schowanie do tray (konfigurowalne). `Zakończ` = zatrzymaj sesje (STOP na TV) i wyjdź.
- Testy offline (pytest) dla wszystkiego, co da się bez TV. Testy na TV: krótkie, ze STOP, tylko gdy TV wolny.

## Rozszerzenia kontraktu (v0.1, od zespołu shell)
1. `models.py`: `MirrorStatus`, `AppState` (kształt eventu `state` i `app.getState`, snake_case); `MediaItem.subtitle_track`.
2. Lokalne metody bridge'a (wątek GTK, poza controllerem): `window.startDrag{button}`, `window.minimize`, `window.toggleMaximize`, `window.close` (= schowaj do tray wg ustawienia), `window.isNative`. Skróty w `bridge.js`: `airlinx.startDrag(e)/minimize()/toggleMaximize()/close()`. Okno CSD bez natywnego nagłówka — UI rysuje własny pasek tytułu.
3. `tv_busy`: `mirror.start`/`media.play` NIE odrzucają promisa — zwracają `{ok:false, code:"tv_busy", message}` i emitują `error{code:"tv_busy", message, app, retry:{method, params:{..., force:true}}}`. UI pyta „Przerwać?” i woła `retry.method(retry.params)`.
4. Drag&drop w oknie natywnym obsługuje Python (`media.add` → `media.queue`). UI w trybie `airlinx.native` robi tylko `preventDefault` na drop.
5. DLNA mirror: `DlnaMirrorAdapter` w controllerze (ffmpeg x11grab→H.264→mpegts) o interfejsie `MirrorSession`. Koder jest wybierany w `start()` tak jak w Cast,
   z tej samej sondy `core.capture.probe_encoders()` (`encoder_from_caps`): `h264_nvenc`, gdy działa, inaczej `libx264` (`ultrafast`, `zerolatency`); brak obu →
   `AirlinxError("no_encoder")`. Zmienna `AIRLINX_DLNA_ENCODER` nadpisuje wykrywanie (sonda jest wtedy pomijana). Wybrany koder trafia do `Stats.encoder`.
6. Media API (zespół media): `MediaServer.publish_async`, `subtitle_tracks`, `poster_url`, `info`, `unpublish`; `CastMediaPlayer.load(..., force, subtitles, poster_url, active_subtitle)`, `set_subtitle`, `set_muted`, `close`; `DlnaPlayer.load(..., force, poster_url, subtitle_url)`.
7. `devices.trustAgain{id}` → `{ok, removed}`: woła `core.cast.castv2.forget_tv_cert(id)` (oraz po IP urządzenia). Przy `tv_cert_changed` controller emituje `error{code:"tv_cert_changed", message, device_id, device_name, retry:{method:"devices.trustAgain", params:{id}}}`; UI pokazuje dialog „Zaufaj ponownie temu telewizorowi”.
8. `mirror.start` jest anulowalny: `mirror.stop`/`app.quit` anulują trwający handshake od razu (bez czekania na lock); anulowany `mirror.start` zwraca `{ok:false, code:"cancelled"}`, `mirror.state` → `idle` z `detail:"Anulowano"`. `mirror.stop` zwraca `{ok, cancelled}`.
9. Fallback Cast→DLNA przerywany dla `permission_pending`, `tv_denied`, `tv_cert_changed`, `tv_auth_failed`; błędy nieudanej próby Cast nie są emitowane, dopóki zostały protokoły do sprawdzenia.
10. **Źródło dźwięku** (`core/audio.py`, ustawienia schemat v2). Przełącznik `mirror.audio` = dźwięk w ogóle; wybór źródła w sekcji `audio`:
    `audio.source ∈ {system, mic, system_mic, app}`, `audio.mic` (nazwa źródła pulse albo `"default"` = `@DEFAULT_SOURCE@`), `audio.mic_name`,
    `audio.app` (stabilny klucz aplikacji: `application.process.binary`, inaczej `application.name`), `audio.app_name`. Migracja v1→v2: `audio.source="system"`.
    Sekcja `mirror` w configu nie zawiera pól `audio_*` (kontroler je wycina i scala z `audio` przy starcie).
    - `audio.list` → `{mics:[{name,label,default}], apps:[{key,name,icon,icon_url,streams}], default_mic}` — mikrofony bez `*.monitor`, aplikacje, które teraz grają
      (bez dźwięków systemowych `media.role=event`); `icon_url` to data URL ikony z motywu (lub `null`). UI odświeża listę przy każdym otwarciu zakładki Dźwięk.
    - `settings.set{patch:{audio:{…}}}` w trakcie transmisji: Cast (`MirrorSession.live_audio_switch = True`) restartuje tylko przechwytywanie dźwięku
      (ten sam strumień RTP, bez nowego OFFER); DLNA (`live_audio_switch = False`) zostawia bieżące źródło, toast „Nowe źródło dźwięku zadziała przy następnym udostępnianiu.”,
      `mirror.audio.pending = true`. `mirror.update{options}` przyjmuje też pola `audio_*` (zapisywane do `audio`).
    - `MirrorStatus.audio` (w `mirror.state`/`state`): `null` (bez dźwięku / bez sesji) albo `{source, requested, label, note, pending?}` — `source` to to, co naprawdę idzie
      (po fallbacku), `requested` to wybór użytkownika, `label` np. „Dźwięk systemu” / „Mikrofon: Blue Yeti” / „Tylko Firefox”, `note` = polski komunikat.
    - Fallback: brak wybranego mikrofonu lub aplikacja nic nie gra przy starcie → dźwięk systemu, toast i `note` (np. „Firefox nic teraz nie odtwarza. Wysyłam dźwięk systemu.”).
      Mirroring nie jest przerywany. Ustawienie użytkownika zostaje bez zmian.
    - Sesje mają opcjonalny atrybut `on_notice(msg)` (kontroler go ustawia): komunikaty z rdzenia (np. „Dźwięk na TV przestał działać. Obraz leci dalej.”) → toast + `mirror.audio.note`.
    - Tryb `app`: tymczasowy `module-null-sink` `airlinx_app`, sink-inputy aplikacji przeniesione `move-sink-input`, nowe strumienie wyłapywane przez `pactl subscribe`
      (zapasowo polling co 2 s), przechwytywany `airlinx_app.monitor`. Aplikacja nie gra wtedy z głośników PC. Sprzątanie (stop, błąd, wyjątek, anulowanie, koniec sesji
      na TV, `app.quit`): strumienie wracają na pierwotne sinki (zniknięty sink → domyślny), pin WirePlumbera jest czyszczony (`pw-metadata -d <id> target.node`) dla strumieni,
      które szły na domyślne wyjście, moduł jest wyładowany. Trasy są zapisywane w `$XDG_RUNTIME_DIR/airlinx/audio-routes.json`; `Controller.start()` woła
      `cleanup_orphans()`, które po crashu przywraca strumienie i wyładowuje moduły `airlinx_*`.
    - Tryb `system_mic`: jeden ffmpeg z dwoma wejściami pulse, `aresample=async=1000` na każdym (dryf zegarów kart) i `amix=inputs=2:duration=longest:normalize=0`
      → jeden strumień Opus, znaczniki czasu z liczby próbek wyjścia (jak dotąd).
    - DLNA mirror (`DlnaMirrorAdapter`) używa tego samego `AudioPipeline` (system = monitor domyślnego wyjścia, nie domyślne wejście jak wcześniej).
11. **Bez cichego downgrade Cast → DLNA.** Jeśli urządzenie ma przypiętą tożsamość Cast (wpis w `known_tvs.json` pod `device.id` lub IP), nieudana próba Cast
    (np. zamknięty port 8009) NIE przechodzi automatycznie na DLNA, bo DLNA nie ma żadnej weryfikacji. `mirror.start` / `media.play` zwracają
    `{ok:false, code:"cast_unverified", message}` i emitują `error{code:"cast_unverified", message:"Nie udało się potwierdzić tożsamości telewizora przez Google Cast.
    Wysłać przez DLNA bez weryfikacji?", retry:{method, params:{..., protocol:"dlna"}}}` (wzorzec jak `tv_busy`). UI pyta (domyślnie „Anuluj”) i woła retry.
    `mirror.start{…, protocol}` i `media.play{…, protocol}` przyjmują `"cast"|"dlna"`: wymusza jeden protokół bez fallbacku. Urządzenia bez pinu
    i ustawienie protokołu „DLNA” działają jak dotąd. Mock: `?mock=unverified`.
    To samo pytanie pada, gdy w trybie „auto” plan protokołów nie ma Cast (urządzenie widoczne tylko przez DLNA, np. sfałszowane
    lub brakujące mDNS), a urządzenie jest przypięte. Plik `known_tvs.json`, który istnieje, ale nie da się go odczytać, liczy się jako pin (fail closed).
12. **Wersja, wykryty koder, sesja Wayland (v0.1.0).**
    - Jedno źródło wersji: `airlinx.__version__` (`airlinx/__init__.py`). Korzystają z niego `AppState.version` (UI: Ustawienia → „AirLinx 0.1.0”), `controller.VERSION`,
      `airlinx --version` oraz nagłówek `USER-AGENT` w SSDP. Wydanie = zmiana tej jednej linii.
    - `AppState.encoder: str|None`: koder H.264, którego użyje kopia ekranu. `None` = jeszcze nie sprawdzono, `"h264_nvenc"`, `"libx264"`, `""` = ffmpeg nie ma żadnego.
      `Controller.start()` uruchamia w tle jedną sondę (`Factories.encoders`, domyślnie `core.capture.probe_encoders`) i po niej emituje `state`; błąd sondy zostawia `None`.
      UI pokazuje to w Ustawienia → Jakość (w trakcie transmisji pierwszeństwo ma `mirror.stats.encoder`). Mock: `?mock=cpu` (= `libx264`).
    - `AppState.wayland: bool`: `core.sources.is_wayland_session(os.environ)`, liczone raz w konstruktorze kontrolera (`XDG_SESSION_TYPE`, a gdy pusty, niepusty
      `WAYLAND_DISPLAY`). W sesji Wayland x11grab daje czarny obraz, więc UI pokazuje w trybie Ekran stałą notę (`#wayland-note`, `data-wayland` na `.app`), a kontroler
      loguje ostrzeżenie. Niczego nie blokujemy: odtwarzanie plików działa normalnie. Mock: `?mock=wayland`.
    - Nowych zdarzeń nie ma: oba pola idą w istniejącym `state` / `app.getState`.
    - Profile Balans (1440p) i Kino (4K) są w UI oznaczone jako eksperymentalne i nie mają obiecanego opóźnienia w ms; liczba (około 110 ms) jest tylko przy Gra / praca.
    - Pliki `.desktop`: `install.sh` (`desktop_exec`) i `app.desktop_exec_quote` zapisują `Exec=` w cudzysłowach wg Desktop Entry Spec, więc `$HOME` ze spacją nie dzieli
      polecenia. Mock domyślnie ma jeden telewizor (`?mock=multi` dodaje drugi).
13. **Kodek wideo: H.264 / H.265 (v0.2.0, `mirror.codec`).**
    - Ustawienie `mirror.codec ∈ {"auto","h264","hevc"}` (`MirrorOptions.codec`, schemat ustawień **v3**). **Domyślnie `"h264"`**, bo HEVC nie był jeszcze sprawdzony na
      prawdziwym telewizorze (S85F); `"auto"` i `"hevc"` są opt-in. Migracja v2→v3 dopisuje `"h264"` istniejącym konfiguracjom, więc późniejsza zmiana domyślnej wartości ich nie ruszy.
      Nieznana wartość (plik, RPC `mirror.update`, `settings.set`) wraca do `"h264"`. Zmiana w trakcie transmisji to zmiana strukturalna (`is_structural_change`): nowy OFFER, restart kodera.
    - Wybór kodeka (`cast/mirror.py: plan_video(pref, caps, tv_codecs, exclude)`) daje uporządkowaną listę `(kodek, koder ffmpeg, gpu_scale)`, a ostatni element to plan B:
      `h264` → tylko H.264. `auto` → HEVC pierwszy tylko gdy ffmpeg ma koder (`hevc_nvenc`, inaczej `libx265`) **i** TV wymienia `"hevc"` w `mediaCaps`
      (brak odpowiedzi = nieznane = H.264). `hevc` → HEVC pierwszy, chyba że lista TV go nie zawiera (brak odpowiedzi = próbujemy). H.264 zostaje planem B, gdy jest jego koder.
    - Możliwości TV: `GET_CAPABILITIES` na `remoting` (odpowiedź przychodzi na `webrtc`, `capabilities.mediaCaps`), tylko gdy HEVC jest kandydatem; limit `CAPS_TIMEOUT` = 2 s.
    - OFFER: `codecName:"hevc"`, `profile:"main"`, `level` z `hevc_level(w,h,fps)` ("4.1" do 1080p60, "5.0" do 1440p60 i 4K30, "5.1" 4K60). `codecParameter` pomijamy celowo: jest opcjonalny,
      openscreen wymaga prefiksu `hev1`, a Chrome go nie wysyła. Reszta pól jak dla H.264 (`rtpPayloadType` 96, `timeBase` 1/90000).
    - **Powrót do H.264 (automatyczny, ta sama sesja Cast, bez nowego LAUNCH):** (a) ANSWER odrzuca strumień wideo albo nie przychodzi w 10 s → drugi OFFER z H.264 i toast „Telewizor nie przyjął H.265. Używam H.264.”;
      (b) TV przyjął OFFER, ale nie potwierdza ramek (`STALL_TIMEOUT_START`) albo koder HEVC pada → `_hevc_failed`: `stall` → reconnect z H.264 (i toast „H.265 nie działa z tym telewizorem. Przełączam na H.264.”);
      TV trafia do `mirror._hevc_bad` (pamięć procesu), więc `auto` nie próbuje HEVC drugi raz; jawne `hevc` zawsze próbuje. Bez planu B (brak kodera H.264) błąd kodera jest zwykłym `fatal`.
    - Przechwytywanie (`core/capture.py`): ffmpeg 4.4 nie zapisuje HEVC w FLV, więc koder pisze surowy Annex B (`-f hevc`, `-aud 1` / x265 `aud=1:repeat-headers=1`), a `HevcParser` tnie go na access units po AUD
      (NAL 35). `key` = slice IRAP (NAL 16–21); klatka kluczowa ma zawsze VPS/SPS/PPS (brakujące dopisuje z pamięci); puste jednostki są odrzucane; bajty przed pierwszym AUD
      i ogon klatki przeciętej `flush()` są odrzucane (`discarded_bytes`). Surowy strumień nie ma znaczników czasu: czas przechwycenia = moment, w którym w paczce pojawił się początek AU, minus `ENCODE_FLOOR_S`,
      rosnąco (H.264 liczy go z FLV jak dotąd). AU kończy się dopiero przy następnym AUD, więc czytnik po `HEVC_IDLE_FLUSH_S` (3 ms ciszy w rurze) oddaje oczekującą jednostkę, żeby nie dokładać klatki opóźnienia.
      Parametry kodera jak dla H.264 (`p1`, `tune ull`, CBR, bez B-klatek, GOP 2 s, `forced-idr`); ten sam mechanizm restartu kodera na PLI / zmianę bitrate.
    - `cast/rtp.py` nie zakłada kodeka: pakietyzacja jest na poziomie ramek (flaga `K` = klatka kluczowa, `RefFID`), a szyfrowanie i SR są takie same.
    - Sonda `probe_encoders()` zwraca też `hevc_nvenc` i `x265`. `AppState.hevc_encoder: str|None` (`"hevc_nvenc"`, `"libx265"`, `""` = brak, `None` = jeszcze nie sprawdzono) obok `encoder` (pkt 12).
      `Stats.encoder` zawsze mówi, co naprawdę koduje (np. `"hevc_nvenc + scale_cuda"`), więc UI pokazuje faktyczny kodek także po powrocie do H.264. DLNA mirror zawsze używa H.264.
    - UI: Ustawienia → Obraz → „Kodek” (segment Auto / H.264 / H.265, `data-group="codec"`) z opisem po polsku; linia „Koder wideo” nazywa koder H.265. Mock: `?mock=nohevc` (brak kodera), `?mock=tvnohevc` (TV odrzuca HEVC).
    - Pomiary i rekomendacje bitrate dla HEVC: `docs/perf/hevc.md`. Skrypt: `scripts/bench_hevc.py`. Test na prawdziwym TV (ręcznie): `docs/perf/hevc.md`, sekcja „Test na telewizorze”.
