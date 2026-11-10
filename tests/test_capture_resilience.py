"""Encoder crash handling in core/capture.py with stand-in processes (no ffmpeg, no X): a crash loop must end
in on_fatal with the encoder's last words, never in an endless respawn storm, and stop() must always win."""
import asyncio
import random

import pytest

from airlinx.core import capture as c

PARAMS = dict(geometry=(0, 0, 64, 64), out_w=64, out_h=64)


def run(coro, timeout=15):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_video_crash_loop_ends_in_fatal_with_stderr_tail(monkeypatch):
    spawns = []
    monkeypatch.setattr(c, "build_video_cmd",
                        lambda p, d=None: spawns.append(1) or ["sh", "-c", "echo 'NVENC: no capable device' >&2; exit 3"])

    async def main():
        fatal = asyncio.get_running_loop().create_future()
        v = c.VideoCapture(c.VideoParams(**PARAMS), lambda f: None, lambda msg: fatal.done() or fatal.set_result(msg))
        await v.start()
        msg = await asyncio.wait_for(fatal, 12)
        await v.stop()
        return msg, v

    msg, v = run(main())
    assert "NVENC: no capable device" in msg
    assert len(spawns) == c.MAX_FAILS, "exactly the failure budget, then give up"
    assert v.running_processes == 0 and not v._tasks


def test_audio_crash_loop_ends_in_fatal_with_stderr_tail(monkeypatch):
    spawns = []
    monkeypatch.setattr(c, "build_audio_cmd",
                        lambda src, br=128000: spawns.append(1) or ["sh", "-c", "echo 'pulse: connection refused' >&2; exit 1"])

    async def main():
        fatal = asyncio.get_running_loop().create_future()
        a = c.AudioCapture("x.monitor", lambda pk, cap: None, lambda msg: fatal.done() or fatal.set_result(msg))
        await a.start()
        msg = await asyncio.wait_for(fatal, 12)
        await a.stop()
        return msg

    msg = run(main())
    assert "pulse: connection refused" in msg and len(spawns) == c.MAX_FAILS


def test_stop_cancels_a_pending_respawn(monkeypatch):
    monkeypatch.setattr(c, "build_video_cmd", lambda p, d=None: ["sh", "-c", "exit 1"])

    async def main():
        fatal = []
        v = c.VideoCapture(c.VideoParams(**PARAMS), lambda f: None, fatal.append)
        await v.start()
        await asyncio.sleep(0.1)       # first crash seen, respawn pending
        await v.stop()
        await asyncio.sleep(0.8)       # would have respawned by now
        return fatal, v

    fatal, v = run(main())
    assert fatal == [] and v.running_processes == 0 and v._active is None


def test_failure_budget_forgets_old_failures(monkeypatch):
    b = c._FailureBudget()
    t = [1000.0]
    monkeypatch.setattr(c.time, "monotonic", lambda: t[0])
    for _ in range(c.MAX_FAILS - 1):
        assert b.hit() is False
        t[0] += 1
    t[0] += c.FAIL_WINDOW_S + 1          # long quiet period
    assert b.hit() is False, "old failures must age out of the window"
    for _ in range(c.MAX_FAILS - 2):
        assert b.hit() is False
    assert b.hit() is True


def test_video_frames_flow_to_the_callback_with_monotonic_capture_times(monkeypatch):
    """A real FLV stream from a stand-in process: frames arrive in order with sane timestamps."""
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg needed to build the FLV fixture")
    flv = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=1",
                          "-c:v", "libx264", "-bf", "0", "-g", "15", "-pix_fmt", "yuv420p", "-f", "flv", "-"],
                         capture_output=True)
    if flv.returncode != 0:
        pytest.skip("libx264 unavailable")
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".flv") as f:
        f.write(flv.stdout)
    monkeypatch.setattr(c, "build_video_cmd", lambda p, d=None: ["sh", "-c", f"cat {f.name}; sleep 30"])

    async def main():
        got = []
        v = c.VideoCapture(c.VideoParams(**PARAMS), got.append, lambda m: None)
        await v.start()
        for _ in range(100):
            if len(got) >= 30:
                break
            await asyncio.sleep(0.05)
        await v.stop()
        return got

    try:
        got = run(main())
    finally:
        os.unlink(f.name)
    assert len(got) == 30
    assert got[0].key and [x.key for x in got].count(True) == 2
    caps = [x.cap for x in got]
    assert caps == sorted(caps)
    assert all(x.arrival >= x.cap - 1 for x in got)


def test_flv_parser_is_robust_to_damage_after_a_valid_header():
    rng = random.Random(7)
    header = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"
    for _ in range(200):
        p = c.FlvParser()
        blob = header + bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 400)))
        try:
            for i in range(0, len(blob), 37):
                p.feed(blob[i:i + 37])
        except ValueError:
            pass            # rejecting garbage is fine; any other exception is a bug
