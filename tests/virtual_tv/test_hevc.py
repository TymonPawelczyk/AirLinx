"""HEVC mirroring end to end: the real MirrorSession (real ffmpeg encoders, Annex B parser, RTP, AES) against the
virtual Cast TV. The TV decodes what it receives through ffprobe/ffmpeg, so "the stream is valid HEVC" is checked
by a real decoder, not by our own parser.

Lives next to the other virtual-TV tests; skipped as a module when tools/virtual_tv is not part of the checkout.
"""
import asyncio
import shutil
import subprocess

import pytest

pytest.importorskip("tools.virtual_tv.cast_receiver", reason="needs the virtual TV (feature/virtual-tv)")

from airlinx.core import models  # noqa: E402
from airlinx.core.cast import mirror as mirror_mod  # noqa: E402
from airlinx.core.cast.mirror import MirrorSession  # noqa: E402

import vtvkit  # noqa: E402
from vtvkit import decode_errors, ffprobe_frames, make_device, synthetic_source, wait_until  # noqa: E402

LIVE_DEADLINE_S = 15


def hevc_nvenc_works() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=256x144:r=30",
                        "-frames:v", "3", "-c:v", "hevc_nvenc", "-f", "null", "-"], capture_output=True, timeout=30)
    return r.returncode == 0


ENCODERS = [pytest.param("libx265", id="libx265"),
            pytest.param("hevc_nvenc", id="hevc_nvenc",
                         marks=pytest.mark.skipif(not hevc_nvenc_works(), reason="no working hevc_nvenc"))]


@pytest.fixture
def hevc_pc(monkeypatch, synthetic):
    """The synthetic lavfi source from the shared fixture, plus an encoder probe that offers HEVC."""
    def use(encoder: str = "libx265", *, x264: bool = True):
        async def probe():
            return {"nvenc": False, "cuda_scale": False, "x264": x264,
                    "hevc_nvenc": encoder == "hevc_nvenc", "x265": encoder == "libx265"}
        monkeypatch.setattr(mirror_mod, "probe_encoders", probe)
    return use


class Run:
    def __init__(self, tv, opts):
        self.tv = tv
        self.states, self.stats, self.notices = [], [], []
        self.live = asyncio.Event()
        self.session = MirrorSession(make_device(tv), synthetic_source(), opts, self.stats.append, self._on_state)
        self.session.on_notice = self.notices.append

    def _on_state(self, state, detail):
        self.states.append((state, detail))
        if state == "live":
            self.live.set()

    async def start(self):
        await self.session.start()
        await asyncio.wait_for(self.live.wait(), LIVE_DEADLINE_S)
        return self


def opts(codec, **kw):
    kw.setdefault("fps", 60)
    kw.setdefault("bitrate", 6_000_000)
    kw.setdefault("audio", False)
    return models.MirrorOptions(codec=codec, **kw)


def offered(tv):
    return [o["offer"]["supportedStreams"][0]["codecName"] for o in tv.offers]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoder", ENCODERS)
async def test_hevc_session_streams_a_stream_a_real_decoder_accepts(make_tv, hevc_pc, encoder):
    hevc_pc(encoder)
    before = vtvkit.ffmpeg_pids()
    tv = await make_tv()
    run = await Run(tv, opts("hevc", audio=True)).start()
    plane = tv.plane
    f0 = len(plane.records("video"))
    await asyncio.sleep(4)
    fps = (len(plane.records("video")) - f0) / 4
    stats = run.stats[-1]
    await run.session.stop()

    assert offered(tv) == ["hevc"] and tv.offers[0]["offer"]["supportedStreams"][0]["level"] == "4.1"
    assert tv.recorder.video_codec == "hevc" and tv.recorder.video_path.name == "video.h265"
    assert fps >= 50, f"received only {fps:.1f} video frames/s"
    assert stats.encoder.startswith(encoder) and stats.width == 1280 and stats.height == 720
    st = plane.stats()
    assert st["video"]["dropped_before_sr"] == 0 and st["video"]["lost_given_up"] == 0 and st["malformed"] == 0
    assert st["video"]["keyframes"] >= 1
    info = ffprobe_frames(tv.recorder.video_path, "hevc")["streams"][0]
    assert (info["codec_name"], info["width"], info["height"]) == ("hevc", 1280, 720)
    assert int(info["nb_read_frames"]) == st["video"]["frames"]
    assert decode_errors(tv.recorder.video_path, "hevc") == "", "the decoder complained about the received H.265"
    assert vtvkit.ffmpeg_pids() <= before, "ffmpeg left running after STOP"


@pytest.mark.asyncio
async def test_auto_negotiates_hevc_with_a_tv_that_accepts_it(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    run = await Run(tv, opts("auto")).start()
    await run.session.stop()
    assert offered(tv) == ["hevc"] and tv.recorder.video_codec == "hevc"
    assert any(isinstance(p, dict) and p.get("type") == "GET_CAPABILITIES" for *_, p in tv.received)


@pytest.mark.asyncio
async def test_default_codec_stays_h264_and_does_not_query_capabilities(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    run = await Run(tv, opts("h264")).start()
    await run.session.stop()
    assert offered(tv) == ["h264"] and tv.recorder.video_codec == "h264"
    assert not any(isinstance(p, dict) and p.get("type") == "GET_CAPABILITIES" for *_, p in tv.received)


@pytest.mark.asyncio
@pytest.mark.parametrize("pref", ["auto", "hevc"])
async def test_tv_that_rejects_hevc_gets_h264_in_the_same_session(make_tv, hevc_pc, pref):
    hevc_pc("libx265")
    tv = await make_tv(codecs=("h264",))             # `--codecs h264`: ANSWER error for an HEVC OFFER
    run = await Run(tv, opts(pref)).start()
    await asyncio.sleep(2)
    frames = tv.plane.stats()["video"]["frames"]
    await run.session.stop()
    assert offered(tv) == ["hevc", "h264"]
    assert tv.recorder.video_codec == "h264"
    assert frames > 60
    assert run.stats[-1].encoder == "libx264"
    assert any("H.264" in n for n in run.notices)
    assert decode_errors(tv.recorder.video_path, "h264") == ""
    assert not [s for s, _ in run.states if s == "error"]


@pytest.mark.asyncio
async def test_hevc_receiver_pli_gets_a_new_keyframe(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    run = await Run(tv, opts("hevc")).start()
    await asyncio.sleep(2.5)                   # GOP is 2 s: the next natural keyframe is far away
    keys = tv.plane.stats()["video"]["keyframes"]
    tv.plane.send_pli()
    await wait_until(lambda: tv.plane.stats()["video"]["keyframes"] > keys, 8, "a keyframe after the PLI")
    await run.session.stop()
    assert decode_errors(tv.recorder.video_path, "hevc") == ""


@pytest.mark.asyncio
async def test_hevc_survives_packet_loss_and_everything_still_decodes(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    lost = set()

    def drop(pkt, n):
        if pkt.pt == 96 and n % 50 == 0 and (pkt.fid8, pkt.pid) not in lost:
            lost.add((pkt.fid8, pkt.pid))
            return True
        return False
    tv.drop = drop
    run = await Run(tv, opts("hevc", bitrate=8_000_000)).start()
    plane = tv.plane
    await asyncio.sleep(5)
    await run.session.stop()
    st = plane.stats()
    assert st["dropped_by_fault"] > 10 and st["video"]["lost_given_up"] == 0
    assert decode_errors(tv.recorder.video_path, "hevc") == ""


@pytest.mark.asyncio
async def test_hevc_bitrate_change_is_applied_live(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    run = await Run(tv, opts("hevc", bitrate=12_000_000)).start()
    plane = tv.plane
    await asyncio.sleep(3)
    n = len(plane.records("video"))
    await run.session.update(opts("hevc", bitrate=2_000_000))
    assert len(tv.offers) == 1                   # same OFFER: only the encoder restarts
    await wait_until(lambda: len(plane.records("video")) > n + 120, 10, "frames after the bitrate change")
    await run.session.stop()
    assert plane.stats()["video"]["keyframes"] >= 2
    assert decode_errors(tv.recorder.video_path, "hevc") == "", "the switch to the new encoder broke decoding"


@pytest.mark.asyncio
async def test_switching_codec_while_live_renegotiates(make_tv, hevc_pc):
    hevc_pc("libx265")
    tv = await make_tv()
    run = await Run(tv, opts("h264")).start()
    await asyncio.sleep(1.5)
    await run.session.update(opts("hevc"))
    await wait_until(lambda: len(tv.offers) == 2, 10, "a second OFFER")
    await asyncio.sleep(2)
    await run.session.stop()
    assert offered(tv) == ["h264", "hevc"]
    assert run.stats[-1].encoder.startswith("libx265")
