"""Real MirrorSession against the virtual Cast TV: OFFER/ANSWER, live time, frame rate, audio, decoding, STOP,
live bitrate change, recovery from packet loss and keyframe requests."""
import asyncio
import statistics
import time

import pytest

from airlinx.core import models
from airlinx.core.cast.mirror import MirrorSession

import vtvkit
from vtvkit import decode_errors, ffprobe_frames, make_device, synthetic_source, wait_until

LIVE_DEADLINE_S = 10
SAMPLE_S = 5


class Run:
    """A started mirror session plus what it reported."""

    def __init__(self, tv, opts=None):
        self.tv = tv
        self.states: list[tuple[float, str, str]] = []
        self.stats: list = []
        self.t0 = time.monotonic()
        self.live = asyncio.Event()
        self.session = MirrorSession(make_device(tv), synthetic_source(),
                                     opts or models.MirrorOptions(fps=60, bitrate=8_000_000, audio=True),
                                     self.stats.append, self._on_state)

    def _on_state(self, state, detail):
        self.states.append((time.monotonic() - self.t0, state, detail))
        if state == "live":
            self.t_live = time.monotonic() - self.t0
            self.live.set()

    async def start(self):
        await self.session.start()
        await asyncio.wait_for(self.live.wait(), LIVE_DEADLINE_S)
        return self


@pytest.fixture
def pcs_before():
    return vtvkit.ffmpeg_pids()


@pytest.mark.asyncio
async def test_mirror_goes_live_streams_decodable_video_and_audio_then_stops(make_tv, synthetic, pcs_before):
    tv = await make_tv()
    run = await Run(tv).start()
    assert run.t_live <= LIVE_DEADLINE_S
    plane = tv.plane
    f0 = len(plane.records("video"))
    a0 = len(plane.records("audio"))
    await asyncio.sleep(SAMPLE_S)
    fps = (len(plane.records("video")) - f0) / SAMPLE_S
    audio_rate = (len(plane.records("audio")) - a0) / SAMPLE_S
    await run.session.stop()

    assert fps >= 50, f"received only {fps:.1f} video frames/s"
    assert audio_rate >= 90, f"received only {audio_rate:.1f} audio frames/s (Opus 10 ms = 100/s)"
    st = plane.stats()
    assert st["video"]["dropped_before_sr"] == 0 and st["audio"]["dropped_before_sr"] == 0, \
        "sender must send a Sender Report before the first RTP packet"
    assert st["video"]["lost_given_up"] == 0 and st["malformed"] == 0 and st["unknown_ssrc"] == 0

    # what the TV would decode: the saved elementary streams go through ffprobe/ffmpeg untouched
    vp = tv.recorder.video_path
    info = ffprobe_frames(vp, "h264")["streams"][0]
    assert (info["codec_name"], info["width"], info["height"]) == ("h264", 1280, 720)
    assert int(info["nb_read_frames"]) == st["video"]["frames"]
    assert decode_errors(vp, "h264") == "", "decoder complained about the received H.264"
    ainfo = ffprobe_frames(tv.recorder.audio_path)["streams"][0]
    assert ainfo["codec_name"] == "opus" and int(ainfo["sample_rate"]) == 48000
    assert int(ainfo["nb_read_packets"]) == st["audio"]["frames"]
    assert decode_errors(tv.recorder.audio_path) == ""

    # STOP: the TV is idle again, the sender cleaned up
    assert tv.app is None and tv.events_of("stop")
    assert run.session.state == "stopped" and not [s for s in run.states if s[1] == "error"]
    assert vtvkit.ffmpeg_pids() - pcs_before == set(), "leftover ffmpeg"


@pytest.mark.asyncio
async def test_offer_carries_what_the_sender_promises(make_tv, synthetic):
    tv = await make_tv()
    run = await Run(tv, models.MirrorOptions(fps=60, bitrate=6_000_000, audio=True, target_delay_ms=100)).start()
    await run.session.stop()
    offer = tv.offers[0]["offer"]
    v, a = offer["supportedStreams"]
    assert (v["type"], v["codecName"], v["rtpPayloadType"], v["maxBitRate"], v["targetDelay"]) == (
        "video_source", "h264", 96, 6_000_000, 100)
    assert (a["type"], a["codecName"], a["rtpPayloadType"], a["sampleRate"], a["channels"]) == (
        "audio_source", "opus", 127, 48000, 2)
    assert len(bytes.fromhex(v["aesKey"])) == 16 and v["aesKey"] != a["aesKey"] and v["ssrc"] != a["ssrc"]
    assert v["resolutions"] == [{"width": 1280, "height": 720}]
    ans = tv.answers[0]
    assert ans["result"] == "ok" and ans["answer"]["sendIndexes"] == [0, 1]
    assert ans["answer"]["ssrcs"] == [v["ssrc"] + 1, a["ssrc"] + 1]


@pytest.mark.asyncio
async def test_bitrate_change_is_applied_live_and_the_stream_continues(make_tv, synthetic):
    tv = await make_tv()
    run = await Run(tv, models.MirrorOptions(fps=60, bitrate=12_000_000, audio=False)).start()
    plane = tv.plane
    await asyncio.sleep(3)
    t_change = time.time()
    n_before = len(plane.records("video"))
    await run.session.update(models.MirrorOptions(fps=60, bitrate=2_000_000, audio=False))
    # same OFFER session: no renegotiation, the encoder restarts and the TV gets a fresh keyframe
    assert len(tv.offers) == 1
    await wait_until(lambda: len(plane.records("video")) > n_before + 120, 8, "frames after the bitrate change")
    await asyncio.sleep(2.5)

    def rate(lo, hi):
        recs = [r for r in plane.records("video") if lo <= r.t_done < hi]
        return sum(r.size for r in recs) * 8 / (hi - lo)
    before = rate(t_change - 2.5, t_change - 0.5)
    after = rate(time.time() - 2.0, time.time())
    await run.session.stop()
    assert after < before * 0.6, f"bitrate did not drop: {before / 1e6:.1f} -> {after / 1e6:.1f} Mb/s"
    assert plane.stats()["video"]["keyframes"] >= 2
    assert decode_errors(tv.recorder.video_path, "h264") == "", "the switch to the new encoder broke decoding"


@pytest.mark.asyncio
async def test_lost_packets_are_nacked_retransmitted_and_everything_still_decodes(make_tv, synthetic):
    tv = await make_tv()
    lost = set()

    def drop(pkt, n):                         # lose ~2% of the video packets once each
        if pkt.pt == 96 and n % 50 == 0 and (pkt.fid8, pkt.pid) not in lost:
            lost.add((pkt.fid8, pkt.pid))
            return True
        return False
    tv.drop = drop
    run = await Run(tv, models.MirrorOptions(fps=60, bitrate=8_000_000, audio=False)).start()
    plane = tv.plane
    await asyncio.sleep(5)
    sent_retx = run.session._media.video.st["retx"]   # noqa: SLF001 (the sender's own counter)
    await run.session.stop()
    st = plane.stats()
    assert st["dropped_by_fault"] > 10
    assert sent_retx >= st["dropped_by_fault"] * 0.8, f"sender retransmitted {sent_retx} for {st['dropped_by_fault']} losses"
    assert st["video"]["lost_given_up"] == 0, f"{st['video']['lost_given_up']} frames never recovered"
    assert decode_errors(tv.recorder.video_path, "h264") == ""
    sess = run.session
    assert sess.state == "stopped"


@pytest.mark.asyncio
async def test_receiver_pli_gets_a_new_keyframe(make_tv, synthetic):
    tv = await make_tv()
    run = await Run(tv, models.MirrorOptions(fps=60, bitrate=8_000_000, audio=False, preset="game")).start()
    await asyncio.sleep(2.5)                  # the GOP is 2 s: the next natural keyframe is far away
    keys = tv.plane.stats()["video"]["keyframes"]
    tv.plane.send_pli()
    await wait_until(lambda: tv.plane.stats()["video"]["keyframes"] > keys, 6, "a keyframe after the PLI")
    await run.session.stop()
    assert decode_errors(tv.recorder.video_path, "h264") == ""


@pytest.mark.asyncio
async def test_app_closed_on_the_tv_ends_the_session_without_relaunching(make_tv, synthetic):
    tv = await make_tv()
    run = await Run(tv, models.MirrorOptions(fps=30, bitrate=4_000_000, audio=False)).start()
    await tv.kill_app()                       # "Home" on the real remote
    await wait_until(lambda: run.session.state == "stopped", 8, "the session to notice")
    assert len(tv.events_of("launched")) == 1, "the sender must never relaunch an app the user closed"
    await run.session.stop()
