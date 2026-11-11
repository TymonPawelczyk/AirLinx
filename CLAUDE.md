# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AirLinx is a Linux desktop app (GNOME on **X11**, tested on Zorin OS 17 / Ubuntu 22.04) that mirrors the screen and plays
media files on a TV over the home LAN, using Google Cast (low-latency mirroring and the Default Media Receiver) with a
DLNA fallback. No app is installed on the TV.

## Commands

```bash
.venv/bin/python -m airlinx              # window + tray icon
.venv/bin/python -m airlinx --debug      # DEBUG logs + WebKit inspector (right-click → Inspect)
.venv/bin/python -m airlinx --no-window  # start hidden in the tray

.venv/bin/python -m pytest -q                                    # offline test suite, no TV needed
.venv/bin/python -m pytest tests/test_dlna.py -q                 # one file
.venv/bin/python -m pytest tests/test_dlna.py::test_name -q      # one test
.venv/bin/python -m pytest -q -k cast                            # by keyword
.venv/bin/python -m pytest tests/e2e -m e2e -q                   # Playwright: UI mock + landing page
AIRLINX_TV_IP=<tv-ip> .venv/bin/python -m pytest -m tv tests/live   # real, idle TV only

./install.sh [--autostart] | --uninstall   # per-user install into ~/.local (own venv in ~/.local/share/airlinx)
```

- The venv is `.venv`, created with uv and `--system-site-packages`. PyGObject (Gtk 3, WebKit2 4.1, Gio) comes from
  system Python 3.10, and the PyPI deps are in the `DEPS` array in `install.sh`. There is no pyproject; test and CI deps
  are in `requirements-dev.txt`.
- Type checking uses Pyright with `pyrightconfig.json` (it adds `/usr/lib/python3/dist-packages` and excludes `spikes/`).
- `pytest.ini` restricts collection to `tests/` and deselects the `e2e` and `tv` markers by default (`slow` is the third
  marker); passing your own `-m` replaces that filter. Test layers, setup (`requirements-dev.txt`, `playwright install
  chromium`) and CI (`.github/workflows/ci.yml`) are in `docs/TESTING.md`. `spikes/` holds throwaway prototypes.
- The version lives only in `airlinx/__init__.py` (`__version__`); `airlinx --version` prints it.
- UI without the Python backend: open `ui/index.html` in a normal browser. `ui/bridge.js` then switches to a mock
  (`?mock=nodevices|busy|slow`, `airlinx.mock.*`). Screenshots: `python ui/shoot.py [name]` (Playwright, 1100×720,
  output in `ui/shots/`).

### Virtual TV

`tools/virtual_tv` is a local Cast + DLNA receiver for end-to-end tests without a TV (`pytest -m vtv tests/virtual_tv`, CLI `python -m tools.virtual_tv`, isolated network `tools/virtual_tv/netns.sh`). See `docs/VIRTUAL_TV.md`. Its test root CA is trusted only with `AIRLINX_DEV=1` + `AIRLINX_DEV_CAST_ROOT`, never from settings.

### Live tests against the real TV

`tests/live` (marker `tv`, needs `AIRLINX_TV_IP`) asserts on a real session. The older smoke scripts are
`scripts/smoke_mirror.py` (mirror N seconds, prints states and stats, always sends STOP) and `scripts/smoke_media.py`
(discovery, and with `--play` a 10 s clip via Cast and then DLNA). Rules: keep runs short, and **never pass `--force`**.
The scripts refuse to start when another app is running on the TV, because someone may be watching it. Check for
leftover `ffmpeg` processes afterwards. The dev TV is a Samsung S85F; its address is passed in `AIRLINX_TV_IP` (the smoke scripts also take `--tv` / `--ip`)
and is never written into tracked files.

## Architecture

The authoritative module contract is `docs/ARCHITECTURE.md` (Polish). Change the contract there first. The Cast
mirroring wire protocol is documented in `spikes/cast_streaming/PROTOCOL.md`.

**Threads.** GTK runs on the main thread. All networking and ffmpeg work runs in **one asyncio loop on a background
thread** (`airlinx/runtime.py`: `run_coro()` returns a `concurrent.futures.Future`, `call_in_gtk()` = `GLib.idle_add`).
Modules under `airlinx/core/` are pure asyncio and must never import Gtk.

**UI ⇄ backend.** The UI is static HTML/CSS/JS in `ui/`, hosted in a `WebKit2.WebView` by `app.py`. The window is CSD
with no native header bar, so the HTML draws the title bar. `bridge.py` and `ui/bridge.js` implement JSON RPC
(`airlinx.call(method, params)` → Promise) plus pushed events (`airlinx.on(event, cb)`). `controller.py` owns all app
state and every RPC method. It emits `state` (the full `AppState`), `mirror.state`, `mirror.stats`, `media.*`, `error`
and `toast`. Payloads are `dataclasses.asdict` of the types in `core/models.py`. Adding a feature usually touches
`models.py` → `controller.py` → `ui/app.js`, plus the mock in `ui/bridge.js`.

**Mirroring (Cast).**
- `core/sources.py` lists monitors and windows (xrandr/Xlib).
- `core/capture.py` runs ffmpeg: `x11grab` → `h264_nvenc` (falls back to libx264) emitting FLV, which is parsed into
  H.264 frames. With `mirror.codec` = `auto`/`hevc` (default `h264`) it encodes `hevc_nvenc` (falls back to libx265) as raw
  Annex B split into access units by `HevcParser`; `MirrorSession` offers HEVC only when the TV lists it and falls back to
  H.264 on a rejected ANSWER or an HEVC stream that never gets acknowledged (docs/ARCHITECTURE.md, item 13). Audio → Opus, 10 ms frames, from the source chosen in settings (`core/audio.py`): the default sink's
  monitor, a microphone, both mixed in one ffmpeg (`amix`), or a single app. The single-app mode moves the app's
  sink-inputs to a temporary null sink `airlinx_app`. It records routes in `$XDG_RUNTIME_DIR/airlinx/audio-routes.json`,
  and `cleanup_orphans()` restores them at startup after a crash. Never leave a user's app muted or re-pinned.
  `pactl` runs with `LC_ALL=C` because its JSON output is broken under the Polish locale.
- `core/cast/castv2.py` is the TLS CASTv2 channel with **device authentication**: an AuthChallenge whose signature
  chains to the embedded Cast/Eureka root CAs, with the device identity pinned in `known_tvs.json` (TOFU). The TLS
  certificate itself rotates every ≤4 days, so pin the identity, not the TLS cert. `tv_cert_changed` and
  `tv_auth_failed` are deliberately never retried.
- `core/cast/rtp.py` handles AES-CTR RTP and RTCP.
- `core/cast/mirror.py` (`MirrorSession`) does GET_STATUS → LAUNCH of the mirroring app → OFFER/ANSWER → streaming.
  `stop()` must always send STOP to the TV. `start()` is cancellable, and reconnect only happens while the receiver
  session is still alive. `update()` restarts only the encoder, so the session survives.
- If another app is running on the TV, `start` raises `TvBusyError`. The controller returns
  `{ok:false, code:"tv_busy"}` together with a `retry` payload carrying `force:true`, and the UI asks the user first.

**Fallback.** The controller tries Cast and falls back to DLNA (`DlnaMirrorAdapter`: ffmpeg → MPEG-TS served by
`media_server.py`; NVENC with a libx264 fallback). The fallback is aborted on `permission_pending`, `tv_denied`, `tv_cert_changed` and `tv_auth_failed`.
When the TV has a pinned Cast identity, any other Cast failure returns `cast_unverified` with a `retry` carrying
`protocol:"dlna"` instead of silently downgrading, and the UI asks first (same pattern as `tv_busy`).

**Media files.**
- `core/probe.py` (ffprobe) decides direct / remux / transcode per target.
- `core/media_server.py` (aiohttp, Range + CORS) publishes the file URL. For example, MKV is remuxed to fMP4 on the
  fly for Cast.
- `core/cast/media.py` (Default Media Receiver) and `core/dlna.py` (AVTransport) share one `MediaPlayer` protocol.
- `core/discovery.py` finds TVs via mDNS `_googlecast` plus SSDP MediaRenderer and merges them into `Device` objects
  with `caps` ⊂ {`cast_mirror`, `cast_media`, `dlna`}.

**Shell.**
- `core/sources.py` also detects a Wayland session; the controller publishes it as `AppState.wayland` and the UI shows a
  warning (screen capture needs X11).
- `tray.py` implements StatusNotifierItem + com.canonical.dbusmenu directly over Gio D-Bus (no libappindicator). It
  needs the `zorin-appindicator` / AppIndicator GNOME extension enabled.
- `settings.py` manages `~/.config/airlinx/config.json`, a versioned schema. A corrupt file is moved to
  `config.json.bak` and defaults are used.
- Closing the window hides it to the tray. "Zakończ" (Quit) stops sessions (STOP on TV) and exits.

## Conventions

- User-facing strings are **Polish with full diacritics**. Logs and code are English.
- Design: UI variant "OLED Cinema" (`ui/DESIGN.md`, tokens in `ui/tokens.css`) and brand "Styk"
  (`docs/brand/final/BRAND.md`). `ui/brand-tokens.css` is a copy of the brand tokens and must not be hand-edited.
  The accent colour (`#3DF2A0`) is reserved for the live state, the primary action and the logo's diamond. No CDN or
  network dependencies in `ui/`.
- Branching is **Git Flow**:
  - `main` holds releases only, each tagged `vX.Y.Z`.
  - `develop` is the integration branch.
  - `feature/*` branches start from `develop` and return to it.
  - `release/X.Y.Z` branches from `develop` and merges into both `main` and `develop`.
  - `hotfix/*` branches from `main`.
- Release flow: on `release/X.Y.Z` bump `__version__`, add the `CHANGELOG.md` section and
  `docs/release-notes/vX.Y.Z.md`, merge `--no-ff` into `main`, tag `vX.Y.Z`, merge back into `develop`. Details in
  `CONTRIBUTING.md`; vulnerability reporting in `SECURITY.md`. Commits use Conventional Commits in English.
- The landing page `site/` (Polish) is deployed to GitHub Pages (https://tymonpawelczyk.github.io/AirLinx/) by
  `.github/workflows/pages.yml` on push to `main`. Its repo URL comes only from `data-repo-url` on `<html>`.
- Repo: https://github.com/TymonPawelczyk/AirLinx. Contact is GitHub only (Issues; security via Private Vulnerability
  Reporting). No author e-mail or real name in tracked files, the site or docs.
