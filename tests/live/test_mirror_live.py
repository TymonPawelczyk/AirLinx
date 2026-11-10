"""Screen mirroring to a real TV over Cast: device-auth, time to live, fps, audio acks, clean STOP.

One 12 s session is run once per module; each test asserts one property of that run.
Replaces the assertions that scripts/smoke_mirror.py only printed.
"""
import asyncio
import json
import os
import statistics
import time

import pytest

from airlinx.core import models
from airlinx.core.cast import castv2
from airlinx.core.cast.mirror import MirrorSession, TvBusyError
from airlinx.core.sources import list_sources

from livehelpers import ffmpeg_pids, require_idle, wait_ffmpeg_gone

pytestmark = pytest.mark.tv

LIVE_DEADLINE_S = 15
SAMPLE_S = 12
WARMUP_S = 3


async def _session(tv_ip: str) -> dict:
    srcs = [s for s in await list_sources(with_thumbnails=False) if s.kind == "monitor"]
    if not srcs:
        pytest.skip("no monitor to capture (is DISPLAY set?)")
    src = next((s for s in srcs if os.environ.get("AIRLINX_MONITOR", "") in s.id), srcs[0])
    dev = models.Device(id=tv_ip, name="TV", model="", ip=tv_ip, cast_port=8009, caps={"cast_mirror"})
    r: dict = {"states": [], "stats": [], "ffmpeg_before": ffmpeg_pids()}
    t0 = time.monotonic()
    live = asyncio.Event()

    def on_state(state, detail):
        r["states"].append((time.monotonic() - t0, state, detail))
        if state == "live":
            r["t_live"] = time.monotonic() - t0
            live.set()

    def on_stats(st):
        r["stats"].append((time.monotonic() - t0, st))

    s = MirrorSession(dev, src, models.MirrorOptions(audio=True, fps=60, bitrate=12_000_000), on_stats, on_state)
    try:
        try:
            await s.start(force=False)                      # NEVER force
        except TvBusyError as e:
            pytest.skip(f"TV became busy: {e}")
        try:
            await asyncio.wait_for(live.wait(), LIVE_DEADLINE_S)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(SAMPLE_S)
        m = s._media  # noqa: SLF001 (diagnostics only, as in the smoke script)
        if m is not None:
            r["video"] = m.video.snapshot()
            r["audio"] = m.audio.snapshot() if m.audio else None
    finally:
        await s.stop()
    r["state_after_stop"] = s.state
    r["running_after"] = await castv2.get_running_app(tv_ip, pin_key=tv_ip)
    r["orphans"] = await wait_ffmpeg_gone(r["ffmpeg_before"])
    pins = json.load(open(os.environ["AIRLINX_KNOWN_TVS"])) if os.path.exists(os.environ["AIRLINX_KNOWN_TVS"]) else {}
    r["pin"] = pins.get(tv_ip)
    return r


@pytest.fixture(scope="module")
def run(tv_ip, pins_file):
    require_idle(tv_ip)
    return asyncio.run(_session(tv_ip))


def test_device_auth_verified_and_identity_pinned(run):
    assert run["pin"] is not None, "TV identity was not pinned"
    assert run["pin"]["auth"] == "device", f"device-auth did not succeed, fell back to {run['pin']['auth']!r}"
    assert run["pin"]["device_subject"]


def test_reaches_live_within_15_seconds(run):
    assert "t_live" in run, f"never live; states: {run['states']}"
    assert run["t_live"] <= LIVE_DEADLINE_S, f"live after {run['t_live']:.1f}s"
    assert not [x for x in run["states"] if x[1] == "error"], run["states"]


def test_frame_rate_is_at_least_50_fps(run):
    t_live = run["t_live"]
    fps = [st.fps for t, st in run["stats"] if t >= t_live + WARMUP_S]
    assert len(fps) >= 3, f"too few stats samples: {len(fps)}"
    assert statistics.median(fps) >= 50, f"median fps {statistics.median(fps):.1f}, samples {fps}"


def test_stats_report_sane_latency_and_resolution(run):
    last = run["stats"][-1][1]
    assert last.width and last.height <= 1080
    assert last.loss < 0.05
    assert last.latency_ms is None or last.latency_ms < 500


def test_at_least_95_percent_of_audio_frames_acked(run):
    audio = run["audio"]
    assert audio is not None, "no audio stream was negotiated"
    assert audio["frames"] > 0
    ratio = audio["acked"] / audio["frames"]
    assert ratio >= 0.95, f"audio acked {audio['acked']}/{audio['frames']} = {ratio:.1%}"


def test_stop_leaves_tv_idle(run):
    assert run["state_after_stop"] == "stopped"
    assert run["running_after"] is None, f"TV still runs {run['running_after']}"


def test_no_orphaned_ffmpeg_after_stop(run):
    assert run["orphans"] == set(), f"leftover ffmpeg pids {sorted(run['orphans'])}"
