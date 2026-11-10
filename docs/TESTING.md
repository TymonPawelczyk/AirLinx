# Testing

Five layers, from fast and offline to real hardware. The default `pytest -q` runs only the first.

| Layer | Where | Needs | Run | CI |
|---|---|---|---|---|
| Unit and component (offline) | `tests/test_*.py` | Python deps, PyGObject, ffmpeg (some tests skip without it) | `pytest -q` | yes, with coverage |
| UI e2e on the mock bridge | `tests/e2e/test_ui.py` | Playwright + Chromium | `pytest tests/e2e -m e2e` | yes |
| Landing page e2e | `tests/e2e/test_site.py` | Playwright + Chromium | `pytest tests/e2e -m e2e` | yes |
| `install.sh` in a throwaway `$HOME` | `tests/test_shell_install.py` | bash | `pytest -q` | yes |
| Live TV | `tests/live/` | a real, idle TV | `AIRLINX_TV_IP=<tv-ip> pytest -m tv tests/live` | no, local only |

## Setup

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m playwright install chromium     # only for e2e
```

## Markers (pytest.ini)

- `e2e`: Playwright tests. Deselected by default; `-m e2e` selects them. They skip, not fail, when Chromium is missing.
- `tv`: needs a real TV. Deselected by default; run with `-m tv` and `AIRLINX_TV_IP`, otherwise they skip.
- `slow`: more than a few seconds. Deselect with `-m "not slow"`.

The default `addopts` is `-m "not e2e and not tv"`, so passing your own `-m` replaces it. Use `-m "e2e or not tv"` to run everything except the TV.

## What each layer is for

- **Unit and component.** Protocol parsers (RTP/RTCP, CASTV2, SSDP, UPnP XML, mDNS, DIDL, ffprobe), `media_server` (tokens, Range, hostile requests), the controller and `MirrorSession` against in-process fake receivers, audio routing against a fake `pactl`, settings migration, capture crash handling with stand-in processes, `sources` against stub X tools. No network, no TV, no X server. A test that cannot fail when the code is wrong does not belong here: prefer a fake that behaves like the real peer, and check outcomes (what the TV got, what was cleaned up) over call counts.
- **UI e2e.** `ui/index.html` opened over a local HTTP server, using the mock in `ui/bridge.js` (`?mock=nodevices|busy|slow|noapps|nomic`, `airlinx.mock.*`). Selectors are roles, aria-labels and the `data-screen` / `data-session` / `data-mode` hooks on `.app`. Nothing sleeps: tests wait for states. Every test also fails on console errors and on requests to anything but the local server.
- **Landing e2e.** `site/` served locally: no console errors or third-party requests, no horizontal scroll at 360 and 1440 px, anchors, copy buttons, theme persistence, the repository URL (the shipped page links to the public repo; tests rewrite `data-repo-url` on `<html>` to check that the `{{REPO_URL}}` placeholder stays inert, that another URL is substituted everywhere and that a `javascript:` value never becomes a link), alt text, and a hand-written a11y audit (landmarks, headings, accessible names, focus). axe-core is not bundled, to keep the run offline.
- **install.sh.** Real runs against a temporary `$HOME` with `AIRLINX_SKIP_DEPS=1` and `AIRLINX_PYTHON`: files, launcher, `.desktop` entries, icons, autostart, idempotence, and that `--uninstall` removes exactly what was installed and nothing else. One `slow` test builds the real own venv.
- **Live TV.** One 12 s mirror session and one Cast plus DLNA playback, each asserted: device-auth succeeded and the identity was pinned, live within 15 s, at least 50 fps, at least 95% of audio frames acked, STOP leaves the TV idle, no orphaned `ffmpeg`. Safety rules: never `force`; skip when anything runs on the TV or any `ffmpeg` already runs on this PC; the pin file is a temp file so the real `known_tvs.json` is untouched. Check `pgrep ffmpeg` afterwards anyway.

## Coverage

```bash
.venv/bin/python -m pytest -q --cov=airlinx --cov-report=term-missing
```

CI uploads `coverage.xml` and `htmlcov/` as the `coverage-report` artifact. There is no percentage gate on purpose: `app.py`, `tray.py` and `runtime.py` are GTK glue that only a display can exercise. Watch the logic modules (`core/*`, `controller.py`, `settings.py`) instead.

## Known app bugs recorded as strict xfail

A test that exposes a bug in the app is marked `xfail(strict=True, reason="BUG: ...")`. When the bug is fixed the test starts to XPASS and fails the run, which is the signal to remove the marker. `grep -rn "BUG:" tests` lists them.

## CI

`.github/workflows/ci.yml`, on push and pull request to `develop` and `main`, on `ubuntu-22.04` (Zorin OS 17 base). Job `unit`: apt packages (`python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 ffmpeg xvfb`), venv with `--system-site-packages`, `pytest -q` with coverage. Job `e2e`: Playwright Chromium, `pytest tests/e2e -m e2e`. Live TV tests never run in CI.
