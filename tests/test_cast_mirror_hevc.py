"""HEVC negotiation in MirrorSession: OFFER codec, TV capabilities, fallback to H.264."""
import asyncio
import dataclasses

import pytest

from airlinx.core import models
from airlinx.core.cast import castv2 as cv
from airlinx.core.cast import mirror as mm

from test_cast_mirror import FakeTv, FakeVideo, isolated_pins, make_session, patched, wait_until  # noqa: F401

CAPS_HEVC = {"nvenc": True, "cuda_scale": True, "x264": True, "hevc_nvenc": True, "x265": True}
TV_CAPS = ["aac", "opus", "vp9", "h264", "hevc", "vp8", "audio"]


# ---------------------------------------------------------------------------------- pure helpers
def test_hevc_levels_follow_picture_size_and_rate():
    assert mm.hevc_level(1920, 1080, 60) == "4.1"
    assert mm.hevc_level(2560, 1440, 60) == "5.0"
    assert mm.hevc_level(3840, 2160, 30) == "5.0"
    assert mm.hevc_level(3840, 2160, 60) == "5.1"


def test_hevc_offer_names_the_codec_and_keeps_the_h264_one_unchanged():
    keys = mm.StreamKeys.random()
    h = mm.build_offer(video=keys, audio=None, width=2560, height=1440, fps=60, bitrate=12_000_000,
                       target_delay_ms=100, codec="hevc")["offer"]["supportedStreams"][0]
    assert h["codecName"] == "hevc" and h["profile"] == "main" and h["level"] == "5.0"
    assert "codecParameter" not in h          # optional; Chrome omits it, and it must start with "hev1" if sent
    assert h["maxBitRate"] == 12_000_000 and h["timeBase"] == "1/90000" and h["rtpPayloadType"] == mm.VIDEO_PT
    d = mm.build_offer(video=keys, audio=None, width=1920, height=1080, fps=60, bitrate=12_000_000,
                       target_delay_ms=100)["offer"]["supportedStreams"][0]
    assert d["codecName"] == "h264" and d["level"] == "4" and d["profile"] == "main"


def test_tv_codecs_are_read_from_the_capabilities_reply():
    ok = {"capabilities": {"mediaCaps": TV_CAPS, "remoting": 2}}
    assert mm.parse_tv_codecs(ok) == {"aac", "opus", "vp9", "h264", "hevc", "vp8", "audio"}
    for bad in (None, {}, {"capabilities": None}, {"capabilities": {}}, {"capabilities": {"mediaCaps": "hevc"}}):
        assert mm.parse_tv_codecs(bad) is None
    assert mm.parse_tv_codecs({"capabilities": {"mediaCaps": [1, None, "hevc"]}}) == {"hevc"}


@pytest.mark.parametrize("pref,caps,tv,want", [
    ("h264", CAPS_HEVC, {"h264", "hevc"}, ["h264"]),
    ("auto", CAPS_HEVC, {"h264", "hevc"}, ["hevc", "h264"]),
    ("auto", CAPS_HEVC, {"h264"}, ["h264"]),                      # TV does not list hevc
    ("auto", CAPS_HEVC, None, ["h264"]),                          # caps unknown: stay on the proven codec
    ("auto", {"nvenc": True, "x264": True}, {"hevc", "h264"}, ["h264"]),    # no HEVC encoder
    ("hevc", CAPS_HEVC, None, ["hevc", "h264"]),                  # the user insists: try even when unknown
    ("hevc", CAPS_HEVC, {"h264"}, ["h264"]),                      # ...but not against the TV's own word
    ("hevc", {"x265": True, "x264": True}, {"hevc"}, ["hevc", "h264"]),
    ("hevc", {"x265": True}, {"hevc"}, ["hevc"]),                 # no H.264 encoder: nothing to fall back to
    ("bogus", CAPS_HEVC, {"hevc"}, ["h264"]),
])
def test_plan_video(pref, caps, tv, want):
    plan = mm.plan_video(pref, caps, tv)
    assert [codec for codec, _enc, _gpu in plan] == want


def test_plan_video_picks_encoders_and_gpu_scaling():
    plan = mm.plan_video("auto", CAPS_HEVC, {"hevc"})
    assert plan == [("hevc", "hevc_nvenc", True), ("h264", "h264_nvenc", True)]
    plan = mm.plan_video("hevc", {"x265": True, "x264": True}, None)
    assert plan == [("hevc", "libx265", False), ("h264", "libx264", False)]


def test_plan_video_without_any_encoder_raises_no_encoder():
    with pytest.raises(models.AirlinxError) as ei:
        mm.plan_video("auto", {}, {"hevc"})
    assert ei.value.code == "no_encoder"


def test_plan_video_can_exclude_a_codec():
    assert [c for c, *_ in mm.plan_video("hevc", CAPS_HEVC, None, exclude={"hevc"})] == ["h264"]


def test_codec_change_is_a_structural_change():
    a = mm.effective_options(models.MirrorOptions(codec="h264"))
    b = mm.effective_options(models.MirrorOptions(codec="hevc"))
    assert mm.is_structural_change(a, b) and not mm.is_structural_change(a, a)


# ---------------------------------------------------------------------------------- fake TV with codecs
class CodecTv(FakeTv):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.codecs = list(TV_CAPS)
        self.answer_caps = True
        self.reject_hevc = False
        self.caps_requests = 0

    @property
    def offers(self):
        return [p for _, _, p in self.received if p.get("type") == "OFFER"]

    @property
    def offered_codecs(self):
        return [p["offer"]["supportedStreams"][0]["codecName"] for p in self.offers]

    async def handle(self, w, m, p):
        if m.namespace == cv.NS_REMOTING and p.get("type") == "GET_CAPABILITIES":
            self.caps_requests += 1
            if self.answer_caps:   # like the real TV: the reply comes on the webrtc namespace
                self.send(w, cv.NS_WEBRTC, {"capabilities": {"mediaCaps": self.codecs, "remoting": 2}}, src="T1")
                await w.drain()
            return
        if m.namespace == cv.NS_WEBRTC and p.get("type") == "OFFER" and self.reject_hevc and \
                p["offer"]["supportedStreams"][0]["codecName"] == "hevc":
            self.send(w, cv.NS_WEBRTC, {"type": "ANSWER", "seqNum": p["seqNum"], "result": "error",
                                        "error": {"code": 1, "description": "unsupported codec hevc"}}, src="T1")
            await w.drain()
            return
        await super().handle(w, m, p)


class NoteVideo(FakeVideo):
    """FakeVideo that keeps on_fatal and reports its encoder."""
    def __init__(self, params, on_frame, on_fatal, **kw):
        super().__init__(params, on_frame, on_fatal, **kw)
        self.on_fatal = on_fatal
        self.encoder_label = params.encoder


@pytest.fixture(autouse=True)
def hevc_env(patched, monkeypatch):  # noqa: F811
    async def fake_caps():
        return dict(CAPS_HEVC)

    monkeypatch.setattr(mm, "probe_encoders", fake_caps)
    monkeypatch.setattr(mm, "VideoCapture", NoteVideo)
    monkeypatch.setattr(mm, "CAPS_TIMEOUT", 0.3)
    mm._hevc_bad.clear()
    yield
    mm._hevc_bad.clear()


def opts(codec, **kw):
    return models.MirrorOptions(codec=codec, **kw)


def run(coro, timeout=20):
    asyncio.run(asyncio.wait_for(coro, timeout))


async def started(tv, codec, notices=None, **kw):
    port = tv.port = await tv.start()
    states, stats = [], []
    s = make_session(port, states, stats, opts(codec, **kw))
    if notices is not None:
        s.on_notice = notices.append
    await s.start()
    return s, states, stats


# ---------------------------------------------------------------------------------- negotiation
def test_default_codec_is_h264_and_never_asks_the_tv_for_capabilities():
    async def main():
        tv = CodecTv()
        s, *_ = await started(tv, "h264")
        await wait_until(lambda: s.state == "live")
        assert tv.offered_codecs == ["h264"] and tv.caps_requests == 0
        assert NoteVideo.instances[-1].params.encoder == "h264_nvenc"
        await s.stop()
        await tv.close()
    run(main())


def test_auto_offers_hevc_when_the_tv_lists_it_and_streams_with_the_hevc_encoder():
    async def main():
        tv = CodecTv()
        s, *_ = await started(tv, "auto")
        await wait_until(lambda: s.state == "live")
        assert tv.caps_requests == 1 and tv.offered_codecs == ["hevc"]
        assert tv.offers[0]["offer"]["supportedStreams"][0]["level"] == "4.1"
        assert NoteVideo.instances[-1].params.encoder == "hevc_nvenc"
        await s.stop()
        await tv.close()
    run(main())


def test_auto_stays_on_h264_when_the_tv_does_not_list_hevc():
    async def main():
        tv = CodecTv()
        tv.codecs = ["aac", "opus", "h264", "vp8"]
        s, *_ = await started(tv, "auto")
        await wait_until(lambda: s.state == "live")
        assert tv.offered_codecs == ["h264"]
        assert NoteVideo.instances[-1].params.encoder == "h264_nvenc"
        await s.stop()
        await tv.close()
    run(main())


def test_auto_stays_on_h264_when_the_capabilities_reply_never_comes():
    async def main():
        tv = CodecTv()
        tv.answer_caps = False
        s, *_ = await started(tv, "auto")
        await wait_until(lambda: s.state == "live")
        assert tv.offered_codecs == ["h264"]
        await s.stop()
        await tv.close()
    run(main())


def test_auto_without_a_hevc_encoder_does_not_even_ask(monkeypatch):
    async def only_h264():
        return {"nvenc": True, "cuda_scale": True, "x264": True}
    monkeypatch.setattr(mm, "probe_encoders", only_h264)

    async def main():
        tv = CodecTv()
        s, *_ = await started(tv, "auto")
        await wait_until(lambda: s.state == "live")
        assert tv.caps_requests == 0 and tv.offered_codecs == ["h264"]
        await s.stop()
        await tv.close()
    run(main())


def test_forced_hevc_falls_back_to_h264_when_the_answer_rejects_it():
    async def main():
        tv = CodecTv()
        tv.reject_hevc = True
        notices = []
        s, *_ = await started(tv, "hevc", notices)
        await wait_until(lambda: s.state == "live")
        assert tv.offered_codecs == ["hevc", "h264"]
        assert NoteVideo.instances[-1].params.encoder == "h264_nvenc"
        assert not any(i.params.encoder == "hevc_nvenc" and i.task for i in NoteVideo.instances)   # never ran
        assert any("H.264" in n for n in notices)
        await s.stop()
        await tv.close()
    run(main())


def test_forced_hevc_is_offered_even_when_the_tv_does_not_answer_the_capabilities_query():
    async def main():
        tv = CodecTv()
        tv.answer_caps = False
        s, *_ = await started(tv, "hevc")
        await wait_until(lambda: s.state == "live")
        assert tv.offered_codecs == ["hevc"]
        await s.stop()
        await tv.close()
    run(main())


def test_hevc_that_the_tv_accepts_but_never_acks_switches_to_h264_and_is_remembered(monkeypatch):
    monkeypatch.setattr(mm, "STALL_TIMEOUT_START", 0.6)
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))

    async def main():
        tv = CodecTv()
        tv.mute_feedback = True            # decodes nothing, acks nothing
        notices = []
        s, states, _ = await started(tv, "auto", notices)
        await wait_until(lambda: any(x == "reconnecting" for x, _ in states), timeout=8)
        tv.mute_feedback = False
        await wait_until(lambda: s.state == "live", timeout=8)
        assert tv.offered_codecs == ["hevc", "h264"]
        assert any("H.264" in n for n in notices)
        await s.stop()
        # a new session to the same TV does not try HEVC again (in "auto")
        s2, *_ = await started_on(tv, "auto")
        await wait_until(lambda: s2.state == "live")
        assert tv.offered_codecs == ["hevc", "h264", "h264"]
        await s2.stop()
        # ...but an explicit "hevc" always tries
        s3, *_ = await started_on(tv, "hevc")
        await wait_until(lambda: s3.state == "live")
        assert tv.offered_codecs[-1] == "hevc"
        await s3.stop()
        await tv.close()
    run(main(), 40)


async def started_on(tv, codec):
    states, stats = [], []
    s = make_session(tv.port, states, stats, opts(codec))
    await s.start()
    return s, states, stats


def test_hevc_encoder_failure_switches_to_h264(monkeypatch):
    monkeypatch.setattr(mm, "RECONNECT_BACKOFF", (0.05, 0.05))

    async def main():
        tv = CodecTv()
        notices = []
        s, states, _ = await started(tv, "hevc", notices)
        await wait_until(lambda: s.state == "live")
        NoteVideo.instances[-1].on_fatal("ffmpeg (wideo) kończy się błędem: boom")
        await wait_until(lambda: tv.offered_codecs == ["hevc", "h264"] and s.state == "live", timeout=8)
        assert NoteVideo.instances[-1].params.encoder == "h264_nvenc"
        assert any("H.264" in n for n in notices)
        await s.stop()
        await tv.close()
    run(main())


def test_h264_encoder_failure_is_still_fatal():
    async def main():
        tv = CodecTv()
        s, states, _ = await started(tv, "h264")
        await wait_until(lambda: s.state == "live")
        NoteVideo.instances[-1].on_fatal("boom")
        await wait_until(lambda: s.state == "error")
        await tv.close()
    run(main())


def test_changing_the_codec_while_live_renegotiates():
    async def main():
        tv = CodecTv()
        s, *_ = await started(tv, "h264")
        await wait_until(lambda: s.state == "live")
        await s.update(dataclasses.replace(s.opts, codec="hevc"))
        await wait_until(lambda: tv.offered_codecs == ["h264", "hevc"])
        assert NoteVideo.instances[-1].params.encoder == "hevc_nvenc"
        await s.stop()
        await tv.close()
    run(main())


def test_stats_name_the_real_encoder():
    async def main():
        tv = CodecTv()
        s, _, stats = await started(tv, "hevc")
        await wait_until(lambda: len(stats) >= 2)
        assert stats[-1].encoder == "hevc_nvenc"
        await s.stop()
        await tv.close()
    run(main())
