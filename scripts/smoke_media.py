#!/usr/bin/env python3
"""Smoke test for discovery + media playback on a real TV.

  .venv/bin/python scripts/smoke_media.py                 # discovery only
  .venv/bin/python scripts/smoke_media.py --play          # + 10 s test clip via Cast, then DLNA (≤15 s each, STOP)
  options: --ip TV_IP (add_manual), --format mkv|mp4, --seconds 12, --only cast|dlna, --force

Playback runs only when the TV's Cast receiver is idle (no app other than backdrop), unless --force.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from airlinx.core.cast.media import IDLE_APPS, CastMediaPlayer  # noqa: E402
from airlinx.core.discovery import Discovery  # noqa: E402
from airlinx.core.dlna import DlnaPlayer  # noqa: E402
from airlinx.core.media_server import MediaServer  # noqa: E402
from airlinx.core.models import MediaItem, PlaybackState  # noqa: E402

SCRATCH = os.environ.get("AIRLINX_SCRATCH") or tempfile.gettempdir()


def make_clip(fmt: str) -> str:
    path = os.path.join(SCRATCH, f"airlinx_smoke.{fmt}")
    if not os.path.exists(path):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "10",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "30",
                        "-c:a", "aac", "-b:a", "128k", path], check=True)
    return path


def printer(tag: str):
    last = [None]

    def cb(s: PlaybackState):
        key = (s.state, int(s.position))
        if key != last[0]:
            last[0] = key
            print(f"  [{tag}] {s.state:9s} pos={s.position:5.1f} dur={s.duration} vol={s.volume:.2f}"
                  + (f" err={s.error}" if s.error else ""), flush=True)
    return cb


async def wait_playing(player, seconds: float) -> bool:
    t0 = time.monotonic()
    reached = False
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(0.5)
        st = player.state.state
        if st == "playing":
            reached = True
        if st in ("ended", "error"):
            break
    return reached


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--ip", default=None)
    ap.add_argument("--format", default="mkv", choices=["mkv", "mp4"])
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--only", choices=["cast", "dlna"], default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("pychromecast", "zeroconf", "async_upnp_client", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    disc = Discovery(lambda devs: print(f"  on_change: {len(devs)} device(s)", flush=True))
    await disc.start()
    print(f"interface {disc.interface_ip}; scanning 6 s…")
    await asyncio.sleep(6)
    if args.ip:
        d = await disc.add_manual(args.ip)
        print(f"add_manual({args.ip}) → {d}")
    for d in disc.devices:
        print(f"* {d.name!r} model={d.model!r} ip={d.ip} id={d.id} cast_port={d.cast_port} "
              f"dlna={d.dlna_location} caps={sorted(d.caps)} online={d.online}")
    devices = [d for d in disc.devices if d.online]
    await disc.stop()
    if not args.play:
        return 0
    tv = next((d for d in devices if {"cast_media", "dlna"} <= d.caps), None) or (devices[0] if devices else None)
    if tv is None:
        print("no device to play on")
        return 1

    clip = make_clip(args.format)
    item = MediaItem("smoke", clip, "AirLinx — test", 10.0, "video")
    server = MediaServer(lan_ip=None)
    await server.start()
    print(f"media server {server.base_url}")
    rc = 0
    try:
        if "cast_media" in tv.caps and args.only in (None, "cast"):
            cast = CastMediaPlayer(tv, printer("cast"))
            await cast.connect()
            app_id, name = cast.running_app()
            print(f"Cast status: app_id={app_id} name={name!r}")
            if app_id not in IDLE_APPS and not args.force:
                print("TV busy — skipping playback")
                await cast.close()
                return 2
            url, mime = await server.publish_async(item, "cast", target_ip=tv.ip)
            print(f"cast url {url} ({mime}, {server.info(url).plan.reason if server.info(url) else 'direct'})")
            try:
                await cast.load(item, url, mime, subtitles=server.subtitle_tracks(url),
                                poster_url=server.poster_url(url))
                ok = await wait_playing(cast, args.seconds)
                print(f"CAST RESULT: {'played' if ok else 'NOT playing'} (last={cast.state.state})")
                rc |= 0 if ok else 4
            finally:
                await cast.stop()
                await cast.close()
            await asyncio.sleep(2)
        if "dlna" in tv.caps and args.only in (None, "dlna"):
            dl = DlnaPlayer(tv, printer("dlna"))
            url, mime = await server.publish_async(item, "dlna", target_ip=tv.ip)
            print(f"dlna url {url} ({mime})")
            try:
                await dl.load(item, url, mime, force=args.force)
                ok = await wait_playing(dl, args.seconds)
                print(f"DLNA RESULT: {'played' if ok else 'NOT playing'} (last={dl.state.state})")
                rc |= 0 if ok else 8
            finally:
                await dl.stop()
                await dl.close()
    finally:
        await server.stop()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
