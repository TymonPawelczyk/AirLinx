# Third-party notices

> **Note.** Prepared with the help of AI (Claude). This is not legal advice; review by a lawyer is recommended before publication.
> Version 1.1, 10 November 2026. Covers AirLinx 0.1.0.

AirLinx is licensed under the GNU General Public License, version 3 or (at your option) any later version (SPDX: `GPL-3.0-or-later`; see `LICENSE`). It contains or uses the third-party components listed below, each under its own license. Their copyright holders do not endorse AirLinx.

## 1. Code derived from openscreen (Chromium)

Parts of AirLinx are ported from, or modelled on, the openscreen project (`https://chromium.googlesource.com/openscreen/`):

| AirLinx file | What comes from openscreen |
|---|---|
| `airlinx/core/cast/rtp.py` | Cast Streaming media plane: RTP/RTCP packetization, frame encryption and feedback handling, ported from `cast/streaming/impl` (C++ to Python) |
| `airlinx/core/cast/castv2.py` | Cast device authentication and TLS certificate checks, modelled on `cast/sender/channel/cast_auth_util.cc`, plus the embedded Cast and Eureka root certificates (see section 2) |

openscreen is distributed under the BSD 3-Clause License:

```
Copyright 2018 The Chromium Authors

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

Source of the license text: `https://chromium.googlesource.com/openscreen/+/refs/heads/main/LICENSE` (retrieved 10 November 2026). BSD-3-Clause is compatible with GPL-3.0-or-later.

## 2. Google Cast root certificates

`airlinx/core/cast/castv2.py` embeds two public root CA certificates, used only to verify that a TV is a genuine Cast receiver (device authentication):

| Certificate | Subject | Validity |
|---|---|---|
| `CAST_ROOT_CA_B64` | C=US, ST=California, L=Mountain View, O=Google Inc, OU=Cast, CN=Cast Root CA | 2014-04-02 to 2034-03-28 |
| `EUREKA_ROOT_CA_B64` | C=US, ST=California, L=Mountain View, O=Google Inc, OU=Google TV, CN=Eureka Root CA | 2012-12-17 to 2032-12-12 |

The certificates were taken from openscreen (`cast/common/certificate/*-inc.h`) and are distributed there under the BSD 3-Clause License above. They contain public keys only. AirLinx does not include any private key and does not use the certificates to impersonate a Cast device.

## 3. Fonts

The user interface (`ui/assets/fonts/`) and the website (`site/assets/fonts/`) bundle WOFF builds of these fonts. Copies of the same files are in `docs/brand/final/fonts/` and `docs/design/final/assets/fonts/`.

| Font | Files | Copyright | License |
|---|---|---|---|
| Inter | `Inter-Regular.woff`, `Inter-Medium.woff` | Copyright 2016-2023 Rasmus Andersson, `https://github.com/rsms/inter` | SIL Open Font License 1.1 |
| JetBrains Mono | `JetBrainsMono-Medium.woff`, `JetBrainsMono-ExtraBold.woff` | Copyright 2019-2023 The JetBrains Mono Project Authors, `https://github.com/JetBrains/JetBrainsMono` | SIL Open Font License 1.1 |

The full OFL 1.1 text is in `LICENSE-Inter-OFL-1.1.txt` and `LICENSE-JetBrainsMono-OFL-1.1.txt` next to the font files, and at `https://openfontlicense.org`. Neither font declares a Reserved Font Name. The OFL allows the fonts to be bundled with software under any license. The fonts themselves remain under the OFL and are not relicensed under the GPL.

## 4. Python dependencies

AirLinx does not bundle these packages. `install.sh` downloads them from PyPI into a virtual environment on the user's machine. The table lists the runtime dependency tree as installed and tested (versions from the project's environment on 10 November 2026). Direct dependencies are in **bold**.

| Package | Tested version | License (SPDX) | Project |
|---|---|---|---|
| **aiohttp** | 3.14.5 | Apache-2.0 AND MIT | https://github.com/aio-libs/aiohttp |
| **async-upnp-client** | 0.48.2 | Apache-2.0 | https://github.com/StevenLooman/async_upnp_client |
| **cryptography** | 50.0.2 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| **defusedxml** (imported directly, also via async-upnp-client) | 0.7.1 | PSF-2.0 | https://github.com/tiran/defusedxml |
| **zeroconf** | 0.152.1 | LGPL-2.1-or-later | https://github.com/python-zeroconf/python-zeroconf |
| aiohappyeyeballs | 2.7.1 | PSF-2.0 | https://github.com/aio-libs/aiohappyeyeballs |
| aiosignal | 1.4.0 | Apache-2.0 | https://github.com/aio-libs/aiosignal |
| async-timeout | 5.0.1 | Apache-2.0 | https://github.com/aio-libs/async-timeout |
| attrs | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| frozenlist | 1.8.0 | Apache-2.0 | https://github.com/aio-libs/frozenlist |
| idna | 3.20 | BSD-3-Clause | https://github.com/kjd/idna |
| ifaddr | 0.2.0 | MIT | https://github.com/pydron/ifaddr |
| multidict | 7.1.0 | Apache-2.0 | https://github.com/aio-libs/multidict |
| propcache | 0.5.4 | Apache-2.0 | https://github.com/aio-libs/propcache |
| pycparser | 3.11 | BSD-3-Clause | https://github.com/eliben/pycparser |
| python-didl-lite | 1.5.1 | Apache-2.0 | https://github.com/StevenLooman/python-didl-lite |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| voluptuous | 0.16.0 | BSD-3-Clause | https://github.com/alecthomas/voluptuous |
| yarl | 1.25.1 | Apache-2.0 | https://github.com/aio-libs/yarl |

Notes:

- `pychromecast` (with `casttube`, `requests`, `protobuf` and their dependencies) was dropped from `install.sh` when Cast media playback moved onto AirLinx's own verified CASTv2 channel. The unused `samsungtvws` (LGPL-3.0) and its dependency `websocket-client` were dropped from `install.sh` in 0.1.0.
- Development and test tools in the project environment (pytest, pytest-asyncio, pytest-cov, coverage, playwright, aiortc, av and others) are not runtime dependencies and are not listed.
- Each package's full license text ships with the package (`*.dist-info/` in the virtual environment, usually `licenses/` or `LICENSE*`).

### GPL-3.0-or-later compatibility

**No incompatible dependency was found.**

| License | Packages | Compatible with GPL-3.0-or-later |
|---|---|---|
| MIT, MIT-0, BSD-2/3-Clause, ISC, PSF-2.0 | most of the tree | Yes (permissive) |
| Apache-2.0 | aiohttp, async-upnp-client, yarl, multidict, requests and others | Yes (Apache-2.0 is compatible with GPL version 3 and later, not with GPL-2.0) |
| Apache-2.0 OR BSD-3-Clause | cryptography | Yes |
| LGPL-2.1-or-later | zeroconf | Yes (allows use under GPL-3.0) |
| MPL-2.0 | certifi | Yes, through the MPL-2.0 Secondary License clause (certifi is not marked "Incompatible With Secondary Licenses") |

"Or later" lets recipients use AirLinx under GPL-3.0 or a future GPL version. Every component above permits combination under GPL-3.0. Whether a combination could be relicensed under a future GPL version would depend on that version's terms and on each component's license.

If AirLinx is ever distributed with its dependencies bundled (AppImage, Flatpak, .deb, vendored wheels), the full license texts and notices of every bundled package must ship with it. Apache-2.0 packages that include a `NOTICE` file need that file reproduced too.

## 5. System components (not bundled)

AirLinx requires, but does not include, the following components from the user's Linux distribution. They are used under their own licenses:

| Component | Typical license | How AirLinx uses it |
|---|---|---|
| Python 3 | PSF-2.0 | runtime |
| PyGObject | LGPL-2.1-or-later | GTK and WebKit bindings |
| GTK 3 | LGPL-2.1-or-later | window, tray integration |
| WebKit2GTK 4.1 | LGPL-2.1 and BSD-2-Clause | user interface |
| FFmpeg (`ffmpeg`, `ffprobe`) | LGPL-2.1-or-later or GPL-2.0-or-later, depending on the build | screen capture, encoding, media probing; run as a separate process |
| PipeWire / PulseAudio (`pactl`) | MIT / LGPL-2.1-or-later | audio capture and routing; run as a separate process |
| X11 tools (`xrandr`, `xprop`) | MIT/X11 | monitor and window discovery; run as separate processes |

## 6. Trademarks

Product names are used only to describe compatibility. See `docs/legal/TRADEMARKS.md`.
