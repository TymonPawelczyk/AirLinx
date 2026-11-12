import asyncio
import itertools
from types import SimpleNamespace

import pytest

from airlinx.core.cast import castv2 as cv
from airlinx.core.cast import media as cm
from airlinx.core.models import AirlinxError, Device, MediaItem, TvBusyError, TvNotFoundError

DEV = Device("8f2a1b3c4d5e6f708192a3b4c5d6e7f8", "TV", "S85F", "192.0.2.10", cast_port=8009,
             caps={"cast_media"})


class FakeChannel:
    """Stands in for the verified castv2.CastChannel (device-auth + identity pinning happen in open()). It is the
    ONLY connection CastMediaPlayer may use: everything the player sends ends up in ``sent``."""
    opened: list[tuple] = []
    instances: list["FakeChannel"] = []
    fail: BaseException | None = None
    apps: list[dict] = []

    def __init__(self):
        self.closed = asyncio.Event()
        self.close_reason = None
        self.sent: list[tuple] = []
        self.launched: list[str] = []
        self._listeners = []
        self._ids = itertools.count(1)
        self.apps = [dict(a) for a in FakeChannel.apps]

    @classmethod
    async def open(cls, host, port=8009, **kw):
        cls.opened.append((host, port, kw.get("pin_key"), kw.get("heartbeat", True)))
        if cls.fail is not None:
            raise cls.fail
        ch = cls()
        cls.instances.append(ch)
        return ch

    @property
    def alive(self):
        return not self.closed.is_set()

    def next_id(self):
        return next(self._ids)

    async def get_status(self, timeout=8.0):
        return {"applications": self.apps, "volume": {"level": 0.4, "muted": False}}

    async def launch(self, app_id, *, timeout=30.0, on_progress=None):
        self.launched.append(app_id)
        app = {"appId": app_id, "displayName": "Default Media Receiver", "transportId": "tr-1", "sessionId": "sess-1"}
        self.apps = [app]
        return app

    async def connect_virtual(self, dest):
        self.sent.append((cv.NS_CONNECTION, {"type": "CONNECT"}, dest))

    async def send(self, namespace, payload, dest=cv.RECEIVER_ID):
        if self.closed.is_set():
            raise cv.ChannelClosed("closed")
        self.sent.append((namespace, payload, dest))

    async def request(self, namespace, payload, dest=cv.RECEIVER_ID, *, id_field="requestId", timeout=10.0):
        await self.send(namespace, dict(payload, **{id_field: self.next_id()}), dest)
        return {"type": "MEDIA_STATUS", "status": []}

    async def stop_app(self, session_id, *, wait=True, timeout=5.0):
        self.sent.append((cv.NS_RECEIVER, {"type": "STOP", "sessionId": session_id}, cv.RECEIVER_ID))

    def add_listener(self, cb):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def emit(self, namespace, payload, src="tr-1"):
        m = SimpleNamespace(namespace=namespace, source_id=src)
        for cb in list(self._listeners):
            cb(m, payload)

    async def close(self, graceful=True):
        self.closed.set()

    def of(self, typ):
        return [(ns, p, d) for ns, p, d in self.sent if p.get("type") == typ]


@pytest.fixture(autouse=True)
def fake_channel(monkeypatch):
    FakeChannel.opened, FakeChannel.fail, FakeChannel.instances, FakeChannel.apps = [], None, [], []
    monkeypatch.setattr(cv.CastChannel, "open", FakeChannel.open)
    return FakeChannel


def test_build_load_message_with_tracks():
    item = MediaItem("i", "/x.mkv", "Film", 120.0, "video")
    subs = [{"id": 1, "url": "http://h/sub/t/0.vtt", "label": "Polski", "language": "pol"},
            {"id": 2, "url": "http://h/sub/t/1.vtt", "label": "English", "language": "eng"}]
    msg = cm.build_load_message(item, "http://h/s/t/media.mp4?t=20.000", "video/mp4", 20,
                                poster_url="http://h/p/t/poster.png", subtitles=subs, active_subtitle=1)
    m = msg["media"]
    assert msg["type"] == "LOAD" and msg["autoplay"] is True
    assert m["contentId"].endswith("t=20.000") and m["contentType"] == "video/mp4"
    assert m["metadata"]["title"] == "Film" and m["metadata"]["images"][0]["url"].endswith("poster.png")
    assert m["duration"] == 100.0
    assert [t["trackId"] for t in m["tracks"]] == [1, 2] and m["tracks"][0]["type"] == "TEXT"
    assert msg["activeTrackIds"] == [1]
    assert "currentTime" not in msg  # offset baked into the stream URL
    direct = cm.build_load_message(item, "http://h/f/t/media.mp4", "video/mp4", 15)
    assert direct["currentTime"] == 15 and "tracks" not in direct["media"]
    img = cm.build_load_message(MediaItem("p", "/a.jpg", "Foto", None, "image"), "http://h/f/t/a.jpg", "image/jpeg")
    assert img["media"]["metadata"]["metadataType"] == 4


def test_map_media_status():
    assert cm.map_media_status("PLAYING", None) == "playing"
    assert cm.map_media_status("BUFFERING", None) == "buffering"
    assert cm.map_media_status("IDLE", "FINISHED") == "ended"
    assert cm.map_media_status("IDLE", "ERROR") == "error"
    assert cm.map_media_status("IDLE", "CANCELLED") == "idle"
    assert cm.map_media_status("IDLE", "INTERRUPTED") == "idle"   # must not auto-advance
    assert cm.map_media_status("IDLE", None) == "idle"


def media_status(state, t=0.0, idle_reason=None, session=7, duration=None):
    st = {"mediaSessionId": session, "playerState": state, "currentTime": t, "playbackRate": 1}
    if idle_reason:
        st["idleReason"] = idle_reason
    if duration:
        st["media"] = {"duration": duration}
    return {"type": "MEDIA_STATUS", "status": [st]}


def make(states):
    return cm.CastMediaPlayer(DEV, states.append)


ITEM = MediaItem("i", "/x.mp4", "Film", 60.0, "video")


async def settle(pred, n=100):
    for _ in range(n):
        if pred():
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_busy_tv_raises_unless_forced(fake_channel):
    fake_channel.apps = [{"appId": "0F5096E8", "displayName": "Netflix", "transportId": "x", "sessionId": "s"}]
    p = make([])
    with pytest.raises(TvBusyError) as ei:
        await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    assert "Netflix" in ei.value.message_pl
    assert fake_channel.instances[0].of("LOAD") == []
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4", force=True)
    ch = fake_channel.instances[0]
    assert ch.launched == [cm.DMR_APP_ID] and ch.of("LOAD")[0][2] == "tr-1"
    await p.close()


@pytest.mark.asyncio
async def test_running_dmr_is_reused_not_relaunched(fake_channel):
    fake_channel.apps = [{"appId": cm.DMR_APP_ID, "displayName": "DMR", "transportId": "tr-9", "sessionId": "s-9"}]
    p = make([])
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    ch = fake_channel.instances[0]
    assert ch.launched == [] and ch.of("LOAD")[0][2] == "tr-9"
    assert any(d == "tr-9" and pl["type"] == "CONNECT" for _, pl, d in ch.sent)
    await p.close()


@pytest.mark.asyncio
async def test_load_status_and_controls(fake_channel):
    states = []
    p = make(states)
    item = MediaItem("i", "/x.mkv", "Film", 60.0, "video")
    await p.load(item, "http://h/s/t/m.mp4", "video/mp4", start=10)
    ch = fake_channel.instances[0]
    assert states[0].state == "loading"
    ns, load, dest = ch.of("LOAD")[0]
    assert ns == cv.NS_MEDIA and dest == "tr-1" and load["media"]["contentId"].endswith("?t=10.000")
    assert p.running_app() == (cm.DMR_APP_ID, "Default Media Receiver")
    ch.emit(cv.NS_MEDIA, media_status("PLAYING", 2.0))
    assert states[-1].state == "playing" and 12.0 <= states[-1].position < 12.5 and states[-1].volume == 0.4
    await p.pause()
    await p.play()
    await p.set_volume(0.7)
    await p.set_muted(True)
    assert ch.of("PAUSE")[0][1]["mediaSessionId"] == 7 and ch.of("PAUSE")[0][2] == "tr-1"
    assert ch.of("PLAY")[0][1]["mediaSessionId"] == 7
    assert ch.of("SET_VOLUME")[0][1]["volume"] == {"level": 0.7} and ch.of("SET_VOLUME")[0][2] == cv.RECEIVER_ID
    assert ch.of("SET_VOLUME")[1][1]["volume"] == {"muted": True}
    await p.seek(30)  # stream -> reload
    assert ch.of("LOAD")[-1][1]["media"]["contentId"].endswith("?t=30.000")
    await p.stop()
    assert ch.of("STOP")[0][1]["mediaSessionId"] == 7 and ch.of("STOP")[0][2] == "tr-1"
    assert ch.of("STOP")[1][1]["sessionId"] == "sess-1"   # DMR app quit on the receiver
    assert states[-1].state == "idle"
    await p.close()
    assert ch.closed.is_set()


@pytest.mark.asyncio
async def test_direct_seek_and_subtitles_use_media_namespace(fake_channel):
    p = make([])
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    ch = fake_channel.instances[0]
    ch.emit(cv.NS_MEDIA, media_status("PLAYING", 1.0))
    await p.seek(25)
    assert ch.of("SEEK")[0][1]["currentTime"] == 25 and ch.of("SEEK")[0][1]["mediaSessionId"] == 7
    await p.set_subtitle(2)
    await p.set_subtitle(None)
    assert [x[1]["activeTrackIds"] for x in ch.of("EDIT_TRACKS_INFO")] == [[2], []]
    await p.close()


@pytest.mark.asyncio
async def test_finished_maps_to_ended(fake_channel):
    states = []
    p = make(states)
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    ch = fake_channel.instances[0]
    ch.emit(cv.NS_MEDIA, media_status("PLAYING", 1))
    ch.emit(cv.NS_MEDIA, media_status("IDLE", 60, "FINISHED"))
    assert states[-1].state == "ended" and states[-1].position == 60.0
    await p.close()


@pytest.mark.asyncio
async def test_load_failed_response_raises_and_emits_error(fake_channel):
    states = []
    p = make(states)

    async def failing(self, namespace, payload, dest=cv.RECEIVER_ID, **kw):
        await self.send(namespace, dict(payload, requestId=self.next_id()), dest)
        return {"type": "LOAD_FAILED"}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(FakeChannel, "request", failing)
    try:
        with pytest.raises(AirlinxError) as ei:
            await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    finally:
        monkey.undo()
    assert ei.value.code == "cast_load_failed" and states[-1].state == "error"
    await p.close()


@pytest.mark.asyncio
async def test_everything_goes_through_the_one_verified_channel(fake_channel):
    """TOCTOU: no second, unverified connection may be opened after device-auth (pychromecast used to do that)."""
    p = make([])
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    await p.pause()
    await p.stop()
    assert fake_channel.opened == [("192.0.2.10", 8009, DEV.id, True)]   # same pin key as mirroring
    assert len(fake_channel.instances) == 1
    await p.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("err", [cv.TvAuthFailedError("bad signature"), cv.TvCertChangedError(DEV.id)])
async def test_failed_identity_check_sends_nothing(fake_channel, err):
    fake_channel.fail = err
    p = make([])
    with pytest.raises(type(err)):     # not wrapped into TvNotFoundError: the controller must see the code
        await p.load(ITEM, "http://h/f/t/m.mp4?token=x", "video/mp4")
    assert fake_channel.instances == []   # no channel -> the URL with its token never left the machine


@pytest.mark.asyncio
async def test_unreachable_tv_is_tv_not_found(fake_channel):
    fake_channel.fail = OSError("no route")
    with pytest.raises(TvNotFoundError):
        await make([]).connect()


@pytest.mark.asyncio
async def test_lost_channel_is_reopened_and_verified_again(fake_channel):
    fake_channel.apps = [{"appId": cm.DMR_APP_ID, "displayName": "DMR", "transportId": "tr-1", "sessionId": "sess-1"}]
    p = make([])
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    first = fake_channel.instances[0]
    first.emit(cv.NS_MEDIA, media_status("PLAYING", 1.0))
    first.closed.set()                           # the TV dropped the connection
    await p.pause()                              # next command re-opens (device-auth again) and re-attaches
    assert len(fake_channel.instances) == 2
    second = fake_channel.instances[1]
    assert len(second.of("PAUSE")) == 1 and second.of("PAUSE")[0][1]["mediaSessionId"] == 7
    assert any(d == "tr-1" and pl["type"] == "CONNECT" for _, pl, d in second.sent)
    await p.close()


@pytest.mark.asyncio
async def test_app_closed_on_tv_ends_playback(fake_channel):
    states = []
    p = make(states)
    await p.load(ITEM, "http://h/f/t/m.mp4", "video/mp4")
    ch = fake_channel.instances[0]
    ch.emit(cv.NS_MEDIA, media_status("PLAYING", 1))
    ch.emit(cv.NS_RECEIVER, {"type": "RECEIVER_STATUS", "status": {"applications": [], "volume": {"level": 0.4}}},
            src=cv.RECEIVER_ID)
    assert states[-1].state == "idle"
    await p.close()
