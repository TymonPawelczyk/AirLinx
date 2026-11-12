# Changelog

All notable changes to AirLinx are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] - 2026-11-13

### Added

- H.265 (HEVC) for Cast screen mirroring, chosen in Settings > Obraz > Kodek (Auto, H.264, H.265). The default stays H.264 until H.265 is confirmed on a real TV. H.265 is encoded with NVENC (libx265 on the CPU as a fallback) and offered only when the TV lists it. If the TV rejects it, never acknowledges the stream, or the encoder fails, AirLinx switches to H.264 in the same session.
- Virtual TV (`tools/virtual_tv`): a local Cast and DLNA receiver for end-to-end tests without a TV, with an isolated network namespace, DLNA failure injection and opt-in tests (`pytest -m vtv`).
- `scripts/perf_latency.py`, a latency sweep tool for a real TV, and first measurements in `docs/perf/`.
- `scripts/smoke_mirror.py --window` to share a single window and watch the session end when it closes.

### Changed

- Cast media playback now runs over AirLinx's own authenticated CASTv2 channel instead of pychromecast, so the TV identity check also covers file playback. `pychromecast` and `protobuf` are no longer dependencies.
- File and live stream URLs are bound to the receiver's IP address and expire after a sliding idle period.
- The UI no longer repaints expensive CSS effects under live statistics in WebKitGTK.

### Fixed

- Discovery re-derives the LAN address on every rescan and rebinds SSDP and mDNS, keeps the last good address when only loopback is available, bounds the SSDP scan, filters mDNS addresses, tries several LOCATIONs per host, and no longer marks a TV offline because of one slow description fetch.
- DLNA: live mirror clients are closed on stop, each mirror session uses a unique live stream name, and seeking is relative to the `?t=` start offset.
- The media server answers a malformed `Range` with 416 and a bad `?t=` with 400 instead of HTTP 500.
- Capture: encoder capabilities are published atomically, a probe ffmpeg is killed on timeout or cancel, a crashed ffmpeg is reaped before respawn, and capture objects are single-use.
- A corrupt `known_tvs.json` is backed up and its pins are kept until "trust again"; invalid pin entries are ignored.
- Settings values are validated (ranges, enums and kinds), and a damaged audio-routing state file no longer breaks cleanup after a crash.
- Consent to DLNA without Cast verification is remembered for the playback session and bound to the endpoint, the player and the automatic protocol setting.
- The tray shows media playback instead of a stale mirroring error.
- Menus and dialogs get focus with reduced motion enabled.
- The installer and the test suite require `cryptography>=39`.

## [0.1.0] - 2026-11-10

First public release.

### Added

- Screen mirroring over Google Cast: a whole monitor or a single window, with cursor and sound, using the TV's built-in Cast mirroring receiver (CASTv2 over TLS, AES-CTR encrypted RTP/RTCP). Measured on a Samsung S85F: 1080p at 60 fps, 12 Mb/s, about 111 ms screen to TV.
- Cast device authentication: the TV signs a challenge with its device certificate, the chain is checked against the embedded Cast and Eureka root CAs, and the device identity is pinned on first use (TOFU) in `~/.config/airlinx/known_tvs.json`. A changed identity stops the connection and offers a "trust again" flow.
- DLNA fallback for mirroring (MPEG-TS over HTTP) when Cast mirroring is unavailable.
- Video encoding with NVIDIA NVENC, falling back to libx264 on the CPU for both Cast and DLNA mirroring. The settings screen shows the encoder in use.
- Quality profiles "Gra / praca", "Balans" and "Kino", plus a manual bitrate from 6 to 40 Mb/s. Changing quality or source during a session restarts only the encoder.
- Live statistics (latency, frame rate, bitrate, resolution, RTT) and a per-stage latency breakdown.
- Automatic reconnect (three attempts) while the receiver session is still alive.
- Media playback through the Cast Default Media Receiver or DLNA AVTransport: a queue of local files and URLs, direct play, on-the-fly remux (for example MKV to fragmented MP4 for Cast) or transcode, chosen per file with ffprobe. Pause, seek, next item and TV volume.
- Text subtitles (SRT, ASS, WebVTT) from a sidecar file or embedded MKV tracks, selectable during playback.
- Audio source selection: system output, a microphone, both mixed, or a single application. Single-app capture uses a temporary null sink, and the original routing is restored when streaming stops or on the next start after a crash.
- TV discovery over mDNS and SSDP, with manual entry by IP address.
- A prompt before interrupting an app that is already running on the TV.
- GTK window with a WebKit-hosted UI (Polish), a StatusNotifierItem tray icon, single-instance behaviour, and close-to-tray. Quit always sends STOP to the TV.
- Per-user installer `install.sh` with its own virtual environment, menu entry, icons, optional autostart (`--autostart`) and `--uninstall`.
- Warning when the app runs in a Wayland session, which is not supported.
- Polish landing page in `site/`, terms of use and privacy policy.
- Offline test suite, Playwright e2e tests for the UI mock and the landing page, `install.sh` tests, opt-in live TV tests, and GitHub Actions CI.

### Security

- No fallback from Cast to DLNA after a failed Cast device authentication, a changed TV identity, a denied permission or a pending permission prompt.
- A TV with a pinned Cast identity is never downgraded to unauthenticated DLNA silently: the user is asked first. The same applies when a pinned TV shows up without Cast.
- Failing closed when the pin store (`known_tvs.json`) cannot be read.
- The Cast media receiver is authenticated before it is given a media URL.
- The local media server publishes each file under a random, unguessable token, supports only the methods the TVs need, rejects hostile requests, and unpublishes URLs when playback ends. Tokens are masked in logs.
- `known_tvs.json` is written with mode 0600.

### Known issues

- Only X11 sessions are supported. Wayland is detected and a warning is shown, but screen capture does not work there.
- The "Balans" and "Kino" profiles are experimental and their latency has not been measured.
- Tested on one TV only (Samsung S85F).
- File playback and DLNA mirroring use unencrypted HTTP in the LAN, protected only by a random token in the URL.
- Latency of DLNA mirroring and of the libx264 encoder has not been measured. There is no VAAPI encoder for AMD and Intel GPUs.
- Image-based subtitles (PGS, DVB) are not supported.

[Unreleased]: https://github.com/TymonPawelczyk/AirLinx/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/TymonPawelczyk/AirLinx/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TymonPawelczyk/AirLinx/releases/tag/v0.1.0
