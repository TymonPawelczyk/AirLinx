#!/usr/bin/env python3
"""Latency lab for the Cast mirror: run short sessions with a given configuration and record the stats.

  .venv/bin/python scripts/perf_latency.py run  --config delay=60 --config delay=40,gop=1 [--seconds 15]
  .venv/bin/python scripts/perf_latency.py run  --sweep 100,80,60,40       # target_delay sweep
  .venv/bin/python scripts/perf_latency.py run  --config delay=60 --dry-run  # offline: print OFFER + ffmpeg argv
  .venv/bin/python scripts/perf_latency.py idle                              # is the TV idle? (exit 0 = idle)
  .venv/bin/python scripts/perf_latency.py flash                             # motion-to-photon test pattern

Config keys (comma separated, all optional; defaults = what the app ships for the Game profile):
  delay=<ms>        OFFER targetDelay + RTP playout-delay extension (the TV's playback buffer)
  preset=p1..p7     NVENC preset            tune=ull|ll|hq        NVENC tune
  gop=<seconds>     GOP length (keyframe interval)
  bufms=<ms>        rate-control buffer in ms of stream (-bufsize = bitrate * ms / 1000); app default 500
  extra=<a:b:c>     extra ffmpeg output args joined by ':' (e.g. extra=-rc-lookahead:0:-spatial-aq:0)
  name=<label>

Safety: every session is <= 20 s, never forced; a run is skipped unless Cast GET_STATUS shows no app AND
DLNA is not PLAYING (waits --wait-idle seconds, then gives up). STOP is always sent and leftover ffmpeg
processes started by this script are killed. Results go to docs/perf/results.csv (+ a JSON per run).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
PERF_DIR = os.path.join(ROOT, "docs", "perf")
MAX_SECONDS = 20.0

from airlinx.core import capture, models  # noqa: E402
from airlinx.core.cast import castv2, mirror  # noqa: E402

DEFAULTS = dict(delay=100, preset="p1", tune="ull", gop=2.0, bufms=500, extra="", name="")


# ---------------------------------------------------------------------------------- configs
def parse_config(text: str) -> dict:
    cfg = dict(DEFAULTS)
    for part in filter(None, (p.strip() for p in text.split(","))):
        k, _, v = part.partition("=")
        if k not in DEFAULTS:
            raise SystemExit(f"unknown config key {k!r}")
        cfg[k] = type(DEFAULTS[k])(v)
    cfg["name"] = cfg["name"] or "_".join(f"{k}{cfg[k]}" for k in ("delay", "preset", "tune", "gop", "bufms")
                                          ) + (f"_{cfg['extra']}" if cfg["extra"] else "")
    return cfg


_TUNE = {"hq": "hq", "ll": "ll", "ull": "ull"}


def patched_video_cmd(cfg: dict):
    orig = capture.build_video_cmd

    def build(p, disp=None):
        cmd = orig(p, disp)
        if p.encoder == "h264_nvenc":
            cmd[cmd.index("-preset") + 1] = cfg["preset"]
            cmd[cmd.index("-tune") + 1] = _TUNE[cfg["tune"]]
        cmd[cmd.index("-bufsize") + 1] = str(max(int(p.bitrate * cfg["bufms"] / 1000), 100_000))
        extra = [a for a in cfg["extra"].split(":") if a]
        if extra:
            i = len(cmd) - 1 - cmd[::-1].index("-f")           # the output muxer, not the x11grab input
            cmd[i:i] = extra
        return cmd
    return build


def patched_effective(cfg: dict):
    orig = mirror.effective_options

    def eff(o):
        e = orig(o)
        e.target_delay_ms, e.gop_s = int(cfg["delay"]), float(cfg["gop"])
        return e
    return eff


class Patch:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __enter__(self):
        self.o1, self.o2 = capture.build_video_cmd, mirror.effective_options
        capture.build_video_cmd, mirror.effective_options = patched_video_cmd(self.cfg), patched_effective(self.cfg)

    def __exit__(self, *a):
        capture.build_video_cmd, mirror.effective_options = self.o1, self.o2


# ---------------------------------------------------------------------------------- TV state
async def dlna_state(ip: str) -> str:
    """AVTransport state via a discovered renderer ('' when the TV has no DLNA, 'UNKNOWN' on error)."""
    from airlinx.core.discovery import Discovery
    from airlinx.core.dlna import DlnaPlayer, _tstate
    disc = Discovery(lambda devs: None)
    await disc.start()
    try:
        await asyncio.sleep(4)
        d = await disc.add_manual(ip)
        if d is None or not d.dlna_location:
            return ""
        pl = DlnaPlayer(d)
        await pl.connect()
        return _tstate(pl._dmr) or "UNKNOWN"  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        return f"UNKNOWN({e.__class__.__name__})"
    finally:
        await disc.stop()


async def tv_idle(ip: str) -> tuple[bool, str]:
    app = await castv2.get_running_app(ip)
    if app:
        return False, f"cast app: {app.get('displayName')}"
    ds = await dlna_state(ip)
    if ds.upper().startswith("PLAYING") or ds.upper().startswith("TRANSITIONING"):
        return False, f"dlna: {ds}"
    return True, f"idle (dlna={ds or 'n/a'})"


async def wait_idle(ip: str, wait_s: float) -> bool:
    end = time.monotonic() + wait_s
    while True:
        ok, why = await tv_idle(ip)
        print(f"  TV: {why}")
        if ok:
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(min(30, max(1, end - time.monotonic())))


# ---------------------------------------------------------------------------------- run
def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


async def run_one(cfg: dict, a: argparse.Namespace, src, dev) -> dict:
    secs = min(a.seconds, MAX_SECONDS)
    opts = models.MirrorOptions(audio=not a.no_audio, fps=60, bitrate=a.bitrate)
    samples: list[models.Stats] = []
    vsnaps: list[dict] = []
    states: list[tuple[float, str]] = []
    t0 = time.monotonic()
    live_at: list[float] = []

    def on_stats(st):
        samples.append(st)
        if st.latency_ms is not None and not live_at:
            live_at.append(time.monotonic() - t0)

    s = mirror.MirrorSession(dev, src, opts, on_stats, lambda st, d: states.append((round(time.monotonic() - t0, 2), st)))
    res: dict = dict(cfg, seconds=secs, ts=datetime.now().isoformat(timespec="seconds"), ok=False)
    pids_before = set(subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True).stdout.split())
    try:
        with Patch(cfg):
            await s.start(force=False)
            t_started = time.monotonic() - t0
            end = time.monotonic() + secs
            while time.monotonic() < end:
                await asyncio.sleep(1.0)
                m = s._media  # noqa: SLF001
                if m is not None:
                    vsnaps.append(dict(t=round(time.monotonic() - t0, 1), **m.video.snapshot()))
        res.update(ok=True, start_s=round(t_started, 2), time_to_live_s=round(live_at[0], 2) if live_at else None)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        await s.stop()
        await asyncio.sleep(0.5)
        left = set(subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True).stdout.split()) - pids_before
        for pid in left:
            subprocess.run(["kill", pid])
        res["orphan_ffmpeg"] = len(left)
        after = await castv2.get_running_app(a.tv)
        res["tv_app_after_stop"] = after.get("displayName") if after else None
    warm = [x for x in samples if x.latency_ms is not None][4:] or [x for x in samples if x.latency_ms is not None]
    st = lambda k: _med([x.stages.get(k) for x in warm])  # noqa: E731
    res.update(latency_ms=_med([x.latency_ms for x in warm]), capture_ms=st("capture"), encode_ms=st("encode"),
               network_ms=st("network"), tv_ms=st("tv"), fps=_med([x.fps for x in warm]),
               bitrate_mbps=_med([x.bitrate / 1e6 for x in warm]), rtt_ms=_med([x.rtt_ms for x in warm]),
               loss=_med([x.loss for x in warm]), states=[x[1] for x in states])
    if vsnaps:
        v0 = vsnaps[min(4, len(vsnaps) - 1)]      # after warm-up
        v1 = vsnaps[-1]
        dt = max(1.0, v1["t"] - v0["t"])
        res.update(retx_total=v1["retx"], nack_total=v1["nack_msgs"], dropped_total=v1["dropped"], pli_total=v1["pli"],
                   retx_per_s_steady=round((v1["retx"] - v0["retx"]) / dt, 2),
                   nack_per_s_steady=round((v1["nack_msgs"] - v0["nack_msgs"]) / dt, 2),
                   playout_delay_ms=v1["playout_delay_ms"], send2ack_ms=_med([x["send2ack_ms"] for x in vsnaps]),
                   lag_max=max(x["lag"] for x in vsnaps))
    res["dropped_connection"] = any(x in ("reconnecting", "error") for x in res.get("states", []))
    return res


CSV_FIELDS = ["ts", "name", "delay", "preset", "tune", "gop", "bufms", "extra", "seconds", "ok", "error", "start_s",
              "time_to_live_s", "latency_ms", "capture_ms", "encode_ms", "network_ms", "tv_ms", "fps", "bitrate_mbps",
              "rtt_ms", "loss", "send2ack_ms", "retx_total", "nack_total", "retx_per_s_steady", "nack_per_s_steady",
              "dropped_total", "pli_total", "playout_delay_ms", "lag_max", "dropped_connection", "orphan_ffmpeg",
              "tv_app_after_stop"]


def save(results: list[dict]) -> None:
    os.makedirs(PERF_DIR, exist_ok=True)
    path = os.path.join(PERF_DIR, "results.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, CSV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(results)
    js = os.path.join(PERF_DIR, f"run-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(js, "w") as f:
        json.dump(results, f, indent=1, default=str)
    print(f"saved {path} and {js}")


async def cmd_run(a: argparse.Namespace) -> int:
    cfgs = [parse_config(c) for c in a.config] + [parse_config(f"delay={d}") for d in
                                                   (a.sweep.split(",") if a.sweep else [])]
    if not cfgs:
        raise SystemExit("give --config and/or --sweep")
    if a.dry_run:
        for cfg in cfgs:
            with Patch(cfg):
                e = mirror.effective_options(models.MirrorOptions())
                offer = mirror.build_offer(video=mirror.StreamKeys.random(), audio=None, width=1920, height=1080, fps=60,
                                           bitrate=a.bitrate, target_delay_ms=e.target_delay_ms)
                p = capture.VideoParams((0, 0, 2560, 1440), 1920, 1080, 60, a.bitrate, e.gop_s)
                print(cfg["name"], "targetDelay", offer["offer"]["supportedStreams"][0]["targetDelay"])
                print("  ", " ".join(capture.build_video_cmd(p, ":0")))
        return 0
    from airlinx.core.sources import list_sources
    srcs = [s for s in await list_sources(with_thumbnails=False) if s.kind == "monitor"]
    src = next((s for s in srcs if a.monitor in s.id), srcs[0])
    dev = models.Device(id=a.tv, name="TV", model="", ip=a.tv, cast_port=8009, caps={"cast_mirror"})
    results = []
    try:
        for cfg in cfgs:
            print(f"== {cfg['name']}")
            if not await wait_idle(a.tv, a.wait_idle):
                print("  TV busy, skipping (never forced)")
                results.append(dict(cfg, ok=False, error="tv_busy", ts=datetime.now().isoformat(timespec="seconds")))
                continue
            r = await run_one(cfg, a, src, dev)
            results.append(r)
            print({k: r.get(k) for k in ("ok", "error", "latency_ms", "encode_ms", "network_ms", "tv_ms", "fps",
                                         "time_to_live_s", "retx_per_s_steady", "nack_per_s_steady", "dropped_total",
                                         "dropped_connection", "orphan_ffmpeg")})
            await asyncio.sleep(a.pause)
    finally:
        save(results)
    return 0


async def cmd_idle(a: argparse.Namespace) -> int:
    ok, why = await tv_idle(a.tv)
    print(why)
    return 0 if ok else 3


# ---------------------------------------------------------------------------------- flash
def cmd_flash(a: argparse.Namespace) -> int:
    """Full-screen blinking square + millisecond clock; film PC screen and TV together with a phone camera."""
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    win = Gtk.Window(title="AirLinx motion-to-photon")
    area = Gtk.DrawingArea()
    win.add(area)
    state = {"on": False, "t0": time.monotonic()}

    def draw(_w, cr):
        alloc = area.get_allocation()
        w, h = alloc.width, alloc.height
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        t = time.monotonic() - state["t0"]
        # square that flips every `period` seconds, plus a running millisecond counter (read both on the photo)
        if state["on"]:
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(w * 0.05, h * 0.1, w * 0.4, h * 0.8)
            cr.fill()
        cr.set_source_rgb(1, 1, 1) if not state["on"] else cr.set_source_rgb(0, 0, 0)
        cr.select_font_face("monospace")
        cr.set_font_size(h * 0.16)
        cr.move_to(w * 0.5, h * 0.55)
        cr.show_text(f"{int(t * 1000) % 100000:05d}")
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.set_font_size(h * 0.04)
        cr.move_to(w * 0.05, h * 0.97)
        cr.show_text("ms clock: PC screen and TV show different values; the difference is the end-to-end latency. Esc = quit")
        return False

    def tick():
        state["on"] = int((time.monotonic() - state["t0"]) / a.period) % 2 == 1
        area.queue_draw()
        return True

    win.connect("destroy", Gtk.main_quit)
    win.connect("key-press-event", lambda w, e: Gtk.main_quit() if e.keyval == 0xFF1B else None)
    area.connect("draw", draw)
    GLib.timeout_add(8, tick)           # ~120 Hz redraw, vsync decides the real rate
    win.fullscreen()
    win.show_all()
    Gtk.main()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tv", default=os.environ.get("AIRLINX_TV_IP"), help="TV address (or set AIRLINX_TV_IP)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", action="append", default=[])
    r.add_argument("--sweep", default="", help="comma list of target delays in ms")
    r.add_argument("--seconds", type=float, default=15)
    r.add_argument("--monitor", default="DP-4")
    r.add_argument("--bitrate", type=int, default=12_000_000)
    r.add_argument("--no-audio", action="store_true")
    r.add_argument("--wait-idle", type=float, default=0, help="seconds to wait for an idle TV before skipping")
    r.add_argument("--pause", type=float, default=4, help="seconds between sessions")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("idle")
    f = sub.add_parser("flash")
    f.add_argument("--period", type=float, default=1.0, help="seconds between square flips")
    a = ap.parse_args()
    if not a.tv and (a.cmd == "idle" or (a.cmd == "run" and not a.dry_run)):
        ap.error("pass --tv TV_IP or set AIRLINX_TV_IP")
    logging.basicConfig(level=logging.WARNING)
    if a.cmd == "flash":
        return cmd_flash(a)
    return asyncio.run({"run": cmd_run, "idle": cmd_idle}[a.cmd](a))


if __name__ == "__main__":
    sys.exit(main())
