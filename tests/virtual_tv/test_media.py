"""File playback through the real MediaServer and players against the virtual TV: Cast (Default Media Receiver,
pychromecast) and DLNA (AVTransport, async_upnp_client)."""
import asyncio
import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from airlinx.core import models
from airlinx.core.cast.media import CastMediaPlayer
from airlinx.core.dlna import DlnaPlayer
from airlinx.core.media_server import MediaServer

import vtvkit
from tools.virtual_tv.dlna_renderer import DlnaRenderer, SAMSUNG_UA
from vtvkit import ffprobe_frames, make_clip, make_device, wait_until

CLIP_S = 3.0


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clip") / "control.mp4", CLIP_S)


@pytest_asyncio.fixture
async def server():
    s = MediaServer()
    await s.start()
    yield s
    await s.stop()


@pytest_asyncio.fixture
async def dlna(tmp_path):
    r = DlnaRenderer(port=0, out_dir=tmp_path / "dlna")
    await r.start()
    yield r
    await r.stop()


def item_for(clip: Path) -> models.MediaItem:
    return models.MediaItem(id="c1", path_or_url=str(clip), title="Klip kontrolny", duration=CLIP_S, kind="video")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Seen:
    def __init__(self):
        self.states: list[models.PlaybackState] = []

    def __call__(self, st: models.PlaybackState) -> None:
        self.states.append(st)

    @property
    def names(self) -> list[str]:
        out: list[str] = []
        for s in self.states:
            if not out or out[-1] != s.state:
                out.append(s.state)
        return out


# ------------------------------------------------------------------------------------------ Cast
@pytest.mark.asyncio
async def test_cast_plays_the_clip_and_reports_buffering_playing_ended(make_tv, server, clip):
    tv = await make_tv()
    seen = Seen()
    player = CastMediaPlayer(make_device(tv), seen)
    item = item_for(clip)
    url, mime = await server.publish_async(item, "cast", target_ip="127.0.0.1")
    try:
        await player.load(item, url, mime)
        await wait_until(lambda: "ended" in seen.names, 15, "the clip to finish")
    finally:
        await player.stop()
        await player.close()

    # the controller-visible state machine
    names = seen.names
    assert names[0] == "loading" and "playing" in names and names[-1] in ("ended", "idle")
    assert names.index("playing") < names.index("ended")
    assert [s for s in tv.dmr_sessions[0].states if s[1] == "BUFFERING"], "TV never reported BUFFERING"
    assert ("IDLE", "FINISHED") in [s[1:] for s in tv.dmr_sessions[0].states]

    # what the TV downloaded: a ranged GET of the real file, byte for byte
    dmr = tv.dmr_sessions[0]
    f = dmr.fetches[0]
    assert f.status in (200, 206) and f.headers["Range"] == "bytes=0-"
    assert f.url == url
    assert f.bytes == clip.stat().st_size
    assert sha(dmr.file_path) == sha(clip)
    info = ffprobe_frames(dmr.file_path)["streams"]
    assert {s["codec_name"] for s in info} == {"h264", "aac"}
    load = dmr.load_requests[0]["media"]
    assert load["contentUrl"] == url and load["contentType"] == mime == "video/mp4"
    assert tv.app is None, "stop() must quit the Default Media Receiver"


@pytest.mark.asyncio
async def test_cast_pause_resume_seek_volume_stop(make_tv, server, tmp_path):
    long_clip = make_clip(tmp_path / "long.mp4", 12.0)
    tv = await make_tv()
    seen = Seen()
    player = CastMediaPlayer(make_device(tv), seen)
    item = models.MediaItem(id="c2", path_or_url=str(long_clip), title="Długi klip", duration=12.0, kind="video")
    url, mime = await server.publish_async(item, "cast", target_ip="127.0.0.1")
    try:
        await player.load(item, url, mime)
        await wait_until(lambda: seen.states[-1].state == "playing", 10, "playing")
        dmr = tv.dmr_sessions[0]
        await player.pause()
        await wait_until(lambda: dmr.state == "PAUSED", 5, "TV paused")
        await wait_until(lambda: seen.states[-1].state == "paused", 5, "paused reported to the app")
        await player.play()
        await wait_until(lambda: dmr.state == "PLAYING", 5, "TV resumed")
        await player.seek(8.0)
        await wait_until(lambda: tv.events_of("seek"), 5, "SEEK on the TV")
        assert tv.events_of("seek")[0].data["to"] == 8.0
        await wait_until(lambda: any(f.range.startswith("bytes=") and f.range != "bytes=0-" for f in dmr.fetches),
                         5, "a ranged re-fetch after the seek")
        await player.set_volume(0.3)
        await wait_until(lambda: abs(tv.volume - 0.3) < 1e-6, 5, "volume on the TV")
    finally:
        await player.stop()
        await player.close()
    assert tv.app is None


@pytest.mark.asyncio
async def test_cast_unreachable_url_is_a_load_failure_not_a_hang(make_tv, server, clip):
    tv = await make_tv()
    seen = Seen()
    player = CastMediaPlayer(make_device(tv), seen)
    item = item_for(clip)
    url, mime = await server.publish_async(item, "cast", target_ip="127.0.0.1")
    server.unpublish(url)                      # token gone: the TV gets a 404
    try:
        with pytest.raises(models.AirlinxError) as ei:
            await player.load(item, url, mime)
        assert ei.value.code == "cast_load_failed"
        assert seen.states[-1].state == "error" and "nie odtworzył" in (seen.states[-1].error or "")
    finally:
        await player.stop()
        await player.close()
    assert tv.dmr_sessions[0].fetches[0].status == 404


# ------------------------------------------------------------------------------------------ DLNA
def dlna_device(r: DlnaRenderer) -> models.Device:
    return models.Device(id="dlna:vtv", name=r.name, model="Virtual MediaRenderer", ip=r.host,
                         dlna_location=r.location, caps={"dlna"})


@pytest.mark.asyncio
async def test_dlna_plays_the_clip_with_samsung_style_requests(server, dlna, clip):
    seen = Seen()
    player = DlnaPlayer(dlna_device(dlna), seen, poll_interval=0.2)
    item = item_for(clip)
    url, mime = await server.publish_async(item, "dlna", target_ip="127.0.0.1")
    try:
        await player.load(item, url, mime)
        await wait_until(lambda: "ended" in seen.names, 20, "the clip to end")
    finally:
        await player.stop()
        await player.close()
    assert seen.names[0] == "loading" and "playing" in seen.names and seen.names[-1] in ("ended", "idle")

    head, get = dlna.fetches[0], dlna.fetches[1]
    assert (head.method, get.method) == ("HEAD", "GET")
    for f in (head, get):
        assert f.headers["User-Agent"] == SAMSUNG_UA
        assert f.headers["getcontentFeatures.dlna.org"] == "1"
        assert f.headers["transferMode.dlna.org"] == "Streaming"
    assert get.headers["Range"] == "bytes=0-"
    assert get.status in (200, 206) and get.bytes == clip.stat().st_size
    assert "DLNA.ORG_" in head.response_headers.get("contentFeatures.dlna.org", ""), "server omitted DLNA features"
    assert sha(dlna.file_path) == sha(clip)
    actions = [a[1] for a in dlna.actions]
    assert actions.index("SetAVTransportURI") < actions.index("Play")
    didl = next(a[2] for a in dlna.actions if a[1] == "SetAVTransportURI")["CurrentURIMetaData"]
    assert "object.item.videoItem" in didl and url in didl and "http-get:*:video/mp4" in didl


@pytest.mark.asyncio
async def test_dlna_pause_seek_stop_and_samsung_volume_quirk(server, dlna, tmp_path):
    long_clip = make_clip(tmp_path / "long.mp4", 12.0)
    seen = Seen()
    player = DlnaPlayer(dlna_device(dlna), seen, poll_interval=0.2)
    item = models.MediaItem(id="d2", path_or_url=str(long_clip), title="Długi klip", duration=12.0, kind="video")
    url, mime = await server.publish_async(item, "dlna", target_ip="127.0.0.1")
    try:
        await player.load(item, url, mime)
        await wait_until(lambda: dlna.state == "PLAYING", 10, "PLAYING")
        await player.pause()
        await wait_until(lambda: seen.states[-1].state == "paused", 5, "paused")
        await player.play()
        await wait_until(lambda: seen.states[-1].state == "playing", 5, "playing again")
        await player.seek(8.0)
        await wait_until(lambda: dlna.current_time() >= 8.0, 5, "the seek")
        await wait_until(lambda: seen.states[-1].position >= 8.0, 5, "position reported back")
        # real S85F: GetVolume is always 0 -> AirLinx must treat it as "unknown", never as a real 0 %
        assert seen.states[-1].volume is None
        await player.set_volume(0.4)             # accepted, ignored, like on the TV
    finally:
        await player.stop()
        await player.close()
    assert dlna.state == "STOPPED"
    assert ("SetVolume", {"InstanceID": "0", "Channel": "Master", "DesiredVolume": "40"}) in [
        (a[1], a[2]) for a in dlna.actions]
    assert dlna.volume == 20, "a Samsung-quirk renderer must ignore SetVolume"


@pytest.mark.asyncio
async def test_dlna_real_volume_when_quirks_are_off(server, tmp_path, clip):
    r = DlnaRenderer(port=0, samsung_quirks=False, out_dir=tmp_path / "d")
    await r.start()
    seen = Seen()
    player = DlnaPlayer(dlna_device(r), seen, poll_interval=0.2)
    item = item_for(clip)
    url, mime = await server.publish_async(item, "dlna", target_ip="127.0.0.1")
    try:
        await player.load(item, url, mime)
        await wait_until(lambda: r.state == "PLAYING", 10, "PLAYING")
        await player.set_volume(0.4)
        await wait_until(lambda: seen.states[-1].volume == pytest.approx(0.4), 5, "the real volume is reported")
        assert r.volume == 40
    finally:
        await player.stop()
        await player.close()
        await r.stop()
