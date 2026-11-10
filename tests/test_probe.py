from airlinx.core import probe


def mk(fmt, streams, duration="60.0"):
    return {"format": {"format_name": fmt, "duration": duration}, "streams": streams}


def v(codec, idx=0, w=1920, h=1080, pix="yuv420p", **extra):
    return {"index": idx, "codec_type": "video", "codec_name": codec, "width": w, "height": h, "pix_fmt": pix, **extra}


def a(codec, idx=1, default=0):
    return {"index": idx, "codec_type": "audio", "codec_name": codec, "disposition": {"default": default}}


def s(codec, idx, lang="pol", title=None):
    tags = {"language": lang}
    if title:
        tags["title"] = title
    return {"index": idx, "codec_type": "subtitle", "codec_name": codec, "tags": tags, "disposition": {"default": 0}}


MP4 = "mov,mp4,m4a,3gp,3g2,mj2"
MKV = "matroska,webm"


def plan(data, target, external=None):
    return probe.decide(probe.parse_probe(data, external), target, nvenc=True)


def test_mp4_h264_aac_direct_both():
    d = mk(MP4, [v("h264"), a("aac")])
    for t in ("cast", "dlna"):
        p = plan(d, t)
        assert p.mode == "direct" and p.mime == "video/mp4" and p.kind == "video"
        assert p.duration == 60.0


def test_mkv_h264_aac_remux_for_cast_direct_for_dlna():
    d = mk(MKV, [v("h264"), a("aac")])
    c = plan(d, "cast")
    assert c.mode == "remux" and c.mime == "video/mp4"
    assert "-c:v" in c.ffmpeg_out and c.ffmpeg_out[c.ffmpeg_out.index("-c:v") + 1] == "copy"
    assert "frag_keyframe+empty_moov+default_base_moof" in c.ffmpeg_out
    dl = plan(d, "dlna")
    assert dl.mode == "direct" and dl.mime == "video/x-mkv"


def test_mkv_hevc_dts_audio_transcode_video_copy():
    d = mk(MKV, [v("hevc", pix="yuv420p10le", w=3840, h=2160), a("dts")])
    c = plan(d, "cast")
    assert c.mode == "transcode" and c.video_action == "copy" and c.audio_action == "encode"
    assert "hvc1" in c.ffmpeg_out and "aac" in c.ffmpeg_out
    dl = plan(d, "dlna")
    assert dl.mode == "transcode" and dl.mime == "video/mpeg" and dl.video_action == "copy"
    assert "mpegts" in dl.ffmpeg_out


def test_avi_mpeg4_transcodes_video_with_nvenc_for_cast():
    d = mk("avi", [v("mpeg4"), a("mp3")])
    c = plan(d, "cast")
    assert c.mode == "transcode" and c.video_action == "encode" and c.audio_action == "copy"
    assert "h264_nvenc" in c.ffmpeg_out
    assert plan(d, "dlna").mode == "direct"


def test_h264_10bit_needs_transcode_on_cast():
    c = plan(mk(MP4, [v("h264", pix="yuv420p10le"), a("aac")]), "cast")
    assert c.mode == "transcode" and "yuv420p" in c.ffmpeg_out


def test_libx264_fallback_without_nvenc():
    info = probe.parse_probe(mk("avi", [v("mpeg4"), a("mp3")]))
    p = probe.decide(info, "cast", nvenc=False)
    assert "libx264" in p.ffmpeg_out


def test_8k_is_scaled():
    c = plan(mk(MP4, [v("hevc", w=7680, h=4320), a("aac")]), "cast")
    assert c.mode == "transcode" and any("scale=" in x for x in c.ffmpeg_out)


def test_webm_vp9_opus_direct_cast():
    c = plan(mk(MKV, [v("vp9"), a("opus")]), "cast")
    assert c.mode == "direct" and c.mime == "video/webm"


def test_cover_art_mp3_is_audio():
    d = mk("mp3", [a("mp3", idx=0), v("mjpeg", idx=1, disposition={"attached_pic": 1})], "200")
    for t in ("cast", "dlna"):
        p = plan(d, t)
        assert p.kind == "audio" and p.mode == "direct" and p.mime == "audio/mpeg"


def test_audio_flac_direct_and_ape_transcode():
    assert plan(mk("flac", [a("flac", 0)]), "cast").mime == "audio/flac"
    p = plan(mk("ape", [a("ape", 0)]), "cast")
    assert p.mode == "transcode" and p.mime == "audio/mpeg"


def test_images():
    jpg = mk("image2", [v("mjpeg", w=4000, h=2000)], duration=None)
    p = plan(jpg, "cast")
    assert p.kind == "image" and p.mode == "direct" and p.mime == "image/jpeg"
    heic = mk("mov,mp4,m4a,3gp,3g2,mj2", [v("hevc", w=4032, h=3024)], duration=None)
    heic["streams"][0]["codec_name"] = "heic"
    p = plan(heic, "dlna")
    assert p.kind == "image" and p.mode == "transcode" and p.mime == "image/jpeg"
    webp = mk("webp_pipe", [v("webp")], duration=None)
    assert plan(webp, "cast").mode == "direct"
    assert plan(webp, "dlna").mode == "transcode"


def test_subtitles_text_and_bitmap_and_external():
    d = mk(MKV, [v("h264"), a("aac"), s("subrip", 2, "pol", "Polski"), s("hdmv_pgs_subtitle", 3, "eng"),
                 s("ass", 4, "eng")])
    p = plan(d, "cast", external=["/x/film.en.srt"])
    texts = [t for t in p.subtitles if t.text]
    assert [t.index for t in texts] == [2, 4, -1]
    assert texts[0].label == "Polski"
    assert texts[-1].external_path == "/x/film.en.srt" and texts[-1].codec == "subrip"
    bitmap = [t for t in p.subtitles if not t.text]
    assert bitmap and bitmap[0].codec == "hdmv_pgs_subtitle"
    # remux never carries subtitle streams into the output
    assert "-sn" in p.ffmpeg_out


def test_default_audio_stream_chosen():
    d = mk(MKV, [v("h264"), a("ac3", 1), a("aac", 2, default=1)])
    p = plan(d, "cast")
    assert "0:2" in p.ffmpeg_out and p.audio_action == "copy"


def test_extension_guess_fallback():
    assert probe.plan_for_url_guess("http://x/a.mp4?x=1", "cast").mode == "direct"
    p = probe.plan_for_url_guess("/x/a.xyz", "dlna")
    assert p.mode == "transcode" and "0:v:0?" in p.ffmpeg_out


def test_redact_url():
    r = probe.redact_url
    assert r("http://user:pw@host:8080/a/b.mp4?token=abc#x") == "http://host:8080/a/b.mp4"
    assert r("http://192.0.2.16:4000/s/SeCrEtToKeN123/media.mp4?t=10") == "http://192.0.2.16:4000/s/<token>/media.mp4"
    assert r("http://h/live/tok/mirror.ts") == "http://h/live/<token>/mirror.ts"
    assert r("/home/u/film.mkv") == "/home/u/film.mkv"
    s = probe.scrub_urls("Error opening https://a:b@cdn.x/v.m3u8?sig=XYZ: 403")
    assert "sig=" not in s and "a:b" not in s and "https://cdn.x/v.m3u8" in s


def test_ffprobe_error_has_no_url():
    import pytest as _pt
    url = "http://user:secret@127.0.0.1:1/video.mp4?sig=SECRET"
    with _pt.raises(RuntimeError) as ei:
        probe.run_ffprobe(url, timeout=10)
    msg = str(ei.value)
    assert "SECRET" not in msg and "secret" not in msg and "127.0.0.1" not in msg
    e = probe._probe_error(b"http://x/y?z=1: Connection refused\n/tmp/a.mkv: bad", "/tmp/a.mkv")
    assert "http://" not in str(e) and "/tmp/a.mkv" not in str(e) and "Connection refused" in str(e)
