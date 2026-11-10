"""Offline tests for ffmpeg pipelines (airlinx.core.capture). ffmpeg-based tests skip without ffmpeg."""
import shutil
import subprocess

import pytest

from airlinx.core import capture as c

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_output_size_keeps_aspect_even():
    assert c.output_size(4096, 2304, 1080) == (1920, 1080)
    assert c.output_size(5120, 2880, 720) == (1280, 720)
    assert c.output_size(2032, 2156, 1080) == (1018, 1080)
    assert c.output_size(1001, 601, 1080) == (1000, 600)   # no upscaling, even dims
    assert c.output_size(10000, 1000, 1080) == (3840, 384)  # width cap


def test_video_cmd_low_latency_flags():
    p = c.VideoParams(geometry=(0, 420, 4096, 2304), out_w=1920, out_h=1080, fps=60, bitrate=12_000_000)
    cmd = c.build_video_cmd(p, ":1")
    s = " ".join(cmd)
    assert "-i :1+0,420" in s and "-video_size 4096x2304" in s
    assert "hwupload_cuda,scale_cuda=1920:1080" in s
    for flag in ("-preset p1", "-tune ull", "-bf 0", "-g 120", "-rc cbr", "-b:v 12000000", "-f flv"):
        assert flag in s
    p.gpu_scale = False
    assert "scale=1920:1080:flags=fast_bilinear" in " ".join(c.build_video_cmd(p, ":1"))
    p.encoder = "libx264"
    s2 = " ".join(c.build_video_cmd(p, ":1"))
    assert "libx264" in s2 and "zerolatency" in s2 and "-bf 0" in s2


def test_audio_cmd():
    s = " ".join(c.build_audio_cmd("sink.monitor"))
    assert "-f pulse" in s and "-i sink.monitor" in s and "-frame_duration 10" in s and "-f ogg" in s


def test_audio_cmd_single_source_list_equals_string():
    assert c.build_audio_cmd(["a.monitor"]) == c.build_audio_cmd("a.monitor")
    assert "-filter_complex" not in c.build_audio_cmd(["@DEFAULT_SOURCE@"])


def test_audio_cmd_mixes_two_sources_into_one_opus_stream():
    cmd = c.build_audio_cmd(["sink.monitor", "mic"])
    s = " ".join(cmd)
    assert cmd.count("pulse") == 2 and "-i sink.monitor" in s and "-i mic" in s
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:duration=longest:normalize=0" in fc
    assert fc.count("aresample=async=") == 2          # each live input drift-compensated vs wallclock
    assert cmd[cmd.index("-map") + 1] == "[aout]"
    assert s.count("-c:a libopus") == 1 and "-frame_duration 10" in s and s.endswith("-f ogg pipe:1")


def test_pulse_inputs_helper_for_other_muxers():
    args, maps = c.pulse_audio_args(["m", "x"], first_index=1)
    s = " ".join(args)
    assert s.count("-f pulse") == 2 and "[1:a]" in s and "[2:a]" in s and maps == ["-map", "[aout]"]
    args, maps = c.pulse_audio_args(["m"], first_index=1)
    assert maps == ["-map", "1:a"] and "-filter_complex" not in args


def test_opus_samples_toc():
    assert c.opus_samples(bytes([0xFC])) == 960       # CELT FB 20 ms
    assert c.opus_samples(bytes([(16 << 3) | 0])) == 120  # CELT NB 2.5 ms
    assert c.opus_samples(bytes([(12 << 3) | 1])) == 960  # hybrid 10 ms ×2
    assert c.opus_samples(b"") == 0


@needs_ffmpeg
def test_opus_ogg_incremental_parse():
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", "-ac", "2",
                          "-c:a", "libopus", "-frame_duration", "10", "-page_duration", "10000", "-f", "ogg", "-"],
                         capture_output=True, check=True).stdout
    p = c.OggOpusParser()
    pkts = []
    for i in range(0, len(out), 333):  # odd chunking exercises the incremental path
        pkts += p.feed(out[i:i + 333])
    assert 95 <= len(pkts) <= 105 and all(c.opus_samples(x) == 480 for x in pkts)


@needs_ffmpeg
def test_flv_h264_incremental_parse():
    enc = "libx264"
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2",
                          "-c:v", enc, "-bf", "0", "-g", "30", "-pix_fmt", "yuv420p", "-f", "flv", "-"],
                         capture_output=True)
    if out.returncode != 0:
        pytest.skip("libx264 unavailable")
    p = c.FlvParser()
    frames = []
    for i in range(0, len(out.stdout), 1000):
        frames += p.feed(out.stdout[i:i + 1000])
    assert len(frames) == 60
    keys = [i for i, f in enumerate(frames) if f.key]
    assert keys == [0, 30]
    for i in keys:  # SPS (7) + PPS (8) precede every IDR, Annex-B start codes
        f = frames[i].data
        assert f.startswith(c.START_CODE)
        types = [f[j + 4] & 0x1F for j in range(len(f) - 4) if f[j:j + 4] == c.START_CODE]
        assert 7 in types and 8 in types and 5 in types
    ts = [f.ts_ms for f in frames]
    assert ts == sorted(ts) and ts[-1] - ts[0] in range(1900, 2000)


def test_flv_rejects_garbage():
    with pytest.raises(ValueError):
        c.FlvParser().feed(b"NOTFLV" + bytes(20))


def _sleepy(monkeypatch):
    """Encoder stand-in that never produces a frame (like a stuck NVENC init)."""
    monkeypatch.setattr(c, "build_video_cmd", lambda p, d=None: ["sleep", "30"])


def test_stop_during_restart_kills_all_processes(monkeypatch):
    import asyncio
    _sleepy(monkeypatch)

    async def main():
        v = c.VideoCapture(c.VideoParams(geometry=(0, 0, 64, 64), out_w=64, out_h=64), lambda f: None,
                           lambda m: None)
        await v.start()
        assert v.running_processes == 1
        r = asyncio.create_task(v.restart())
        await asyncio.sleep(0.2)
        assert v.running_processes == 2
        t0 = asyncio.get_running_loop().time()
        await v.stop()
        assert await r is False
        assert asyncio.get_running_loop().time() - t0 < 3.0   # restart didn't wait for its timeout
        assert v.running_processes == 0 and not v._tasks
        assert await v.restart() is False and not v._tasks     # no spawn after stop
    asyncio.run(asyncio.wait_for(main(), 10))


def test_spawn_racing_stop_leaves_nothing(monkeypatch):
    import asyncio
    _sleepy(monkeypatch)

    async def main():
        v = c.VideoCapture(c.VideoParams(geometry=(0, 0, 64, 64), out_w=64, out_h=64), lambda f: None,
                           lambda m: None)
        start = asyncio.create_task(v.start())
        await asyncio.sleep(0)       # spawn in flight
        await v.stop()
        await start
        await asyncio.sleep(0.1)
        assert not v._tasks and v._active is None
        a = c.AudioCapture("x.monitor", lambda pk, cap: None, lambda m: None)
        monkeypatch.setattr(c, "build_audio_cmd", lambda src, br=128000: ["sleep", "30"])
        at = asyncio.create_task(a.start())
        await asyncio.sleep(0)
        await a.stop()
        await at
        assert a._proc is None or a._proc.proc is None or a._proc.proc.returncode is not None
    asyncio.run(asyncio.wait_for(main(), 10))
