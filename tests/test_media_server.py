import asyncio
import re
import shutil
import subprocess

import aiohttp
import pytest
import pytest_asyncio

from airlinx.core import media_server as ms
from airlinx.core import probe
from airlinx.core.models import MediaItem

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest_asyncio.fixture
async def server():
    s = ms.MediaServer(lan_ip="127.0.0.1")
    await s.start(host="127.0.0.1", port=0)
    async with aiohttp.ClientSession() as sess:
        s.sess = sess
        yield s
    await s.stop()


def direct_plan(mime="video/mp4", kind="video"):
    return probe.PlaybackPlan("dlna", "direct", mime, kind, duration=10.0)


@pytest.fixture
def blob(tmp_path):
    p = tmp_path / "f.mp4"
    p.write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    return p


def test_dlna_features_strings():
    f = ms.dlna_content_features("file", "video/mp4")
    assert "DLNA.ORG_OP=01" in f and "DLNA.ORG_FLAGS=01700000" + "0" * 24 in f
    assert "DLNA.ORG_OP=10" in ms.dlna_content_features("stream", "video/mpeg")
    live = ms.dlna_content_features("live", "video/mpeg")
    assert "DLNA.ORG_OP=00" in live and "DLNA.ORG_CI=1" in live
    assert ms.dlna_content_features("image", "image/jpeg").startswith("DLNA.ORG_PN=JPEG_LRG;")


def test_url_helpers():
    u = ms.with_start("http://h:1/s/tok/media.mp4", 12.5)
    assert ms.start_of(u) == 12.5 and ms.is_stream_url(u)
    assert ms.start_of(ms.with_start(u, 0)) == 0
    assert ms.url_kind("http://h/live/x/y.ts") == "live"
    assert ms.parse_npt("npt=0:01:02.500-") == 62.5
    assert ms.parse_npt("npt=30.0-") == 30.0
    assert ms.parse_npt("bytes=0-") is None
    assert ms.fmt_npt(3725.25) == "1:02:05.250"


def test_lan_ip_is_not_loopback():
    assert not ms.get_lan_ip().startswith("127.")


@pytest.mark.asyncio
async def test_range_head_cors_dlna_headers(server, blob):
    item = MediaItem("1", str(blob), "Film", 10.0, "video")
    url, mime = server.publish_plan(item, direct_plan())
    assert url.startswith("http://127.0.0.1:") and "/f/" in url and mime == "video/mp4"
    token = url.split("/")[4]
    assert len(token) >= 20

    async with server.sess.get(url, headers={"Range": "bytes=100-199"}) as r:
        assert r.status == 206
        body = await r.read()
        assert body == (bytes(range(256)) * 40)[100:200]
        assert r.headers["Content-Range"] == "bytes 100-199/10240"
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert r.headers["transferMode.dlna.org"] == "Streaming"
        assert "DLNA.ORG_OP=01" in r.headers["contentFeatures.dlna.org"]
        assert r.headers["Content-Type"] == "video/mp4"
    async with server.sess.head(url) as r:
        assert r.status == 200 and r.headers["Content-Length"] == "10240"
        assert r.headers.get("Accept-Ranges") == "bytes"
    async with server.sess.options(url, headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"}) as r:
        assert r.status == 204 and "Range" in r.headers["Access-Control-Allow-Headers"]
    async with server.sess.get(url.replace(token, "nope")) as r:
        assert r.status == 404


@pytest.mark.asyncio
async def test_poster_and_unpublish(server, blob):
    item = MediaItem("1", str(blob), "Film", 10.0, "video", thumbnail_png=b"\x89PNGfake")
    url, _ = server.publish_plan(item, direct_plan())
    purl = server.poster_url(url)
    async with server.sess.get(purl) as r:
        assert r.status == 200 and await r.read() == b"\x89PNGfake"
        assert r.headers["transferMode.dlna.org"] == "Interactive"
    server.unpublish(url)
    async with server.sess.get(url) as r:
        assert r.status == 404


@pytest.mark.asyncio
async def test_remote_direct_url_passthrough(server):
    item = MediaItem("1", "http://example.invalid/a.mp4", "A", 5, "video")
    url, mime = server.publish_plan(item, probe.PlaybackPlan("cast", "direct", "video/mp4", "video"))
    assert url == "http://example.invalid/a.mp4"


@pytest.mark.asyncio
async def test_live_endpoint_fanout(server):
    async def gen():
        for i in range(5):
            await asyncio.sleep(0.05)
            yield bytes([0x47]) + bytes([i]) * 187

    url = server.register_live("mirror", gen())
    assert "/live/" in url
    async with server.sess.get(url) as r:
        assert r.status == 200
        assert "DLNA.ORG_OP=00" in r.headers["contentFeatures.dlna.org"]
        assert r.headers["Content-Type"] == "video/mpeg"
        data = await r.read()
    assert len(data) == 5 * 188 and data[0] == 0x47


@pytest.mark.asyncio
async def test_live_factory_per_client(server):
    async def gen():
        yield b"x" * 188

    url = server.register_live("m2", gen)
    for _ in range(2):
        async with server.sess.get(url) as r:
            assert await r.read() == b"x" * 188
    server.unregister_live("m2")
    async with server.sess.get(url) as r:
        assert r.status == 404


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not HAVE_FFMPEG:
        pytest.skip("no ffmpeg")
    d = tmp_path_factory.mktemp("clip")
    mkv = d / "clip.mkv"
    srt = d / "clip.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nZażółć gęślą jaźń\n\n2\n00:00:02,000 --> 00:00:02,800\nDwa\n",
                   encoding="utf-8")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-i", str(srt), "-t", "3",
                    "-map", "0", "-map", "1", "-map", "2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt",
                    str(mkv)], check=True)
    return mkv, srt


@pytest.mark.asyncio
async def test_real_probe_and_remux_stream(server, clip):
    mkv, _ = clip
    item = MediaItem("c", str(mkv), "Klip", 3.0, "video")
    url, mime = await server.publish_async(item, "cast")
    assert "/s/" in url and mime == "video/mp4"
    pub = server.info(url)
    assert pub.plan.mode == "remux"
    async with server.sess.get(url) as r:
        assert r.status == 200
        assert "Content-Length" not in r.headers
        assert "DLNA.ORG_OP=10" in r.headers["contentFeatures.dlna.org"]
        data = await r.read()
    assert b"ftyp" in data[:32] and b"moof" in data
    async with server.sess.get(url, headers={"Range": "bytes=1000-"}) as r:
        assert r.status == 416
    # DLNA time seek header honoured
    url2, mime2 = await server.publish_async(item, "dlna")
    assert mime2 == "video/x-mkv"  # mkv h264/aac is direct for Samsung DLNA
    url3, _ = server.publish_plan(item, probe.decide(probe.parse_probe(probe.run_ffprobe(str(mkv))), "cast"))
    async with server.sess.get(url3, headers={"TimeSeekRange.dlna.org": "npt=1.0-"}) as r:
        assert r.headers["TimeSeekRange.dlna.org"].startswith("npt=0:00:01.000-")
        assert len(await r.read()) > 1000


@pytest.mark.asyncio
async def test_subtitles_embedded_and_external(server, clip):
    mkv, srt = clip
    item = MediaItem("c", str(mkv), "Klip", 3.0, "video", subtitles=[str(srt)])
    url, _ = await server.publish_async(item, "cast")
    tracks = server.subtitle_tracks(url)
    assert [t["id"] for t in tracks] == [1, 2]
    async with server.sess.get(tracks[0]["url"]) as r:
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/vtt")
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        text = (await r.read()).decode()
    assert text.startswith("WEBVTT") and "Zażółć gęślą jaźń" in text
    async with server.sess.get(tracks[1]["url"]) as r:
        assert "Dwa" in (await r.read()).decode()
    srt_tracks = server.subtitle_tracks(url, "srt")
    async with server.sess.get(srt_tracks[0]["url"]) as r:
        # cues are stored +23 ms in the clip (muxer aligned them to the first video pts)
        assert re.search(r"00:00:00,5\d\d --> 00:00:01,5\d\d", (await r.read()).decode())


@pytest.mark.asyncio
async def test_dlna_caption_header(server, clip):
    mkv, srt = clip
    item = MediaItem("c", str(mkv), "Klip", 3.0, "video")
    url, _ = await server.publish_async(item, "dlna")
    async with server.sess.head(url) as r:
        assert r.headers["CaptionInfo.sec"].endswith("/0.srt")


@pytest.mark.asyncio
async def test_image_conversion(server, tmp_path):
    if not HAVE_FFMPEG:
        pytest.skip("no ffmpeg")
    bmp = tmp_path / "x.bmp"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=64x48", "-frames:v", "1",
                    str(bmp)], check=True)
    item = MediaItem("i", str(bmp), "Obraz", None, "image")
    url, mime = await server.publish_async(item, "cast")
    assert mime == "image/jpeg" and "/i/" in url
    async with server.sess.get(url) as r:
        body = await r.read()
        assert body[:2] == b"\xff\xd8"
        assert r.headers["transferMode.dlna.org"] == "Interactive"


@pytest.mark.asyncio
async def test_logs_do_not_leak_tokens_or_urls(server, caplog, tmp_path):
    import logging
    caplog.set_level(logging.DEBUG, logger="airlinx")
    secret_url = "http://user:pw@127.0.0.1:1/x.mkv?sig=TOPSECRET"
    item = MediaItem("1", secret_url, "X", 1.0, "video")
    url, _ = await server.publish_async(item, "cast")   # ffprobe fails → guessed plan, warning logged
    token = url.split("/")[4] if "/s/" in url or "/f/" in url else None
    if token:
        async with server.sess.get(url) as r:            # spawns ffmpeg (fails) → debug + warning logs
            await r.read()
    text = caplog.text
    assert "TOPSECRET" not in text and "user:pw" not in text
    if token:
        assert token not in text


# ---------------------------------------------------------------- review fixes

@pytest.mark.asyncio
async def test_lan_ip_resolved_per_publish_towards_target(monkeypatch, blob):
    calls = []

    def fake_lan(peer=None):
        calls.append(peer)
        return {"192.0.2.10": "192.0.2.16", "10.8.0.5": "10.8.0.2"}.get(peer, "172.16.0.9")

    monkeypatch.setattr(ms, "get_lan_ip", fake_lan)
    s = ms.MediaServer()            # no override → per-publish resolution
    await s.start("127.0.0.1", 0)
    try:
        item = MediaItem("1", str(blob), "F", 1.0, "video", thumbnail_png=b"png")
        url1, _ = s.publish_plan(item, direct_plan(), target_ip="192.0.2.10")
        url2, _ = s.publish_plan(item, direct_plan(), target_ip="10.8.0.5")
        url3, _ = s.publish_plan(item, direct_plan())                    # fallback: default route
        assert url1.startswith("http://192.0.2.16:") and url2.startswith("http://10.8.0.2:")
        assert url3.startswith("http://172.16.0.9:")
        assert s.poster_url(url2).startswith("http://10.8.0.2:")         # derived URLs keep item's host
        live = s.register_live("m", lambda: None, target_ip="192.0.2.10")
        assert live.startswith("http://192.0.2.16:")
        assert "192.0.2.10" in calls and "10.8.0.5" in calls and None in calls
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_lan_override_wins(server, blob):
    url, _ = server.publish_plan(MediaItem("1", str(blob), "F", 1.0, "video"), direct_plan(), target_ip="10.0.0.1")
    assert url.startswith("http://127.0.0.1:")


@pytest.mark.asyncio
async def test_publish_async_accepts_target_ip(server, clip):
    mkv, _ = clip
    url, _ = await server.publish_async(MediaItem("c", str(mkv), "K", 3.0, "video"), "cast", target_ip="127.0.0.1")
    assert "/s/" in url


@pytest.fixture
def fake_ffmpeg(tmp_path):
    """Script that floods stderr (>64 KiB pipe buffer) before writing stdout, then fails."""
    script = tmp_path / "ffmpeg"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "for i in range(5000):\n"
        "    sys.stderr.write(f'error line {i} http://u:pw@x/y?sig=SECRET ' + 'x' * 60 + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.stdout.buffer.write(b'DATA' * 1000); sys.stdout.flush()\n"
        "sys.exit(1)\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_stderr_flood_does_not_block_and_tail_logged(fake_ffmpeg, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="airlinx")
    s = ms.MediaServer(lan_ip="127.0.0.1", ffmpeg=fake_ffmpeg)
    await s.start("127.0.0.1", 0)
    try:
        plan = probe.PlaybackPlan("cast", "remux", "video/mp4", "video", 10.0, "mp4", "copy", "copy", ["-f", "mp4"])
        url, _ = s.publish_plan(MediaItem("1", "/nonexistent.mkv", "F", 10.0, "video"), plan)
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                body = await r.read()
        assert body == b"DATA" * 1000
        msgs = [r.getMessage() for r in caplog.records if "ffmpeg stream error" in r.getMessage()]
        assert msgs and "error line 4999" in msgs[0] and "error line 0 " not in msgs[0]
        assert "SECRET" not in msgs[0] and "u:pw" not in msgs[0]
        assert not s._procs
    finally:
        await s.stop()


@pytest.fixture
def slow_ffmpeg(tmp_path):
    script = tmp_path / "ffmpeg"
    script.write_text("#!/usr/bin/env python3\nimport sys, time\n"
                      "while True:\n    sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush(); time.sleep(0.01)\n")
    script.chmod(0o755)
    return str(script)


@pytest.mark.asyncio
async def test_ffmpeg_killed_when_client_disconnects(slow_ffmpeg):
    s = ms.MediaServer(lan_ip="127.0.0.1", ffmpeg=slow_ffmpeg)
    await s.start("127.0.0.1", 0)
    try:
        plan = probe.PlaybackPlan("cast", "remux", "video/mp4", "video", 10.0, "mp4", "copy", "copy", ["-f", "mp4"])
        url, _ = s.publish_plan(MediaItem("1", "/x.mkv", "F", 10.0, "video"), plan)
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as r:
                await r.content.read(65536)
                procs = list(s._procs)
                assert len(procs) == 1
        for _ in range(100):
            await asyncio.sleep(0.05)
            if procs[0].returncode is not None and not s._procs:
                break
        assert procs[0].returncode is not None and not s._procs
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_unpublish_clears_caches_and_kills_streams(slow_ffmpeg, blob):
    s = ms.MediaServer(lan_ip="127.0.0.1", ffmpeg=slow_ffmpeg)
    await s.start("127.0.0.1", 0)
    try:
        plan = probe.PlaybackPlan("cast", "remux", "video/mp4", "video", 10.0, "mp4", "copy", "copy", ["-f", "mp4"])
        url, _ = s.publish_plan(MediaItem("1", "/x.mkv", "F", 10.0, "video"), plan)
        pub = s.info(url)
        pub.image_cache = b"img"
        pub.sub_cache[(0, "vtt", 0)] = b"WEBVTT"
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as r:
                await r.content.read(65536)
                proc = next(iter(pub.procs))
                assert s.unpublish(url) is True
                assert pub.image_cache is None and not pub.sub_cache
                for _ in range(100):
                    if proc.returncode is not None:
                        break
                    await asyncio.sleep(0.05)
                assert proc.returncode is not None
            async with sess.get(url) as r:
                assert r.status == 404
        assert s.unpublish(url) is False
        assert s.info(pub.token) is None
    finally:
        await s.stop()
