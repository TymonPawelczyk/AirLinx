import asyncio
import threading

import pytest

from airlinx.core.cast import castv2 as cv
from airlinx.core.cast import media as cm
from airlinx.core.models import Device, MediaItem, TvBusyError

DEV = Device("8f2a1b3c4d5e6f708192a3b4c5d6e7f8", "TV", "S85F", "192.0.2.10", cast_port=8009,
             caps={"cast_media"})


class FakeChannel:
    """Stands in for castv2.CastChannel.open(): device-auth + identity pinning happen there."""
    opened: list[tuple] = []
    fail: BaseException | None = None

    def __init__(self):
        self.closed = False

    @classmethod
    async def open(cls, host, port=8009, **kw):
        cls.opened.append((host, port, kw.get("pin_key")))
        if cls.fail is not None:
            raise cls.fail
        ch = cls()
        cls.last = ch
        return ch

    async def close(self, graceful=True):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_channel(monkeypatch):
    FakeChannel.opened, FakeChannel.fail = [], None
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


class FakeStatus:
    def __init__(self, app_id=None, name=""):
        self.app_id, self.display_name = app_id, name
        self.volume_level, self.volume_muted = 0.4, False


class FakeMediaStatus:
    def __init__(self, state, t=0.0, idle_reason=None):
        self.player_state, self.idle_reason = state, idle_reason
        self.adjusted_current_time = t
        self.current_time = t
        self.duration = None


class FakeMC:
    def __init__(self, cast=None):
        self.listeners, self.sent, self.cmds = [], [], []
        self.status = None
        self.cast = cast

    def register_status_listener(self, l):
        self.listeners.append(l)

    def send_message(self, msg, inc_session_id=False, callback_function=None):
        self.sent.append(msg)
        self.cast.status.app_id = cm.DMR_APP_ID  # LAUNCH happened
        # answer from "socket thread"
        threading.Timer(0.01, callback_function, args=({"type": "MEDIA_STATUS"},)).start()

    def __getattr__(self, name):
        if name in ("play", "pause", "stop", "seek", "enable_subtitle", "disable_subtitle"):
            return lambda *a: self.cmds.append((name, a))
        raise AttributeError(name)


class FakeCast:
    def __init__(self, app_id=None, name=""):
        self.status = FakeStatus(app_id, name)
        self.media_controller = FakeMC(self)
        self.calls = []

    def wait(self, timeout=None):
        pass

    def set_volume(self, v):
        self.calls.append(("vol", v))

    def quit_app(self):
        self.calls.append(("quit",))

    def disconnect(self, timeout=None):
        self.calls.append(("disconnect",))


def make(cast, states):
    return cm.CastMediaPlayer(DEV, states.append, cast_factory=lambda d: cast)


@pytest.mark.asyncio
async def test_busy_tv_raises_unless_forced():
    cast = FakeCast("0F5096E8", "Netflix")
    p = make(cast, [])
    item = MediaItem("i", "/x.mp4", "Film", 60.0, "video")
    with pytest.raises(TvBusyError) as ei:
        await p.load(item, "http://h/f/t/m.mp4", "video/mp4")
    assert "Netflix" in ei.value.message_pl
    await p.load(item, "http://h/f/t/m.mp4", "video/mp4", force=True)
    assert cast.media_controller.sent[0]["type"] == "LOAD"
    await p.close()


@pytest.mark.asyncio
async def test_load_status_and_controls():
    cast = FakeCast(cm.BACKDROP_APP_ID)
    states = []
    p = make(cast, states)
    item = MediaItem("i", "/x.mkv", "Film", 60.0, "video")
    await p.load(item, "http://h/s/t/m.mp4", "video/mp4", start=10)
    assert states[0].state == "loading"
    assert cast.media_controller.sent[0]["media"]["contentId"].endswith("?t=10.000")
    # status from pychromecast thread
    listener = cast.media_controller.listeners[0]
    threading.Thread(target=listener.new_media_status, args=(FakeMediaStatus("PLAYING", 2.0),)).start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if states[-1].state == "playing":
            break
    assert states[-1].state == "playing" and states[-1].position == 12.0 and states[-1].volume == 0.4
    await p.pause()
    await p.play()
    await p.set_volume(0.7)
    assert ("vol", 0.7) in cast.calls
    await p.seek(30)  # stream → reload
    assert cast.media_controller.sent[-1]["media"]["contentId"].endswith("?t=30.000")
    await p.stop()
    assert [c[0] for c in cast.media_controller.cmds] == ["pause", "play", "stop"]
    assert ("quit",) in cast.calls
    assert states[-1].state == "idle"
    await p.close()


@pytest.mark.asyncio
async def test_direct_seek_uses_cast_seek():
    cast = FakeCast(None)
    p = make(cast, [])
    await p.load(MediaItem("i", "/x.mp4", "F", 60.0, "video"), "http://h/f/t/m.mp4", "video/mp4")
    await p.seek(25)
    assert cast.media_controller.cmds[-1] == ("seek", (25,))
    await p.close()


@pytest.mark.asyncio
async def test_finished_maps_to_ended():
    cast = FakeCast(None)
    states = []
    p = make(cast, states)
    await p.load(MediaItem("i", "/x.mp4", "F", 60.0, "video"), "http://h/f/t/m.mp4", "video/mp4")
    p._on_media_status(FakeMediaStatus("PLAYING", 1))
    p._on_media_status(FakeMediaStatus("IDLE", 60, "FINISHED"))
    assert states[-1].state == "ended"
    await p.close()


@pytest.mark.asyncio
async def test_connect_verifies_receiver_with_device_auth_first(fake_channel):
    cast = FakeCast(None)
    p = make(cast, [])
    await p.load(MediaItem("i", "/x.mp4", "F", 60.0, "video"), "http://h/f/t/m.mp4", "video/mp4")
    assert fake_channel.opened == [("192.0.2.10", 8009, DEV.id)]   # same pin key as mirroring
    assert fake_channel.last.closed
    await p.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("err", [cv.TvAuthFailedError("bad signature"), cv.TvCertChangedError(DEV.id)])
async def test_failed_identity_check_never_reaches_pychromecast(fake_channel, err):
    fake_channel.fail = err
    created = []
    p = cm.CastMediaPlayer(DEV, lambda s: None, cast_factory=lambda d: created.append(d) or FakeCast(None))
    with pytest.raises(type(err)):     # not wrapped into TvNotFoundError: the controller must see the code
        await p.load(MediaItem("i", "/x.mp4", "F", 60.0, "video"), "http://h/f/t/m.mp4?token=x", "video/mp4")
    assert created == []               # the URL with its token was never sent to the impostor
