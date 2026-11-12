"""media_server is a LAN-facing HTTP server: it must serve only what was published, under unguessable tokens,
and answer hostile or sloppy requests with 4xx, never 500 or a file from somewhere else."""
import pytest
import pytest_asyncio
import aiohttp

from airlinx.core import media_server as ms
from airlinx.core import probe
from airlinx.core.models import MediaItem

pytestmark = pytest.mark.asyncio

DATA = bytes(range(256)) * 40  # 10240 bytes


@pytest_asyncio.fixture
async def server():
    s = ms.MediaServer(lan_ip="127.0.0.1")
    await s.start(host="127.0.0.1", port=0)
    async with aiohttp.ClientSession(auto_decompress=False) as sess:
        s.sess = sess
        yield s
    await s.stop()


@pytest.fixture
def blob(tmp_path):
    p = tmp_path / "f.mp4"
    p.write_bytes(DATA)
    return p


def plan(kind="video", mime="video/mp4"):
    return probe.PlaybackPlan("dlna", "direct", mime, kind, duration=10.0)


def publish(server, path, **kw):
    item = MediaItem("1", str(path), "Film", 10.0, "video", **kw)
    return server.publish_plan(item, plan())[0]


def base(server):
    return f"http://127.0.0.1:{server.port}"


# ----------------------------------------------------------------------------- tokens

async def test_tokens_are_long_random_and_unique_per_publish(server, blob):
    urls = {publish(server, blob) for _ in range(50)}
    assert len(urls) == 50
    tokens = {u.split("/")[4] for u in urls}
    assert len(tokens) == 50
    assert all(len(t) >= 20 for t in tokens)


async def test_unpublished_paths_and_guessed_tokens_are_404(server, blob):
    publish(server, blob)
    for path in ("/", "/f/", "/f/x/y", "/f/AAAAAAAAAAAAAAAAAAAAAA/f.mp4", "/s/x/y", "/i/x/y", "/p/x/p.png",
                 "/sub/x/0.vtt", "/live/x/y.ts", "/etc/passwd", "/..%2f..%2fetc/passwd"):
        async with server.sess.get(base(server) + path) as r:
            assert r.status in (404, 405), f"{path} -> {r.status}"


async def test_token_of_one_item_does_not_unlock_another(server, blob, tmp_path):
    other = tmp_path / "secret.mp4"
    other.write_bytes(b"SECRET" * 100)
    url_a = publish(server, blob)
    url_b = publish(server, other)
    async with server.sess.get(url_a) as r:
        assert (await r.read()) == DATA
    async with server.sess.get(url_b) as r:
        assert (await r.read()) == b"SECRET" * 100
    # tampering with the token's last character never lands on another item
    token = url_a.split("/")[4]
    flipped = url_a.replace(token, token[:-1] + ("A" if token[-1] != "A" else "B"))
    async with server.sess.get(flipped) as r:
        assert r.status == 404


async def test_name_segment_is_cosmetic_and_cannot_traverse(server, blob):
    url = publish(server, blob)
    token = url.split("/")[4]
    for name in ("f.mp4", "other-name.mkv", "..", "%2e%2e", "..%2f..%2fetc%2fpasswd", "a%00b"):
        async with server.sess.get(f"{base(server)}/f/{token}/{name}") as r:
            assert r.status in (200, 404, 400), f"{name} -> {r.status}"
            if r.status == 200:
                assert (await r.read()) == DATA, "always the published file, never what the name points to"


async def test_only_get_head_and_options_are_allowed(server, blob):
    url = publish(server, blob)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        async with server.sess.request(method, url, data=b"x") as r:
            assert r.status == 405, f"{method} -> {r.status}"


async def test_unpublish_revokes_access_immediately(server, blob):
    url = publish(server, blob)
    async with server.sess.get(url) as r:
        assert r.status == 200
    assert server.unpublish(url) is True
    async with server.sess.get(url) as r:
        assert r.status == 404
    assert server.unpublish(url) is False


async def test_file_deleted_after_publish_is_404_not_500(server, blob):
    url = publish(server, blob)
    blob.unlink()
    async with server.sess.get(url) as r:
        assert r.status == 404


async def test_file_url_source_serves_the_local_path(server, blob):
    item = MediaItem("1", blob.as_uri(), "Film", 10.0, "video")
    url, _ = server.publish_plan(item, plan())
    async with server.sess.get(url) as r:
        assert r.status == 200 and (await r.read()) == DATA


# ----------------------------------------------------------------------------- Range

@pytest.mark.parametrize("hdr,status,expected", [
    ("bytes=0-0", 206, DATA[0:1]),
    ("bytes=10-19", 206, DATA[10:20]),
    ("bytes=10239-", 206, DATA[10239:]),
    ("bytes=-10", 206, DATA[-10:]),                      # suffix range
    ("bytes=10000-99999", 206, DATA[10000:]),            # end past EOF is clamped
    ("bytes=10240-", 416, None),                         # start at EOF
    ("bytes=99999-", 416, None),
])
async def test_file_range_semantics(server, blob, hdr, status, expected):
    url = publish(server, blob)
    async with server.sess.get(url, headers={"Range": hdr}) as r:
        assert r.status == status, (hdr, r.status)
        if expected is not None:
            assert (await r.read()) == expected
        else:
            assert r.headers.get("Content-Range") == "bytes */10240"


async def test_head_with_range_has_no_body(server, blob):
    url = publish(server, blob)
    async with server.sess.head(url, headers={"Range": "bytes=0-99"}) as r:
        assert r.status == 206 and r.headers["Content-Range"] == "bytes 0-99/10240"


async def test_whole_file_streams_without_range(server, blob):
    url = publish(server, blob)
    async with server.sess.get(url) as r:
        assert r.status == 200 and r.headers["Accept-Ranges"] == "bytes"
        assert (await r.read()) == DATA


@pytest.mark.parametrize("hdr", ["bytes=abc", "bytes=5-2", "bytes=", "items=0-5", "bytes=0-5,10-20", "garbage"])
async def test_malformed_range_never_causes_a_server_error_on_files(server, blob, hdr):
    url = publish(server, blob)
    async with server.sess.get(url, headers={"Range": hdr}) as r:
        assert r.status < 500, f"{hdr!r} -> {r.status}"


@pytest.mark.parametrize("hdr", ["bytes=abc", "bytes=5-2", "bytes=", "items=0-5", "garbage"])
async def test_malformed_range_never_causes_a_server_error_on_cached_bytes(server, blob, hdr):
    item = MediaItem("1", str(blob), "Film", 10.0, "video", thumbnail_png=b"\x89PNG" + b"x" * 100)
    url = server.publish_plan(item, plan())[0]
    async with server.sess.get(server.poster_url(url), headers={"Range": hdr}) as r:
        assert r.status == 416, f"{hdr!r} -> {r.status}"


async def test_stream_route_refuses_byte_seeking_but_not_the_first_byte(server, blob):
    item = MediaItem("1", str(blob), "Film", 10.0, "video")
    p = probe.PlaybackPlan("dlna", "remux", "video/mp2t", "video", duration=10.0, ffmpeg_out=["-f", "mpegts"])
    url, _ = server.publish_plan(item, p)
    assert "/s/" in url
    async with server.sess.head(url, headers={"Range": "bytes=500-"}) as r:
        assert r.status == 416
    async with server.sess.head(url, headers={"Range": "bytes=0-"}) as r:
        assert r.status == 200


# ----------------------------------------------------------------------------- query parameters

async def test_garbage_seek_parameter_is_a_client_error_on_stream(server, blob):
    item = MediaItem("1", str(blob), "Film", 10.0, "video")
    p = probe.PlaybackPlan("dlna", "remux", "video/mp2t", "video", duration=10.0, ffmpeg_out=["-f", "mpegts"])
    url, _ = server.publish_plan(item, p)
    for bad in ("abc", "nan", "inf", "-5"):
        async with server.sess.head(url + "?t=" + bad) as r:
            assert r.status == 400, f"t={bad} -> {r.status}"


async def test_garbage_seek_parameter_is_a_client_error_on_subtitles(server, tmp_path):
    srt = tmp_path / "s.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    item = MediaItem("1", str(tmp_path / "f.mp4"), "Film", 10.0, "video")
    p = probe.PlaybackPlan("dlna", "direct", "video/mp4", "video", duration=10.0,
                           subtitles=[probe.SubtitleTrack(index=-1, codec="subrip", external_path=str(srt))])
    url = server.publish_plan(item, p)[0]
    token = url.split("/")[4]
    for bad in ("abc", "nan", "-1"):
        async with server.sess.get(f"{base(server)}/sub/{token}/0.vtt?t={bad}") as r:
            assert r.status == 400, f"t={bad} -> {r.status}"


async def test_bad_subtitle_names_are_404(server, blob):
    url = publish(server, blob)
    token = url.split("/")[4]
    for name in ("0.vtt", "x.vtt", "../0.vtt", "0.exe", "-1.srt", "99999999999999999999.srt", "0.vtt%00"):
        async with server.sess.get(f"{base(server)}/sub/{token}/{name}") as r:
            assert r.status == 404, f"{name} -> {r.status}"


async def test_cors_preflight_works_without_a_token(server):
    async with server.sess.options(f"{base(server)}/f/anything/x", headers={
            "Origin": "http://cast.receiver", "Access-Control-Request-Method": "GET"}) as r:
        assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "*"
    # the CORS wildcard must not appear to allow state-changing methods
    assert "POST" not in r.headers["Access-Control-Allow-Methods"]


async def test_server_binds_only_to_the_requested_host(server):
    sock_hosts = {s.getsockname()[0] for s in server._runner.sites and [x for site in server._runner.sites
                                                                         for x in site._server.sockets]}
    assert sock_hosts == {"127.0.0.1"}


# cached-bytes routes (poster, converted images, subtitles) use the server's own Range implementation
@pytest.mark.parametrize("hdr,status,expected,crange", [
    ("bytes=0-0", 206, DATA[0:1], "bytes 0-0/10240"),
    ("bytes=10-19", 206, DATA[10:20], "bytes 10-19/10240"),
    ("bytes=10239-", 206, DATA[10239:], "bytes 10239-10239/10240"),
    ("bytes=-10", 206, DATA[-10:], "bytes 10230-10239/10240"),
    ("bytes=-99999", 206, DATA, "bytes 0-10239/10240"),            # suffix longer than the body
    ("bytes=10000-99999", 206, DATA[10000:], "bytes 10000-10239/10240"),
    ("bytes=10240-", 416, None, "bytes */10240"),
    ("bytes=99999-100000", 416, None, "bytes */10240"),
])
async def test_cached_bytes_range_semantics(server, blob, hdr, status, expected, crange):
    item = MediaItem("1", str(blob), "Film", 10.0, "video", thumbnail_png=DATA)
    url = server.publish_plan(item, plan())[0]
    async with server.sess.get(server.poster_url(url), headers={"Range": hdr}) as r:
        assert r.status == status, (hdr, r.status)
        assert r.headers["Content-Range"] == crange
        if expected is not None:
            assert (await r.read()) == expected
    async with server.sess.head(server.poster_url(url), headers={"Range": hdr}) as r:
        assert r.status == status and (await r.read()) == b""


# ----------------------------------------------------------------------------- receiver binding and token lifetime
# DLNA has no receiver authentication, so the URL is the only credential: it is valid for the one
# receiver it was published for (source IP) and for the playback session only. The TV legitimately
# issues many GETs (Range, retries, HEAD, poster, subtitles), so "one-shot" would break playback.

def publish_for(server, blob, target_ip, **kw):
    item = MediaItem("1", str(blob), "Film", 10.0, "video", thumbnail_png=b"\x89PNG" + b"x" * 50)
    subs = [probe.SubtitleTrack(index=-1, codec="subrip", external_path=str(blob))]
    p = probe.PlaybackPlan("dlna", "direct", "video/mp4", "video", duration=10.0, subtitles=subs)
    return server.publish_plan(item, p, target_ip=target_ip, **kw)[0]


async def test_url_works_for_the_target_receiver_with_many_requests(server, blob):
    url = publish_for(server, blob, "127.0.0.1")   # the test client connects from 127.0.0.1
    for hdr in ({}, {"Range": "bytes=0-9"}, {"Range": "bytes=100-"}, {}):
        async with server.sess.get(url, headers=hdr) as r:
            assert r.status in (200, 206)
    async with server.sess.head(url) as r:
        assert r.status == 200
    async with server.sess.get(server.poster_url(url)) as r:
        assert r.status == 200


async def test_url_is_refused_from_any_other_ip_on_every_route(server, blob):
    url = publish_for(server, blob, "192.168.1.10")   # published for the TV; we are 127.0.0.1
    token = url.split("/")[4]
    urls = [url, server.poster_url(url), f"{base(server)}/sub/{token}/0.srt",
            f"{base(server)}/s/{token}/media.ts", f"{base(server)}/i/{token}/media.jpg"]
    for u in urls:
        async with server.sess.get(u) as r:
            assert r.status == 404, u
        async with server.sess.head(u) as r:
            assert r.status == 404, u


async def test_url_without_a_known_receiver_is_not_ip_bound(server, blob):
    url = publish(server, blob)   # no target_ip (tests, manual use)
    async with server.sess.get(url) as r:
        assert r.status == 200


async def test_binding_can_be_disabled(blob):
    s = ms.MediaServer(lan_ip="127.0.0.1", bind_to_target=False)
    await s.start(host="127.0.0.1", port=0)
    try:
        async with aiohttp.ClientSession() as sess:
            url = publish_for(s, blob, "192.168.1.10")
            async with sess.get(url) as r:
                assert r.status == 200
    finally:
        await s.stop()


async def test_live_url_is_bound_to_the_receiver(server):
    async def gen():
        yield b"x" * 188

    good = server.register_live("a", gen, target_ip="127.0.0.1")
    bad = server.register_live("b", gen, target_ip="192.168.1.10")
    async with server.sess.get(good) as r:
        assert r.status == 200
    async with server.sess.get(bad) as r:
        assert r.status == 404


async def test_unpublish_invalidates_the_url_for_the_receiver_too(server, blob):
    url = publish_for(server, blob, "127.0.0.1")
    async with server.sess.get(url) as r:
        assert r.status == 200
    server.unpublish(url)
    async with server.sess.get(url) as r:
        assert r.status == 404


async def test_idle_token_expires_but_activity_keeps_it_alive(blob):
    now = [1000.0]
    s = ms.MediaServer(lan_ip="127.0.0.1", token_ttl=60, clock=lambda: now[0])
    await s.start(host="127.0.0.1", port=0)
    try:
        async with aiohttp.ClientSession() as sess:
            url = publish_for(s, blob, "127.0.0.1")
            for dt in (50, 50, 50):             # a request every 50 s keeps a 60 s idle TTL alive
                now[0] += dt
                async with sess.get(url) as r:
                    assert r.status == 200
            now[0] += 61
            async with sess.get(url) as r:
                assert r.status == 404
            assert not s.items                  # and the entry is dropped, not just hidden
    finally:
        await s.stop()
