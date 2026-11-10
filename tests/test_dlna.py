import asyncio

import defusedxml.ElementTree as ET
import pytest

from airlinx.core import dlna
from airlinx.core.models import AirlinxError, Device, MediaItem, TvBusyError

NS = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/", "dc": "http://purl.org/dc/elements/1.1/",
      "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/", "sec": "http://www.sec.co.kr/"}

DEV = Device("dlna:x", "TV", "S85F", "192.0.2.10", dlna_location="http://192.0.2.10:9197/dmr", caps={"dlna"})


def test_didl_direct_file():
    item = MediaItem("id&1", "/x.mp4", 'Film "A" & <B>', 3725.0, "video")
    xml = dlna.build_didl(item, "http://h:1/f/tok/media.mp4", "video/mp4", poster_url="http://h:1/p/tok/poster.png",
                          subtitle_url="http://h:1/sub/tok/0.srt")
    root = ET.fromstring(xml)
    it = root.find("d:item", NS)
    assert it.get("id") == "id&1"
    assert it.find("dc:title", NS).text == 'Film "A" & <B>'
    assert it.find("upnp:class", NS).text == "object.item.videoItem"
    res = it.find("d:res", NS)
    assert res.text == "http://h:1/f/tok/media.mp4"
    assert res.get("protocolInfo").startswith("http-get:*:video/mp4:DLNA.ORG_OP=01;")
    assert res.get("duration") == "1:02:05.000"
    assert it.find("sec:CaptionInfoEx", NS).text.endswith("0.srt")
    assert it.find("upnp:albumArtURI", NS).text.endswith("poster.png")


def test_didl_stream_and_live():
    item = MediaItem("1", "/x.mkv", "X", 100.0, "video")
    xml = dlna.build_didl(item, "http://h:1/s/tok/media.ts?t=40.000", "video/mpeg")
    res = ET.fromstring(xml).find("d:item/d:res", NS)
    assert "DLNA.ORG_OP=10" in res.get("protocolInfo")
    assert res.get("duration") == "0:01:00.000"  # remaining after offset
    live = dlna.build_didl(MediaItem("m", "", "Ekran", None, "video"), "http://h:1/live/t/mirror.ts", "video/mpeg")
    res = ET.fromstring(live).find("d:item/d:res", NS)
    assert "DLNA.ORG_OP=00" in res.get("protocolInfo") and res.get("duration") is None
    audio = dlna.build_didl(MediaItem("a", "", "S", 10.0, "audio"), "http://h/f/t/a.mp3", "audio/mpeg")
    assert "musicTrack" in audio and "DLNA.ORG_PN=MP3" in audio


def test_map_transport_state():
    m = dlna.map_transport_state
    assert m("PLAYING", "loading", False) == "playing"
    assert m("PAUSED_PLAYBACK", "playing", False) == "paused"
    assert m("TRANSITIONING", "loading", False) == "buffering"
    assert m("STOPPED", "playing", False) == "ended"
    assert m("STOPPED", "playing", True) == "idle"
    assert m("NO_MEDIA_PRESENT", "idle", False) == "idle"


class FakeAction:
    def __init__(self, dmr, name):
        self.dmr, self.name = dmr, name

    async def async_call(self, **kw):
        self.dmr.calls.append((self.name, kw))
        if self.name == "Play":
            self.dmr.transport_state = "PLAYING"
        elif self.name == "Stop":
            self.dmr.transport_state = "STOPPED"
        elif self.name == "Pause":
            self.dmr.transport_state = "PAUSED_PLAYBACK"


class FakeDmr:
    def __init__(self, state="NO_MEDIA_PRESENT", uri=""):
        self.transport_state = state
        self.current_track_uri = uri
        self.calls = []
        self.media_position = 5
        self.media_duration = 100
        self.volume_level = 0.3
        self.is_volume_muted = False

    def _action(self, svc, name):
        return FakeAction(self, name)

    async def async_set_transport_uri(self, url, title, meta):
        self.calls.append(("SetAVTransportURI", {"url": url, "title": title, "meta": meta}))
        self.current_track_uri = url
        self.transport_state = "STOPPED"

    async def async_wait_for_can_play(self, t):
        pass

    async def async_stop(self):
        await FakeAction(self, "Stop").async_call(InstanceID=0)

    async def async_update(self, do_ping=False):
        pass

    async def async_set_volume_level(self, v):
        self.volume_level = v


@pytest.mark.asyncio
async def test_player_load_play_poll_stop():
    states = []
    fake = FakeDmr()
    p = dlna.DlnaPlayer(DEV, states.append, dmr=fake, poll_interval=3600)
    item = MediaItem("i1", "/x.mp4", "Film", 100.0, "video")
    await p.load(item, "http://h:1/f/tok/media.mp4", "video/mp4")
    names = [c[0] for c in fake.calls]
    assert names == ["SetAVTransportURI", "Play"]
    assert "<dc:title>Film</dc:title>" in fake.calls[0][1]["meta"]
    assert states[0].state == "loading"
    st = await p.poll_once()
    assert st.state == "playing" and st.position == 5 and st.duration == 100 and st.volume == 0.3
    await p.pause()
    assert (await p.poll_once()).state == "paused"
    await p.seek(42)
    assert fake.calls[-1] == ("Seek", {"InstanceID": 0, "Unit": "REL_TIME", "Target": "0:00:42"})
    await p.set_volume(0.5)
    assert fake.volume_level == 0.5
    await p.stop()
    assert fake.calls[-1][0] == "Stop" and states[-1].state == "idle"


@pytest.mark.asyncio
async def test_player_stream_seek_reloads_with_offset():
    fake = FakeDmr()
    p = dlna.DlnaPlayer(DEV, dmr=fake, poll_interval=3600)
    item = MediaItem("i1", "/x.mkv", "Film", 100.0, "video")
    await p.load(item, "http://h:1/s/tok/media.ts", "video/mpeg")
    await p.seek(30)
    sets = [c for c in fake.calls if c[0] == "SetAVTransportURI"]
    assert sets[-1][1]["url"].endswith("?t=30.000")
    p._grace_until = 0
    st = await p.poll_once()
    assert st.position == 35  # 30 offset + 5 reported
    await p.close()


@pytest.mark.asyncio
async def test_player_busy_and_force():
    fake = FakeDmr(state="PLAYING", uri="http://other/thing.mp4")
    p = dlna.DlnaPlayer(DEV, dmr=fake, poll_interval=3600)
    item = MediaItem("i1", "/x.mp4", "Film", 100.0, "video")
    with pytest.raises(TvBusyError):
        await p.load(item, "http://h:1/f/tok/media.mp4", "video/mp4")
    await p.load(item, "http://h:1/f/tok/media.mp4", "video/mp4", force=True)
    assert [c[0] for c in fake.calls] == ["Stop", "SetAVTransportURI", "Play"]
    await p.close()


@pytest.mark.asyncio
async def test_ended_detection():
    states = []
    fake = FakeDmr()
    p = dlna.DlnaPlayer(DEV, states.append, dmr=fake, poll_interval=3600)
    await p.load(MediaItem("i", "/x.mp4", "F", 10.0, "video"), "http://h/f/t/m.mp4", "video/mp4")
    await p.poll_once()
    fake.transport_state = "STOPPED"
    p._grace_until = 0
    assert (await p.poll_once()).state == "ended"


@pytest.mark.asyncio
async def test_dlna_mirror_registers_live_and_stops():
    from airlinx.core.media_server import MediaServer

    server = MediaServer(lan_ip="127.0.0.1")
    await server.start("127.0.0.1", 0)
    try:
        async def ts():
            yield b"\x47" * 188

        fake = FakeDmr()
        m = dlna.DlnaMirror(DEV, server, ts(), player=dlna.DlnaPlayer(DEV, dmr=fake, poll_interval=3600))
        url = await m.start()
        assert "/live/" in url and server.lives
        meta = fake.calls[0][1]["meta"]
        assert "DLNA.ORG_OP=00" in meta
        await m.stop()
        assert not server.lives and fake.calls[-1][0] == "Stop"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_poll_reports_unknown_volume_when_tv_reads_zero():
    """Samsung DMR answers GetVolume=0 (fixed volume) even while playing — not trustworthy."""
    states = []
    fake = FakeDmr()
    fake.volume_level = 0.0
    p = dlna.DlnaPlayer(DEV, states.append, dmr=fake, poll_interval=3600)
    await p.load(MediaItem("i1", "/x.mp4", "Film", 100.0, "video"), "http://h:1/f/tok/media.mp4", "video/mp4")
    st = await p.poll_once()
    assert st.volume is None


@pytest.mark.asyncio
async def test_poll_zero_volume_is_kept_when_muted():
    fake = FakeDmr()
    fake.volume_level = 0.0
    fake.is_volume_muted = True
    p = dlna.DlnaPlayer(DEV, lambda s: None, dmr=fake, poll_interval=3600)
    await p.load(MediaItem("i1", "/x.mp4", "Film", 100.0, "video"), "http://h:1/f/tok/media.mp4", "video/mp4")
    assert (await p.poll_once()).volume == 0.0


@pytest.mark.asyncio
async def test_lost_connection_error_has_unknown_volume():
    """5 failed polls → "error"; volume must be unknown (None), not the 1.0 default (slider jumps to 100%)."""
    states = []
    fake = FakeDmr()
    p = dlna.DlnaPlayer(DEV, states.append, dmr=fake, poll_interval=0.001)
    await p.load(MediaItem("i1", "/x.mp4", "Film", 100.0, "video"), "http://h:1/f/tok/media.mp4", "video/mp4")

    async def broken(do_ping=False):
        raise OSError("TV gone")

    fake.async_update = broken
    for _ in range(200):
        await asyncio.sleep(0.005)
        if states[-1].state == "error":
            break
    await p.close()
    assert states[-1].state == "error" and states[-1].volume is None


@pytest.mark.asyncio
async def test_rejected_load_error_has_unknown_volume():
    states = []
    fake = FakeDmr()

    async def refuse(url, title, meta):
        raise OSError("500")

    fake.async_set_transport_uri = refuse
    p = dlna.DlnaPlayer(DEV, states.append, dmr=fake, poll_interval=3600)
    with pytest.raises(AirlinxError):
        await p.load(MediaItem("i1", "/x.mp4", "Film", 100.0, "video"), "http://h:1/f/tok/media.mp4", "video/mp4")
    assert states[-1].state == "error" and states[-1].volume is None
