#!/usr/bin/env python3
"""Live smoke test: mirror a monitor to the TV for N seconds, print states/stats, always STOP.

Usage: .venv/bin/python scripts/smoke_mirror.py --tv TV_IP [--seconds 12] [--monitor DP-4]
                                                [--no-audio] [--update]  [--force]
                                                [--audio-source system|mic|system_mic|app] [--mic NAME]
                                                [--app KEY] [--switch-audio SOURCE] [--play]
The TV address comes from --tv or the AIRLINX_TV_IP environment variable; there is no default.
Refuses to start when another app runs on the TV (unless --force).
--audio-source picks what the TV hears (core/audio.py); --switch-audio changes it live at half time;
--play loops a system sound through paplay (app key "pacat") so system/app modes have a signal.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from airlinx.core import models  # noqa: E402
from airlinx.core.cast import castv2  # noqa: E402
from airlinx.core.cast.mirror import MirrorSession  # noqa: E402
from airlinx.core.sources import list_sources  # noqa: E402


async def main(a: argparse.Namespace) -> int:
    running = await castv2.get_running_app(a.tv)
    print(f"TV running app: {running.get('displayName') if running else None}")
    if running and not a.force:
        print("TV busy — not starting (use --force).")
        return 3
    srcs = [s for s in await list_sources(with_thumbnails=False) if s.kind == "monitor"]
    src = next((s for s in srcs if a.monitor in s.id), srcs[0])
    print(f"source: {src.name} {src.geometry}")
    dev = models.Device(id=a.tv, name="TV", model="", ip=a.tv, cast_port=8009, caps={"cast_mirror"})

    def audio_opts(source: str) -> dict:
        return {"audio_source": source, "audio_mic": a.mic or "default", "audio_app": a.app,
                "audio_app_name": a.app}

    opts = models.MirrorOptions(audio=not a.no_audio, fps=a.fps, bitrate=a.bitrate, **audio_opts(a.audio_source))
    print(f"audio: {'off' if a.no_audio else a.audio_source}" + (f" -> {a.switch_audio}" if a.switch_audio else ""))
    player = None
    if a.play:
        snd = "/usr/share/sounds/freedesktop/stereo/complete.oga"
        player = subprocess.Popen(["bash", "-c", f"while true; do paplay {snd}; done"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        await asyncio.sleep(0.5)
    t0 = time.monotonic()

    def on_state(state: str, detail: str | None) -> None:
        print(f"[{time.monotonic() - t0:6.2f}s] STATE {state} {detail or ''}")

    def on_stats(st: models.Stats) -> None:
        print(f"[{time.monotonic() - t0:6.2f}s] STATS {st.width}x{st.height} fps={st.fps:.1f} "
              f"bitrate={st.bitrate / 1e6:.2f}Mb/s rtt={st.rtt_ms if st.rtt_ms is None else round(st.rtt_ms, 1)} "
              f"loss={st.loss:.3f} latency={st.latency_ms and round(st.latency_ms)}ms stages={st.stages} "
              f"enc={st.encoder}")

    s = MirrorSession(dev, src, opts, on_stats, on_state)
    try:
        await s.start(force=a.force)
        if a.switch_audio:
            await asyncio.sleep(a.seconds / 2)
            m = s._media  # noqa: SLF001
            if m is not None and m.audio:
                print("AUDIO before switch", {k: m.audio.snapshot()[k] for k in ("frames", "acked")})
            print(f"update: audio source -> {a.switch_audio} (audio capture restart only)")
            await s.update(models.MirrorOptions(audio=not a.no_audio, fps=a.fps, bitrate=a.bitrate,
                                                **audio_opts(a.switch_audio)))
            await asyncio.sleep(a.seconds / 2)
        elif a.update:
            await asyncio.sleep(a.seconds / 2)
            print("update: bitrate -> 8 Mb/s (encoder restart)")
            await s.update(models.MirrorOptions(audio=not a.no_audio, fps=a.fps, bitrate=8_000_000))
            await asyncio.sleep(a.seconds / 2)
        else:
            await asyncio.sleep(a.seconds)
        m = s._media  # noqa: SLF001 (diagnostics only)
        if m is not None:
            v = m.video.snapshot()
            print("VIDEO", {k: v[k] for k in ("frames", "keyframes", "acked", "lag", "retx", "nack_msgs", "pli",
                                               "dropped", "send2ack_ms", "cap2ack_ms", "playout_delay_ms")})
            if m.audio:
                au = m.audio.snapshot()
                print("AUDIO", {k: au[k] for k in ("frames", "acked", "lag", "retx", "send2ack_ms")})
            print("encoder restarts:", m.vcap.restarts, "last restart ms:", m.vcap.last_restart_ms)
    finally:
        await s.stop()
        if player is not None:
            os.killpg(player.pid, 15)
        after = await castv2.get_running_app(a.tv)
        print(f"TV running app after STOP: {after.get('displayName') if after else None}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", default=os.environ.get("AIRLINX_TV_IP"), help="TV address (or set AIRLINX_TV_IP)")
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--monitor", default="DP-4")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", type=int, default=12_000_000)
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--update", action="store_true", help="change bitrate mid-run")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--audio-source", default="system", choices=("system", "mic", "system_mic", "app"))
    ap.add_argument("--mic", default=None, help="pulse source name (default: @DEFAULT_SOURCE@)")
    ap.add_argument("--app", default=None, help="app key for --audio-source app (e.g. firefox, pacat)")
    ap.add_argument("--switch-audio", default=None, choices=("system", "mic", "system_mic", "app"))
    ap.add_argument("--play", action="store_true", help="loop a sound with paplay during the run")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    if not args.tv:
        ap.error("pass --tv TV_IP or set AIRLINX_TV_IP")
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(main(args)))
