"""python -m tools.virtual_tv --cast --dlna [--show] [--fail=auth|cert|busy|noport|pending] [--name "Wirtualny TV"]"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .announce import MdnsAnnouncer, NotIsolatedError, SsdpResponder, assert_isolated
from .cast_receiver import FAIL_MODES, CastReceiver
from .dlna_renderer import DlnaRenderer


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m tools.virtual_tv",
        description="Virtual TV for AirLinx: a local Google Cast and DLNA receiver (no real television needed).")
    ap.add_argument("--cast", action="store_true", help="start the Google Cast receiver (CASTv2, mirroring, DMR)")
    ap.add_argument("--dlna", action="store_true", help="start the DLNA MediaRenderer")
    ap.add_argument("--show", action="store_true", help="show the received video live in an ffplay window")
    ap.add_argument("--fail", choices=FAIL_MODES, default=None,
                    help="failure mode: auth=wrong device-auth signature (tv_auth_failed), cert=device identity "
                         "changes from the 2nd connection (tv_cert_changed), busy=another app is running (tv_busy), "
                         "noport=nothing listens on the Cast port (cast_unverified / downgrade guard), "
                         "hang=port open, no answer, pending=LAUNCH waits for consent (permission_pending)")
    ap.add_argument("--codecs", default="h264,hevc",
                    help="video codecs the Cast receiver accepts in an OFFER (default h264,hevc); "
                         "restrict it to test negotiation, e.g. --codecs h264")
    ap.add_argument("--netns", action="store_true",
                    help="run inside the rootless two-namespace lab (needs the vtv-dlna module tools.virtual_tv.net)")
    ap.add_argument("--name", default="Wirtualny TV", help="friendly name (mDNS fn / UPnP friendlyName)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    ap.add_argument("--cast-port", type=int, default=8009)
    ap.add_argument("--eureka-port", type=int, default=8008, help="HTTP /setup/eureka_info port (0 = disabled)")
    ap.add_argument("--dlna-port", type=int, default=9197)
    ap.add_argument("--out", default=None, help="directory for recordings (default: a new temp dir)")
    ap.add_argument("--pending-seconds", type=float, default=6.0,
                    help="with --fail=pending: auto-approve after this long (default 6)")
    ap.add_argument("--no-samsung-quirks", action="store_true", help="DLNA: report the real volume instead of 0")
    ap.add_argument("--advertise", action="store_true",
                    help="announce via mDNS and SSDP. Only allowed inside the isolated namespace of netns.sh")
    ap.add_argument("--root-pem", default=None, help="where to write the TEST root CA (public part only)")
    ap.add_argument("--ready-file", default=None, help="write a JSON description here once listening")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if not (a.cast or a.dlna):
        ap.error("pick at least one of --cast / --dlna")
    a.codec_list = tuple(c.strip().lower() for c in a.codecs.split(",") if c.strip())
    if not a.codec_list or any(c not in ("h264", "hevc") for c in a.codec_list):
        ap.error("--codecs takes a comma list of h264, hevc")
    return a


async def run(a: argparse.Namespace) -> int:
    if a.netns:
        try:
            import importlib
            importlib.import_module("tools.virtual_tv.net")
        except ImportError:
            print("--netns: not available yet (module tools.virtual_tv.net is missing); "
                  "use tools/virtual_tv/netns.sh instead", file=sys.stderr)
            return 4
        print("--netns: run `python -m tools.virtual_tv.net run -- <cmd>` or tools/virtual_tv/netns.sh", file=sys.stderr)
        return 4
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    if a.advertise:
        try:
            assert_isolated()
        except NotIsolatedError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="vtv-"))
    out.mkdir(parents=True, exist_ok=True)
    cast: Optional[CastReceiver] = None
    dlna: Optional[DlnaRenderer] = None
    ann: list = []
    info: dict = {"name": a.name, "host": a.host, "out": str(out)}
    try:
        if a.cast:
            cast = CastReceiver(host=a.host, port=a.cast_port, name=a.name, fail=a.fail, out_dir=out / "cast",
                                show=a.show, pending_s=a.pending_seconds, codecs=a.codec_list,
                                eureka_port=a.eureka_port if a.eureka_port else None)
            await cast.start()
            root = Path(a.root_pem) if a.root_pem else out / "test-root-ca.pem"
            cast.pki.write_root_pem(root)
            info.update(cast={"port": cast.port, "eureka_port": cast.eureka_port, "device_id": cast.device_id,
                              "fail": a.fail}, root_pem=str(root))
        if a.dlna:
            dlna = DlnaRenderer(host=a.host, port=a.dlna_port, name=a.name + " (DLNA)",
                                samsung_quirks=not a.no_samsung_quirks, out_dir=out / "dlna", show=a.show)
            await dlna.start()
            info["dlna"] = {"location": dlna.location, "udn": dlna.udn}
        if a.advertise:
            if cast and a.fail != "noport":
                m = MdnsAnnouncer(ip=a.host, port=cast.port, device_id=cast.device_id, name=a.name, model=cast.model)
                await m.start()
                ann.append(m)
            if dlna:
                s = SsdpResponder(ip=a.host, location=dlna.location, udn=dlna.udn)
                await s.start()
                ann.append(s)
        _print_banner(a, info, cast, dlna)
        if a.ready_file:
            Path(a.ready_file).write_text(json.dumps(info))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        print("\nstopping the virtual TV ...")
    finally:
        for x in ann:
            await x.stop()
        if cast:
            await cast.stop()
        if dlna:
            await dlna.stop()
    return 0


def _print_banner(a: argparse.Namespace, info: dict, cast: Optional[CastReceiver], dlna: Optional[DlnaRenderer]) -> None:
    p = print
    p("=" * 78)
    p(f" Virtual TV \"{a.name}\"  (test tool, not a real television)")
    p("=" * 78)
    if cast:
        p(f" Cast   : {a.host}:{cast.port}  device id {cast.device_id}" + (f"  [failure mode: {a.fail}]" if a.fail else ""))
        if cast.eureka_port:
            p(f"          eureka_info on http://{a.host}:{cast.eureka_port}/setup/eureka_info")
    if dlna:
        p(f" DLNA   : {dlna.location}" + ("  [Samsung quirks: GetVolume = 0]" if dlna.samsung_quirks else ""))
    p(f" Output : {info['out']}   (cast/video.h264, cast/audio.opus, cast/media/, dlna/)")
    p(f" Show   : {'live ffplay window' if a.show else 'off (add --show for a live window)'}")
    p("")
    loop = a.host.startswith("127.")
    if loop:
        p(" Listening on loopback. AirLinx's discovery deliberately rejects loopback addresses, so this")
        p(" is for scripted tests (a Device injected in code, see tests/virtual_tv). To see the TV in the")
        p(" AirLinx window use the isolated network, which gives the TV its own IP (10.77.0.2):")
        p("")
        p("     tools/virtual_tv/netns.sh --dev-trust -- .venv/bin/python -m airlinx --debug")
        p("")
    else:
        p(" In AirLinx: add the TV by IP (RPC devices.addManual, in the UI \"Dodaj ręcznie\"):")
        p(f"     {a.host}")
    if cast:
        root = info.get("root_pem", "<root.pem>")
        p(" The virtual TV signs device-auth with a TEST root CA (generated now, never a Google certificate).")
        p(" AirLinx trusts it only in developer mode, explicitly:")
        p(f"     AIRLINX_DEV=1 AIRLINX_DEV_CAST_ROOT={root} .venv/bin/python -m airlinx --debug")
        p(" (AirLinx then shows a warning in the log and in the window. Never set these for daily use.)")
    p("")
    p(" Ctrl+C stops the virtual TV.")
    p("=" * 78, flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
