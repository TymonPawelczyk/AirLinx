"""The receiver is codec-independent: H.264 and HEVC in an OFFER, selection by what the sender offered, the
recorded elementary stream decoded with the matching ffmpeg demuxer, and --codecs to restrict negotiation.
These tests drive the receiver with a raw sender (AirLinx's RTP packetizer, a real ffmpeg stream), not with
MirrorSession, which only offers H.264 today."""
import pytest

import vtvkit
from airlinx.core.cast.mirror import parse_answer
from airlinx.core.cast import castv2 as cv
from vtvkit import decode_errors, encoder_available, ffprobe_frames, offer_for, offer_only, send_stream


@pytest.mark.asyncio
@pytest.mark.parametrize("codec,suffix", [("h264", ".h264"), ("hevc", ".h265")])
async def test_receiver_reassembles_decrypts_and_records_a_decodable_stream(make_tv, codec, suffix):
    if not encoder_available(codec):
        pytest.skip(f"ffmpeg has no encoder for {codec}")
    tv = await make_tv()
    r = await send_stream(tv, codec)
    st = r["plane"].stats()
    assert st["video"]["frames"] == r["sent"] and st["video"]["keyframes"] == r["keyframes"]
    assert st["video"]["dropped_before_sr"] == 0 and st["malformed"] == 0
    path = tv.recorder.video_path
    assert path.suffix == suffix and tv.recorder.video_codec == codec
    info = ffprobe_frames(path, codec)["streams"][0]
    assert info["codec_name"] == codec and (info["width"], info["height"]) == (640, 360)
    assert int(info["nb_read_frames"]) == r["sent"]
    assert decode_errors(path, codec) == ""
    assert tv.events_of("answer")[0].data["codecs"] == [codec]


@pytest.mark.asyncio
async def test_codecs_flag_restricts_what_is_accepted(make_tv):
    tv = await make_tv(codecs=("h264",))
    offer, _ = offer_for(["hevc"])
    ch, app, ans = await offer_only(tv, offer)
    try:
        assert ans["result"] == "error" and "h264" in ans["error"]["description"]
        with pytest.raises(cv.CastError):
            parse_answer(ans)
        assert tv.plane is None
    finally:
        await ch.stop_app(app["sessionId"])
        await ch.close()


@pytest.mark.asyncio
async def test_answer_picks_what_the_sender_offered_when_both_codecs_are_listed(make_tv):
    tv = await make_tv()
    offer, _ = offer_for(["hevc", "h264"])
    ch, app, ans = await offer_only(tv, offer)
    try:
        assert ans["result"] == "ok"
        assert ans["answer"]["sendIndexes"] == [0]          # the first acceptable video stream only
        assert tv.recorder.video_codec == "hevc"
    finally:
        await ch.stop_app(app["sessionId"])
        await ch.close()
    tv2 = await make_tv(codecs=("h264",))
    offer, _ = offer_for(["hevc", "h264"])
    ch, app, ans = await offer_only(tv2, offer, pin_key="raw-2")
    try:
        assert ans["answer"]["sendIndexes"] == [1] and tv2.recorder.video_codec == "h264"
    finally:
        await ch.stop_app(app["sessionId"])
        await ch.close()


@pytest.mark.skip(reason="the AirLinx sender only offers H.264 today; enable when MirrorSession can offer hevc")
def test_mirror_session_offers_and_streams_hevc():
    ...
