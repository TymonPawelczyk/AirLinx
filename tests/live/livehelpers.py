"""Helpers shared by the live TV tests."""
import asyncio
import subprocess
import time

import pytest

from airlinx.core.cast import castv2


def ffmpeg_pids() -> set[int]:
    out = subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True, text=True).stdout
    return {int(x) for x in out.split()}


async def wait_ffmpeg_gone(baseline: set[int], timeout: float = 6.0) -> set[int]:
    """PIDs that appeared after `baseline` and are still alive after `timeout` seconds."""
    end = time.monotonic() + timeout
    while True:
        extra = ffmpeg_pids() - baseline
        if not extra or time.monotonic() > end:
            return extra
        await asyncio.sleep(0.25)


def require_idle(tv_ip) -> None:
    """Skip (never force) unless the TV runs no app and no ffmpeg from another session is active."""
    if ffmpeg_pids():
        pytest.skip(f"ffmpeg already running on this PC (pids {sorted(ffmpeg_pids())}): another AirLinx session?")
    try:
        running = asyncio.run(castv2.get_running_app(tv_ip, timeout=8))
    except Exception as e:
        pytest.skip(f"TV {tv_ip} unreachable: {type(e).__name__}: {e}")
    if running:
        pytest.skip(f"TV busy: {running.get('displayName') or running.get('appId')}")
