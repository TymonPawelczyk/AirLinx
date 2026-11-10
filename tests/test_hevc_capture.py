"""HEVC capture: Annex B access-unit parser, ffmpeg command line, encoder probe, reader."""
import asyncio
import random
import shutil
import subprocess
from typing import Any

import pytest

from airlinx.core import capture as c

needs_x265 = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


# ---------------------------------------------------------------------------------- helpers
def nal(t: int, payload: bytes = b"\x11\x22", *, short: bool = False) -> bytes:
    """One Annex B NAL of HEVC type ``t`` (payload must not contain 00 00 0x)."""
    return (b"\x00\x00\x01" if short else b"\x00\x00\x00\x01") + bytes([t << 1, 1]) + payload


AUD, VPS, SPS, PPS = nal(35, b"\x50"), nal(32, b"\xaa"), nal(33, b"\xbb"), nal(34, b"\xcc")


def au_key(payload=b"\x77\x77"):
    return AUD + VPS + SPS + PPS + nal(19, payload)


def au_delta(payload=b"\x55"):
    return AUD + nal(1, payload)


def feed_all(parser, blob, step):
    out = []
    for i in range(0, len(blob), step):
        out += parser.feed(blob[i:i + step])
    return out


def nal_types(data: bytes) -> list[int]:
    return [(data[i + 3] >> 1) & 0x3F for i in range(len(data) - 3)
            if data[i:i + 3] == b"\x00\x00\x01"]


@pytest.fixture(scope="module")
def real_stream():
    """A short real libx265 Annex B stream with AUDs: 30 frames, an IDR every 10."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    r = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1", "-c:v", "libx265",
         "-preset", "ultrafast", "-x265-params", "aud=1:bframes=0:keyint=10:min-keyint=10:repeat-headers=1:log-level=none",
         "-pix_fmt", "yuv420p", "-f", "hevc", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        pytest.skip("libx265 unavailable")
    return r.stdout


# ---------------------------------------------------------------------------------- parser
def test_real_stream_is_split_into_access_units_with_keyframes(real_stream):
    p = c.HevcParser()
    aus = p.feed(real_stream) + p.flush()
    assert len(aus) == 30
    assert [i for i, a in enumerate(aus) if a.key] == [0, 10, 20]
    assert b"".join(a.data for a in aus) == real_stream          # nothing lost, nothing added
    for a in aus:
        types = nal_types(a.data)
        assert types[0] == 35                                     # every AU starts with its AUD
        assert (a.key and {32, 33, 34} <= set(types)) or not a.key
    assert all(set(nal_types(a.data)) & {1, 19, 20, 21} for a in aus)   # x265 starts GOPs with IDR_N_LP or CRA


@pytest.mark.parametrize("step", [1, 2, 3, 7, 64, 4096])
def test_chunking_does_not_change_the_result(real_stream, step):
    ref = c.HevcParser()
    want = ref.feed(real_stream) + ref.flush()
    p = c.HevcParser()
    got = feed_all(p, real_stream, step) + p.flush()
    assert [(a.data, a.key) for a in got] == [(a.data, a.key) for a in want]


def test_aud_start_code_split_across_buffers():
    blob = au_key() + au_delta() + au_delta() + au_key() + au_delta()
    # cut inside every possible place of the stream, including the middle of each AUD start code
    for cut in range(1, len(blob)):
        p = c.HevcParser()
        got = p.feed(blob[:cut]) + p.feed(blob[cut:]) + p.flush()
        assert [a.key for a in got] == [True, False, False, True, False], cut
        assert b"".join(a.data for a in got) == blob


def test_access_unit_is_held_until_the_next_aud_or_flush():
    p = c.HevcParser()
    assert p.feed(au_key()) == []
    assert [a.key for a in p.feed(au_delta())] == [True]
    assert [a.key for a in p.flush()] == [False]
    assert p.flush() == []


@pytest.mark.parametrize("t,key", [(0, False), (1, False), (9, False), (16, True), (17, True), (18, True),
                                    (19, True), (20, True), (21, True)])
def test_irap_nal_types_mark_keyframes(t, key):
    p = c.HevcParser()
    got = p.feed(AUD + VPS + SPS + PPS + nal(t, b"\x01") + AUD) + p.flush()
    assert [a.key for a in got] == [key]


def test_empty_access_units_are_dropped():
    blob = AUD + AUD + au_key() + AUD + nal(39, b"\x01") + au_delta() + AUD   # AUD-only, SEI-only
    p = c.HevcParser()
    got = p.feed(blob) + p.flush()
    assert [a.key for a in got] == [True, False]
    assert nal_types(got[1].data) == [35, 1]


def test_key_access_unit_without_parameter_sets_gets_the_cached_ones():
    p = c.HevcParser()
    got = p.feed(au_key() + au_delta() + AUD + nal(19, b"\x99") + au_delta()) + p.flush()
    assert [a.key for a in got] == [True, False, True, False]
    third = got[2].data
    assert nal_types(third) == [35, 32, 33, 34, 19]      # AUD first, then VPS/SPS/PPS, then the slice
    assert third.endswith(nal(19, b"\x99"))
    assert nal_types(got[3].data) == [35, 1]              # delta frames are never touched


def test_key_access_unit_with_partial_parameter_sets_is_completed():
    p = c.HevcParser()
    got = p.feed(au_key() + AUD + SPS + nal(19, b"\x99") + au_delta()) + p.flush()
    assert nal_types(got[1].data) == [35, 32, 33, 34, 19]
    assert got[1].data.count(SPS) == 1               # the SPS the frame already had is not duplicated


def test_first_keyframe_without_any_parameter_sets_is_passed_through():
    p = c.HevcParser()
    got = p.feed(AUD + nal(19, b"\x99") + au_delta()) + p.flush()
    assert [a.key for a in got] == [True, False] and nal_types(got[0].data) == [35, 19]


def test_garbage_before_the_first_aud_is_discarded_and_counted():
    p = c.HevcParser()
    got = p.feed(b"\x01\x02\x03tail-of-a-torn-frame" + au_key() + au_delta()) + p.flush()
    assert [a.key for a in got] == [True, False] and got[0].data == au_key()
    assert p.discarded_bytes == len(b"\x01\x02\x03tail-of-a-torn-frame")


def test_three_byte_start_codes_are_accepted():
    aud3 = nal(35, b"\x50", short=True)
    blob = aud3 + nal(19, b"\x01", short=True) + aud3 + nal(1, b"\x01", short=True)
    p = c.HevcParser()
    got = p.feed(blob) + p.flush()
    assert [a.key for a in got] == [True, False] and b"".join(a.data for a in got) == blob


def test_arrival_time_is_the_chunk_in_which_the_access_unit_started():
    p = c.HevcParser()
    assert p.feed(au_key()[:6], now=1.0) == []                          # key AU's AUD is complete here
    assert p.feed(au_key()[6:] + au_delta()[:3], now=2.0) == []         # next AUD still incomplete
    got = p.feed(au_delta()[3:] + au_key(), now=3.0)
    assert [(a.key, a.t) for a in got] == [(True, 1.0), (False, 3.0)]   # delta's AUD completed in chunk 3
    assert [(a.key, a.t) for a in p.flush()] == [(True, 3.0)]


def test_flush_then_the_late_rest_of_the_frame_is_resynchronised():
    blob = au_key(b"\x77" * 50)
    p = c.HevcParser()
    p.feed(blob[:40])
    torn = p.flush()                                   # idle flush fired in the middle of a frame
    assert len(torn) == 1
    got = p.feed(blob[40:] + au_delta()) + p.flush()   # the rest arrives later
    assert [a.key for a in got] == [False]             # the stray tail is dropped, the next frame is fine
    assert p.discarded_bytes == len(blob) - 40


def test_parser_survives_random_damage():
    rng = random.Random(3)
    base = au_key() + au_delta() * 3 + au_key()
    for _ in range(300):
        blob = bytearray(base)
        for _ in range(rng.randint(1, 6)):
            blob[rng.randrange(len(blob))] = rng.getrandbits(8)
        p = c.HevcParser()
        feed_all(p, bytes(blob), rng.randint(1, 20))
        p.flush()


# ---------------------------------------------------------------------------------- command line
def hevc_params(**kw):
    return c.VideoParams(geometry=(0, 420, 4096, 2304), out_w=2560, out_h=1440, fps=60, bitrate=18_000_000,
                         encoder="hevc_nvenc", **kw)


def test_hevc_nvenc_cmd_is_low_latency_annex_b_with_aud():
    cmd = c.build_video_cmd(hevc_params(), ":1")
    s = " ".join(cmd)
    assert "-c:v hevc_nvenc" in s and "hwupload_cuda,scale_cuda=2560:1440" in s
    for flag in ("-preset p1", "-tune ull", "-zerolatency 1", "-delay 0", "-rc cbr", "-forced-idr 1", "-bf 0",
                 "-aud 1", "-g 120", "-b:v 18000000", "-maxrate 18000000", "-profile:v main", "-f hevc"):
        assert flag in s, flag
    assert "flv" not in s and "libx264" not in s and "-profile:v main" in s
    assert cmd[-1] == "pipe:1"


def test_hevc_nvenc_cmd_cpu_scale_and_gop():
    s = " ".join(c.build_video_cmd(hevc_params(gpu_scale=False, gop_s=4.0), ":1"))
    assert "scale=2560:1440:flags=fast_bilinear,format=nv12" in s and "-g 240" in s


def test_libx265_cmd_is_ultrafast_zerolatency_with_aud_and_repeated_headers():
    p = hevc_params()
    p.encoder = "libx265"
    cmd = c.build_video_cmd(p, ":1")
    s = " ".join(cmd)
    assert "-c:v libx265" in s and "-preset ultrafast" in s and "-tune zerolatency" in s and "-f hevc" in s
    params = cmd[cmd.index("-x265-params") + 1]
    for part in ("aud=1", "repeat-headers=1", "bframes=0", "keyint=120", "scenecut=0"):
        assert part in params.split(":"), part
    assert "-pix_fmt yuv420p" in s and "nvenc" not in s and "-rc cbr" not in s


def test_codec_of_encoder_and_labels():
    assert c.codec_of("h264_nvenc") == c.codec_of("libx264") == "h264"
    assert c.codec_of("hevc_nvenc") == c.codec_of("libx265") == "hevc"
    v = c.VideoCapture(hevc_params(), lambda f: None, lambda m: None)
    assert v.encoder_label == "hevc_nvenc + scale_cuda"
    v.params.encoder = "libx265"
    assert v.encoder_label == "libx265"


# ---------------------------------------------------------------------------------- probe
def test_probe_detects_hevc_encoders(monkeypatch):
    ran = []

    async def fake_ok(args, timeout=8.0):
        ran.append(args)
        return "hevc_nvenc" in args or "libx265" in args or "h264_nvenc" in args

    monkeypatch.setattr(c, "_ffmpeg_ok", fake_ok)
    monkeypatch.setattr(c, "_caps", {})
    caps = asyncio.run(c.probe_encoders())
    assert caps["nvenc"] and caps["hevc_nvenc"] and caps["x265"] and not caps["x264"]


def test_probe_reports_missing_hevc_encoders(monkeypatch):
    async def fake_ok(args, timeout=8.0):
        return "libx264" in args

    monkeypatch.setattr(c, "_ffmpeg_ok", fake_ok)
    monkeypatch.setattr(c, "_caps", {})
    caps = asyncio.run(c.probe_encoders())
    assert caps["hevc_nvenc"] is False and caps["x265"] is False and caps["x264"] is True


def test_hevc_encoder_choice():
    assert c.hevc_encoder_from_caps({"hevc_nvenc": True, "cuda_scale": True, "x265": True}) == ("hevc_nvenc", True)
    assert c.hevc_encoder_from_caps({"hevc_nvenc": True, "cuda_scale": False}) == ("hevc_nvenc", False)
    assert c.hevc_encoder_from_caps({"x265": True}) == ("libx265", False)
    assert c.hevc_encoder_from_caps({"nvenc": True, "x264": True}) is None


# ---------------------------------------------------------------------------------- reader
PARAMS: dict[str, Any] = dict(geometry=(0, 0, 64, 64), out_w=64, out_h=64, encoder="hevc_nvenc")


def test_video_capture_delivers_hevc_frames_with_keyflags_and_rising_capture_times(monkeypatch, tmp_path,
                                                                                    real_stream):
    f = tmp_path / "s.hevc"
    f.write_bytes(real_stream)
    monkeypatch.setattr(c, "build_video_cmd", lambda p, d=None: ["sh", "-c", f"cat {f}; exec sleep 30"])

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

    got = asyncio.run(asyncio.wait_for(main(), 15))
    assert len(got) == 30                                  # the last AU too: flushed when the pipe went idle
    assert [i for i, x in enumerate(got) if x.key] == [0, 10, 20]
    caps = [x.cap for x in got]
    assert all(b > a for a, b in zip(caps, caps[1:]))
    assert all(x.arrival >= x.cap for x in got)


def test_hevc_encoder_that_dies_is_fatal_after_the_budget(monkeypatch):
    monkeypatch.setattr(c, "build_video_cmd", lambda p, d=None: ["sh", "-c", "echo boom >&2; exit 1"])
    fatal = []

    async def main():
        v = c.VideoCapture(c.VideoParams(**PARAMS), lambda f: None, fatal.append)
        await v.start()
        for _ in range(100):
            if fatal:
                break
            await asyncio.sleep(0.01)
        await v.stop()

    asyncio.run(asyncio.wait_for(main(), 15))
    assert fatal and "boom" in fatal[0]
