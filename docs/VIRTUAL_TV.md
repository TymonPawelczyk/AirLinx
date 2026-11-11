# Virtual TV

A local Google Cast and DLNA receiver for end-to-end tests of AirLinx without a television.
It lives in `tools/virtual_tv/` (test tooling, never imported by `airlinx/`); its tests are in
`tests/virtual_tv/` and are opt-in (`pytest -m vtv`).

**Where the code lives, and why there.** `tools/` rather than `tests/`: it is also a CLI for manual work
(`python -m tools.virtual_tv`, a live ffplay window, a TV for the GUI), not only a test fixture.

## What it is not

It does not emulate a TV decoder or a TV's jitter buffer, and its certificates are **not** Google's: the
device-auth chain ends in a throw-away test root. A real TV is still needed for interoperability (Samsung
quirks, Wi-Fi, real decoding). `tests/live` (marker `tv`) stays the final check.

## Quick start

```bash
.venv/bin/python -m pytest -m vtv tests/virtual_tv         # all 35 tests, about 90 s
.venv/bin/python -m tools.virtual_tv --cast --dlna --show  # a TV on loopback with a live window
tools/virtual_tv/netns.sh --dev-trust -- .venv/bin/python -m airlinx --debug   # the GUI, TV at 10.77.0.2
```

CLI: `python -m tools.virtual_tv --cast --dlna [--show] [--fail=auth|cert|busy|noport|hang|pending]
[--name "Wirtualny TV"] [--host IP] [--out DIR] [--advertise]`. Recordings go to `--out`:
`cast/video.h264` (Annex-B), `cast/audio.opus` (Ogg), `cast/media/` (downloaded files), `dlna/`, and a
machine-readable `cast/events.jsonl`.

## Architecture

```
AirLinx (MirrorSession, CastMediaPlayer, DlnaPlayer, MediaServer, Discovery)
        |  TLS CASTv2 :8009        |  UDP RTP/RTCP        |  HTTP SOAP :9197        ^ HTTP GET media
        v                          v                      v                         |
 cast_receiver.CastReceiver   cast_media.MediaPlane   dlna_renderer.DlnaRenderer ---+
   control, device-auth,       RTP parse, AES-CTR,      AVTransport, RenderingControl,
   LAUNCH/STOP, OFFER/ANSWER,  frame assembly, RR/XR/   ConnectionManager, Samsung-style
   Default Media Receiver      ACK/NACK feedback        fetch and quirks
        |                          |
     pki.TestPki              sinks.StreamRecorder (files, optional ffplay)
 announce.MdnsAnnouncer / SsdpResponder   (only inside the isolated namespace)
```

- `cast_media.py` is written from `spikes/cast_streaming/PROTOCOL.md` **independently of
  `airlinx.core.cast.rtp`**, so a sender mistake cannot be mirrored by the receiver. Only the CASTv2 message
  codec and device-auth framing are shared with `castv2.py`.
- The receiver behaves like the real one where it matters: it ignores RTP until a Sender Report arrived, sends
  one RR+XR+Cast-feedback packet per frame, NACKs holes, ACKs with the CST2 bit vector, answers PLI-style
  recovery, and stays silent on `webrtc GET_STATUS` as the Samsung does.
- The Default Media Receiver answers the first message of a LOAD like Chrome's receiver: BUFFERING is a
  broadcast, the reply carrying the LOAD's `requestId` is MEDIA_STATUS (or LOAD_FAILED) once the URL was fetched.
  For LAUNCH of CC1AD845 the first reply must be RECEIVER_STATUS (pychromecast drops its callback otherwise).

### Failure modes (`--fail=` / `CastReceiver(fail=...)`)

| mode | what the TV does | what AirLinx must do |
|---|---|---|
| `auth` | device-auth signature is wrong | `tv_auth_failed`, nothing sent but the challenge, no retry, no DLNA fallback |
| `cert` | device identity changes from the 2nd connection | `tv_cert_changed` + `devices.trustAgain` retry, no LAUNCH/OFFER, no DLNA |
| `busy` | YouTube is running | `{ok:false, code:"tv_busy"}` with a `force` retry; with force the app is replaced |
| `noport` | nothing listens on the Cast port | unpinned TV: DLNA fallback for files; pinned TV: `cast_unverified` (downgrade guard) |
| `hang` | TCP accepted, no TLS answer | `tv_not_found` after the connect timeout |
| `pending` | LAUNCH waits for consent (`approve()`/`deny()`) | state `waiting_tv` with "Zaakceptuj połączenie na telewizorze."; deny gives `tv_denied`; a late OK after cancel is STOPped |

`permission_pending` is not an error code in the controller: it is the `detail` of the `waiting_tv` state
(`PermissionPendingError().message_pl`). `STOP_FALLBACK_CODES` lists it but nothing raises it.

## Trusting the test root (developer mode)

A virtual TV cannot carry a Google-issued device certificate. Its AuthResponse chains to a **test root CA**
generated in memory at every start (public part written to `test-root-ca.pem`; private keys never touch the disk).

AirLinx trusts such a root only like this (`castv2.configure_dev_trust`):

- **Off by default.** Nothing in `config.json`/settings can enable it; the environment is read once, by an
  explicit call at app start.
- Needs **both** `AIRLINX_DEV=1` and `AIRLINX_DEV_CAST_ROOT=<pem file>`. The root alone is ignored (with a warning).
- The file must be a valid, self-signed, currently valid CA, at most 64 KB and 4 certificates, and must not reuse
  the subject of a real Cast root. Any problem leaves the hook off.
- **Loud:** a `WARNING` in the log, `AppState.dev_cast_root = true`, and a warning banner in the window.
- A TV authenticated through a dev root is pinned with `dev: true` in a separate file `known_tvs.dev.json`
  (unless `AIRLINX_KNOWN_TVS` is set), and a pin never accepts the other trust domain: swapping a real identity
  for a test one (or back) is `tv_cert_changed`.
- Tests inject roots with `castv2.dev_roots([cert])` or `set_dev_roots()`, no environment involved.
- No shipped script sets these variables (checked by a test).

**Threat.** Whoever holds the private key of a trusted dev root can impersonate any TV to that AirLinx
process: device-auth is what stops a LAN attacker from posing as the TV and receiving the screen, audio and the
tokenised media URL. Therefore the root is per-run and in memory, the TV process is the only holder of the key,
and the switch needs an explicit, per-process opt-in. Never put the variables in a profile, launcher or `.desktop` file.

## Isolated network (rootless)

Discovery rejects loopback, and loopback hides everything about addresses (IP choice for the media URL, which
side is "the PC"). `tools/virtual_tv/netns.py` (wrapper `netns.sh`) builds, **without sudo** and without changing
the host, two network namespaces inside an unprivileged user namespace, joined by a veth pair:

```
 pc  (your command)   vtv-host 10.77.0.1  <-- veth -->  10.77.0.2 vtv-tv   tv (the virtual TV)
```

Neither has a route to the real LAN: `connect(192.168.1.1)` gives `ENETUNREACH`. Inside, real mDNS
(`_googlecast._tcp` with `id/fn/md/ca/...`) and SSDP (MediaRenderer M-SEARCH) discovery work between the two.
Requires `unshare`, `nsenter`, `ip` and `kernel.unprivileged_userns_clone=1` (Ubuntu default);
`netns.sh --check` tells. If unprivileged namespaces are off, only the loopback mode (a `Device` injected in code,
as `tests/virtual_tv` does) works; the same topology can be built with `sudo ip netns` and `ip link add type veth`.

**Announcements are guarded.** mDNS/SSDP are multicast, so `announce.py` refuses to start unless every interface
of the current namespace is `lo` or `vtv*` (read with `socket.if_nameindex()`, not `/sys`, which keeps showing the
host's interfaces inside a namespace). There is no flag to bypass it; `--advertise` on the host exits with code 3.

The GUI works in the namespace too (X over the filesystem socket); that path is not covered by a test.

## Tests (`pytest -m vtv`)

| file | covers |
|---|---|
| `test_mirror.py` | real `MirrorSession`: live <= 10 s, >= 50 fps, 100 audio frames/s, ffprobe/ffmpeg decode of what the TV received, STOP, OFFER contents, live bitrate change, 2% packet loss (NACK + retransmit), PLI keyframe, app closed on the TV |
| `test_media.py` | file over Cast (pychromecast, DMR) and DLNA (SOAP): states, Range/DLNA headers, byte-exact download, pause/seek/volume, LOAD failure, Samsung `GetVolume = 0` |
| `test_failures.py` | each failure mode through the real `Controller` (codes above) |
| `test_netns.py` + `netns_scenario.py` | isolation, real discovery at 10.77.0.2, manual add, mirror + Cast + DLNA playback, media URL host = PC address, unknown token 404 |
| `test_cli.py` | banner, ready file, public-only root PEM, SIGINT, `noport` |
| `test_latency.py` | latency baseline; `VTV_WRITE_BASELINE=1` rewrites `docs/perf/virtual-tv-latency-baseline.md` |

The hook's own safety tests are in the default suite: `tests/test_dev_cast_root.py`. Screen and audio are lavfi
test signals patched into `capture.py`, so the tests need `ffmpeg` but no X server, PulseAudio or GPU.

Not covered: the media server binds tokens to nothing but the URL, so there is no token-to-IP binding to test yet
(the namespace scenario is ready for it: the TV fetches from a different address than the PC's).

## Android TV / Google TV emulator (reconnaissance, nothing installed)

Question: can the Android Studio emulator replace a real TV as a Cast target?

Findings:

- Locally: the SDK has `emulator`, `adb`, `/dev/kvm`, and only a phone image (`google_apis_playstore_ps16k`).
  There is no TV image.
- Real Android TV / Google TV devices ship the Cast receiver as the system app `com.google.android.apps.mediashell`
  ([Chromium dev thread](https://groups.google.com/a/chromium.org/g/chromium-dev/c/2-cYdGUfnlE), 2019). Google's
  [Cast on Android TV docs](https://developers.google.com/cast/docs/android_tv_receiver/debugging) describe it for hardware,
  and say nothing about the emulator.
  **Uncertain:** whether emulator TV images (Google APIs / Google TV) include and run it, whether it advertises over mDNS
  in the emulator, and whether it offers the mirroring app (0F5096E8) rather than only custom Web Receivers. Nothing
  found confirms it; only a download and a try (`adb shell pm list packages | grep mediashell`, then `nc -z <ip> 8009`)
  would settle it.
- Networking if it works: the emulator sits behind a NAT (guest `10.0.2.15`, host alias `10.0.2.2`). TCP 8009 can be reached
  with `adb forward tcp:8009 tcp:8009`, but then the TV is `127.0.0.1` and discovery rejects it (use an injected
  `Device`). **Mirroring is the blocker:** the receiver picks a random UDP port per session (the ANSWER's `udpPort`);
  `adb forward` is TCP-only and the console's `redir add udp:` needs the port in advance, so RTP cannot reach the guest
  ([emulator networking](https://developer.android.com/studio/run/emulator-networking.html)). Emulator 36.5+ documents a
  simplified shared-Wi-Fi model that might remove the NAT; unverified. File playback (TCP only: control plus the guest
  fetching `http://<host ip>:<port>`) has no such problem.
- Even then the emulator tests the real Android decoder in software and a real mediashell, which is valuable
  (interoperability, actual decoding), but not the Samsung firmware.

**Recommendation.** Keep the virtual TV as the daily, deterministic harness. Spend about an hour trying a TV image
(Google TV, API 34+) only to answer one question: does `mediashell` listen on 8009 and accept `LAUNCH 0F5096E8`?
If yes, use it for file playback and decode checks over `adb forward`; for mirroring consider it only if the shared-Wi-Fi mode
works, otherwise it stays a real-TV test. Do not make it a CI dependency (image size, KVM, flaky boot).
