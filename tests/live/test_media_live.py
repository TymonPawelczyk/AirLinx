"""Media playback on a real TV: Cast Default Media Receiver, then DLNA. 10 s test clip, never forced.

Replaces the assertions that scripts/smoke_media.py only printed.
"""
import asyncio
import os
import subprocess
import tempfile
import time

import pytest

from airlinx.core.cast import castv2
from airlinx.core.cast.media import IDLE_APPS, CastMediaPlayer
from airlinx.core.discovery import Discovery
from airlinx.core.dlna import DlnaPlayer
from airlinx.core.media_server import MediaServer
from airlinx.core.models import MediaItem

from livehelpers import ffmpeg_pids, require_idle, wait_ffmpeg_gone

pytestmark = pytest.mark.tv

PLAY_DEADLINE_S = 15


def make_clip(path: str) -> str:
    if not os.path.exists(path):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "10",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", "30",
                        "-c:a", "aac", "-b:a", "128k", path], check=True)
    return path


async def _wait_playing(player, seconds: float):
    """(reached_playing, max_position, last_state)."""
    t0, reached, pos = time.monotonic(), False, 0.0
    while time.monotonic() - t0 < seconds:
        await asyncio.sleep(0.5)
        st = player.state
        if st.state == "playing":
            reached = True
            pos = max(pos, st.position)
            if pos >= 3:
                break
        if st.state in ("ended", "error"):
            break
    return reached, pos, player.state


async def _run(tv_ip: str) -> dict:
    r: dict = {"ffmpeg_before": ffmpeg_pids()}
    disc = Discovery(lambda devs: None)
    await disc.start()
    try:
        tv = await disc.add_manual(tv_ip)
    finally:
        await disc.stop()
    if tv is None:
        pytest.skip(f"TV {tv_ip} not discoverable")
    r["caps"] = set(tv.caps)
    clip = make_clip(os.path.join(os.environ.get("AIRLINX_SCRATCH") or tempfile.gettempdir(), "airlinx_live_test.mkv"))
    item = MediaItem("live", clip, "AirLinx live test", 10.0, "video")
    server = MediaServer(lan_ip=None)
    await server.start()
    try:
        if "cast_media" in tv.caps:
            cast = CastMediaPlayer(tv, lambda s: None)
            await cast.connect()
            app_id, name = cast.running_app()
            if app_id not in IDLE_APPS:
                pytest.skip(f"TV busy: {name!r}")
            url, mime = await server.publish_async(item, "cast", target_ip=tv.ip)
            try:
                await cast.load(item, url, mime, subtitles=server.subtitle_tracks(url),
                                poster_url=server.poster_url(url))
                r["cast"] = await _wait_playing(cast, PLAY_DEADLINE_S)
            finally:
                await cast.stop()
                await cast.close()
            await asyncio.sleep(2)
            r["running_after_cast"] = await castv2.get_running_app(tv_ip, pin_key=tv.id or tv_ip)
        if "dlna" in tv.caps and not r.get("running_after_cast"):
            dl = DlnaPlayer(tv, lambda s: None)
            url, mime = await server.publish_async(item, "dlna", target_ip=tv.ip)
            try:
                await dl.load(item, url, mime, force=False)         # NEVER force
                r["dlna"] = await _wait_playing(dl, PLAY_DEADLINE_S)
            finally:
                await dl.stop()
                await dl.close()
            await asyncio.sleep(2)
    finally:
        await server.stop()
    r["running_after"] = await castv2.get_running_app(tv_ip, pin_key=tv.id or tv_ip)
    r["orphans"] = await wait_ffmpeg_gone(r["ffmpeg_before"])
    return r


@pytest.fixture(scope="module")
def run(tv_ip, pins_file):
    require_idle(tv_ip)
    return asyncio.run(_run(tv_ip))


def test_cast_plays_within_15_seconds_and_position_advances(run):
    if "cast" not in run:
        pytest.skip("TV has no Cast media capability")
    reached, pos, last = run["cast"]
    assert reached, f"Cast never reached 'playing' (last={last})"
    assert pos >= 3, f"position stuck at {pos:.1f}s"


def test_dlna_plays_within_15_seconds_and_position_advances(run):
    if "dlna" not in run:
        pytest.skip("TV has no DLNA renderer, or Cast left an app running")
    reached, pos, last = run["dlna"]
    assert reached, f"DLNA never reached 'playing' (last={last})"
    assert pos >= 3, f"position stuck at {pos:.1f}s"


def test_tv_is_idle_after_stop(run):
    assert run["running_after"] is None, f"TV still runs {run['running_after']}"


def test_no_orphaned_ffmpeg(run):
    assert run["orphans"] == set(), f"leftover ffmpeg pids {sorted(run['orphans'])}"
