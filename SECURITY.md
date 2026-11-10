# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |
| < 0.1 | No |

Fixes are released as a new patch version (`hotfix/*` branch, tag `v0.1.Z`). Only the latest 0.1.x release gets fixes.

## Reporting a vulnerability

Report vulnerabilities privately through **GitHub Private Vulnerability Reporting**: open the [Security tab](https://github.com/TymonPawelczyk/AirLinx/security) of the repository and click **Report a vulnerability** (direct link: https://github.com/TymonPawelczyk/AirLinx/security/advisories/new). You need a GitHub account.

Please do not open a public issue, pull request or discussion for a vulnerability before it is fixed. There is no security e-mail address.

A useful report contains:

- the AirLinx version (`airlinx --version`), distribution and session type;
- the TV model and protocol (Cast mirroring, Cast media, DLNA);
- what an attacker needs (position in the network, user interaction) and what they gain;
- steps or a proof of concept. Mask tokens and personal data in logs.

AirLinx is a private, non-commercial project maintained by one person, so there is no guaranteed response time. The reply comes in the private advisory thread, where a disclosure date is agreed once the issue is understood. After a fix is released, a GitHub Security Advisory describes the issue. The advisory may name the reporter's GitHub account.

## Threat model and scope

AirLinx runs on a desktop PC and talks to a TV on the same local network. It assumes a **trusted home LAN**. The attacker considered is another device in that LAN: a compromised IoT device, a guest, or someone who can impersonate the TV.

In scope:

- **Media server tokens.** While a file or a DLNA stream is published, the local HTTP server listens on a random port on all interfaces and serves it under `/<kind>/<token>/<name>`, where the token comes from `secrets.token_urlsafe(16)`. Guessing or reusing a token, reading files that were not published, path traversal, serving after unpublish, crashing the server with hostile requests, and tokens leaking into logs are all in scope.
- **Cast device authentication.** Before mirroring or handing over a media URL, AirLinx checks that the TV signs a challenge with a Cast device certificate that chains to the embedded Cast or Eureka root CA, and pins the device identity on first use (`~/.config/airlinx/known_tvs.json`). Bypassing the check, getting a different device accepted under a pinned identity, or forcing a silent downgrade from authenticated Cast to DLNA are in scope.
- **Cast mirroring stream.** Weaknesses in the AES-CTR encryption of RTP or in the key exchange over the TLS channel.
- **Local state.** Audio routing changes that are not restored (an application left muted or re-routed), and permissions of the files in `~/.config/airlinx` and `$XDG_RUNTIME_DIR/airlinx`.
- **Untrusted input.** Media files, subtitles, URLs added to the queue, and SSDP, mDNS, UPnP or CASTv2 messages from devices in the LAN.

Known and accepted (not vulnerabilities by themselves):

- **DLNA and file playback are not encrypted.** The DLNA protocol and the Cast Default Media Receiver fetch media over plain HTTP. Anyone who can sniff the LAN can see the tokenised URL and the content while it plays. The documentation tells users to use AirLinx only on a network they trust.
- **First connection is trust on first use.** A device that impersonates the TV before its first pinned connection, and also passes Cast device authentication, is accepted.
- **Discovery is visible.** mDNS and SSDP queries are seen by every device in the LAN.
- What the TV itself does after AirLinx launches a receiver on it, including connections from the TV to Google.

Out of scope: attacks that need local access to the user's account, denial of service by flooding the LAN, and vulnerabilities in ffmpeg, GTK, WebKitGTK, Python packages or the TV firmware (report those upstream; tell us if AirLinx makes them reachable).
