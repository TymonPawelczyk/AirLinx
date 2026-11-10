"""ffprobe → playback plan per target ("cast" | "dlna").

Decision levels:
  * direct    — serve the file/URL as is (HTTP Range supported).
  * remux     — streams copied, container changed on the fly (ffmpeg -c copy).
  * transcode — at least one stream re-encoded (h264_nvenc / aac / mjpeg).

Cast (Default Media Receiver, CC1AD845):
  containers: MP4/MOV, WebM, MP3/AAC/FLAC/OGG/WAV audio, JPEG/PNG/GIF/WEBP images.
  video: h264 (8-bit), hevc (TV reports hevc), vp8, vp9, av1(webm/mp4).  audio: aac, mp3, opus, vorbis, flac.
  Anything else (MKV, AVI, TS...) with good codecs → remux to fragmented MP4.
Samsung DLNA DMR (S85F): MP4, MKV, TS, WebM, AVI, MOV; h264/hevc/mpeg2/mpeg4/vp9/av1; aac/ac3/eac3/mp3/
  flac/opus/vorbis/pcm.  Fallback output: MPEG-TS (video/mpeg) with h264 + aac.

Subtitles:
  * text tracks (subrip, ass/ssa, mov_text, webvtt, text) → WebVTT endpoint for Cast tracks;
    for DLNA they are offered as SRT via the Samsung `CaptionInfo.sec` extension (best effort).
  * bitmap tracks (PGS, DVD, DVB) are listed but not delivered — burn-in is a later TODO.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)

Target = Literal["cast", "dlna"]
Mode = Literal["direct", "remux", "transcode"]

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}
IMAGE_CODECS = {"mjpeg", "png", "gif", "webp", "bmp", "tiff", "jpegls", "jpeg2000", "heic", "hevc_image"}

CAST_VIDEO = {"h264", "hevc", "vp8", "vp9", "av1"}
CAST_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
DLNA_VIDEO = {"h264", "hevc", "mpeg2video", "mpeg4", "vp9", "av1", "vc1"}
DLNA_AUDIO = {"aac", "ac3", "eac3", "mp3", "mp2", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le"}

MAX_W, MAX_H = 4096, 2160


@dataclass
class SubtitleTrack:
    index: int               # absolute stream index in source (or -1 for external file)
    codec: str
    language: Optional[str] = None
    title: Optional[str] = None
    text: bool = True        # text-based → deliverable as WebVTT/SRT
    external_path: Optional[str] = None
    default: bool = False

    @property
    def label(self) -> str:
        return self.title or self.language or (os.path.basename(self.external_path) if self.external_path else f"#{self.index}")


@dataclass
class ProbeInfo:
    format_name: str = ""
    duration: Optional[float] = None
    start_time: float = 0.0
    video: Optional[dict] = None      # first (non-attached-pic) video stream
    audio: Optional[dict] = None      # chosen audio stream (default flag or first)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    kind: Literal["video", "audio", "image"] = "video"
    cover_art: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class PlaybackPlan:
    target: str
    mode: Mode
    mime: str
    kind: Literal["video", "audio", "image"]
    duration: Optional[float] = None
    container: str = ""                 # output container for remux/transcode ("mp4","mpegts","mp3","mjpeg")
    video_action: Optional[str] = None  # "copy" | "encode" | None
    audio_action: Optional[str] = None
    ffmpeg_out: list[str] = field(default_factory=list)   # args after the input (without output target)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    reason: str = ""
    start_time: float = 0.0             # container start_time; embedded subs are shifted by it

    @property
    def seekable_bytes(self) -> bool:
        return self.mode == "direct"


# ---------------------------------------------------------------- log redaction

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
# MediaServer path layout: /<prefix>/<token>/...
_TOKEN_PATH = re.compile(r"^/(f|s|i|p|sub|live)/[^/]+")


def redact_url(url: str) -> str:
    """URL safe for logs: no userinfo, query, fragment or MediaServer token. Non-URLs pass through."""
    if not isinstance(url, str) or "://" not in url:
        return url
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{u.port}" if u.port else "")
    except ValueError:
        return "<url>"
    path = _TOKEN_PATH.sub(lambda m: f"/{m.group(1)}/<token>", u.path)
    return urlunsplit((u.scheme, netloc, path, "", ""))


def scrub_urls(text: str) -> str:
    """Replace every URL inside free text (error messages, ffmpeg stderr) with a redacted form."""
    return _URL_RE.sub(lambda m: redact_url(m.group(0)), text or "")


def _probe_error(stderr: bytes, path: str) -> RuntimeError:
    msg = stderr.decode(errors="replace")
    if path:
        msg = msg.replace(path, "<input>")
    msg = _URL_RE.sub("<url>", msg)
    return RuntimeError(f"ffprobe failed: {msg[:300]}")


# ---------------------------------------------------------------- ffprobe

def _ffprobe_cmd(path: str) -> list[str]:
    return ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]


def run_ffprobe(path: str, timeout: float = 20) -> dict:
    out = subprocess.run(_ffprobe_cmd(path), capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise _probe_error(out.stderr, path)
    return json.loads(out.stdout or b"{}")


async def run_ffprobe_async(path: str, timeout: float = 20) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *_ffprobe_cmd(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise _probe_error(stderr, path)
    return json.loads(stdout or b"{}")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def parse_probe(data: dict, external_subs: Optional[list[str]] = None) -> ProbeInfo:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    info = ProbeInfo(format_name=fmt.get("format_name", ""), duration=_f(fmt.get("duration")),
                     start_time=_f(fmt.get("start_time")) or 0.0, raw=data)
    videos = [s for s in streams if s.get("codec_type") == "video"]
    real_videos = [s for s in videos if not (s.get("disposition") or {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    info.cover_art = len(videos) > len(real_videos)

    if real_videos:
        info.video = real_videos[0]
    if audios:
        info.audio = next((a for a in audios if (a.get("disposition") or {}).get("default")), audios[0])

    fmt_names = set(info.format_name.split(","))
    if info.video and (info.video.get("codec_name") in IMAGE_CODECS) and (
        fmt_names & {"image2", "png_pipe", "jpeg_pipe", "webp_pipe", "gif", "bmp_pipe", "tiff_pipe", "mjpeg"}
        or (info.duration is None and not audios)
    ):
        info.kind = "image"
    elif info.video:
        info.kind = "video"
    elif info.audio:
        info.kind = "audio"
    if info.duration is None:
        for s in (info.video, info.audio):
            if s and _f(s.get("duration")):
                info.duration = _f(s.get("duration"))
                break

    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        codec = s.get("codec_name") or "unknown"
        tags = s.get("tags") or {}
        info.subtitles.append(SubtitleTrack(
            index=int(s.get("index", 0)), codec=codec, language=tags.get("language"), title=tags.get("title"),
            text=codec in TEXT_SUB_CODECS, default=bool((s.get("disposition") or {}).get("default"))))
    for p in external_subs or []:
        ext = os.path.splitext(p)[1].lower().lstrip(".")
        codec = {"srt": "subrip", "vtt": "webvtt", "ass": "ass", "ssa": "ssa", "sub": "subviewer"}.get(ext, ext)
        info.subtitles.append(SubtitleTrack(index=-1, codec=codec, external_path=p, text=True,
                                             title=os.path.basename(p)))
    return info


# ---------------------------------------------------------------- decisions

def _container(info: ProbeInfo) -> str:
    n = set(info.format_name.split(","))
    if n & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
        return "mp4"
    if "matroska" in n or "webm" in n:
        # ffprobe reports "matroska,webm" for both; decide by codecs
        vc = (info.video or {}).get("codec_name")
        ac = (info.audio or {}).get("codec_name")
        if vc in (None, "vp8", "vp9", "av1") and ac in (None, "opus", "vorbis") and (vc or ac):
            return "webm"
        return "matroska"
    if "mpegts" in n:
        return "mpegts"
    if "avi" in n:
        return "avi"
    for k in ("mp3", "flac", "ogg", "wav", "aac", "jpeg_pipe", "png_pipe", "gif", "webp_pipe", "image2"):
        if k in n:
            return k
    return info.format_name or "unknown"


def _is_8bit(v: dict) -> bool:
    pf = v.get("pix_fmt") or "yuv420p"
    return not any(t in pf for t in ("10", "12", "16"))


def _video_too_big(v: dict) -> bool:
    return (v.get("width") or 0) > MAX_W or (v.get("height") or 0) > MAX_H


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=10)
        return b"h264_nvenc" in out.stdout
    except Exception:  # pragma: no cover
        return False


def video_encode_args(v: Optional[dict], nvenc: Optional[bool] = None) -> list[str]:
    nvenc = nvenc_available() if nvenc is None else nvenc
    args: list[str] = []
    if v and _video_too_big(v):
        args += ["-vf", f"scale=w={MAX_W}:h={MAX_H}:force_original_aspect_ratio=decrease:force_divisible_by=2"]
    if nvenc:
        args += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21",
                 "-b:v", "15M", "-maxrate", "30M", "-bufsize", "30M", "-profile:v", "high"]
    else:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high"]
    args += ["-pix_fmt", "yuv420p", "-g", "60"]
    return args


AUDIO_ENC = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
FMP4 = ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof"]
MPEGTS = ["-f", "mpegts", "-mpegts_flags", "+resend_headers"]

CAST_DIRECT_MIME = {"mp4": "video/mp4", "webm": "video/webm"}
CAST_AUDIO_DIRECT = {"mp3": ("mp3", "audio/mpeg"), "aac": ("aac", "audio/aac"), "flac": ("flac", "audio/flac"),
                     "ogg": (None, "audio/ogg"), "wav": ("pcm_s16le", "audio/wav")}
DLNA_DIRECT_MIME = {"mp4": "video/mp4", "matroska": "video/x-mkv", "webm": "video/webm",
                    "mpegts": "video/mpeg", "avi": "video/x-msvideo"}
IMAGE_MIME = {"mjpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
              "bmp": "image/bmp"}


def decide(info: ProbeInfo, target: Target, *, nvenc: Optional[bool] = None) -> PlaybackPlan:
    if target == "cast":
        plan = _decide_cast(info, nvenc)
    elif target == "dlna":
        plan = _decide_dlna(info, nvenc)
    else:
        raise ValueError(f"unknown target {target!r}")
    plan.duration = info.duration
    plan.start_time = info.start_time
    plan.subtitles = [s for s in info.subtitles]
    return plan


def _image_plan(info: ProbeInfo, target: str, ok: set[str]) -> PlaybackPlan:
    codec = (info.video or {}).get("codec_name", "")
    if codec in ok and not _video_too_big(info.video or {}):
        return PlaybackPlan(target, "direct", IMAGE_MIME[codec], "image", reason=f"image {codec}")
    vf = f"scale=w={MAX_W}:h={MAX_H}:force_original_aspect_ratio=decrease"
    return PlaybackPlan(target, "transcode", "image/jpeg", "image", container="mjpeg", video_action="encode",
                        ffmpeg_out=["-frames:v", "1", "-vf", vf, "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe"],
                        reason=f"image {codec} → jpeg")


def _audio_plan(info: ProbeInfo, target: str, ok: set[str], container: str) -> PlaybackPlan:
    ac = (info.audio or {}).get("codec_name")
    if target == "cast":
        direct = CAST_AUDIO_DIRECT.get(container)
        if container == "mp4" and ac in {"aac", "mp3", "flac", "opus"}:
            return PlaybackPlan(target, "direct", "audio/mp4", "audio", reason="audio mp4")
        if direct and (direct[0] is None and ac in {"opus", "vorbis", "flac"} or direct[0] == ac or
                       (container == "wav" and ac and ac.startswith("pcm_s16"))):
            return PlaybackPlan(target, "direct", direct[1], "audio", reason=f"audio {container}")
        if container == "webm" and ac in {"opus", "vorbis"}:
            return PlaybackPlan(target, "direct", "audio/webm", "audio", reason="audio webm")
    else:
        dl = {"mp3": "audio/mpeg", "aac": "audio/vnd.dlna.adts", "flac": "audio/x-flac", "ogg": "audio/ogg",
              "wav": "audio/x-wav"}
        if container == "mp4" and ac in ok:
            return PlaybackPlan(target, "direct", "audio/mp4", "audio", reason="audio mp4")
        if container in dl and (ac in ok or container == "wav"):
            return PlaybackPlan(target, "direct", dl[container], "audio", reason=f"audio {container}")
    return PlaybackPlan(target, "transcode", "audio/mpeg", "audio", container="mp3", audio_action="encode",
                        ffmpeg_out=["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"],
                        reason=f"audio {ac} in {container} → mp3")


def _map_args(info: ProbeInfo) -> list[str]:
    args = []
    if info.video:
        args += ["-map", f"0:{info.video['index']}" if "index" in info.video else "0:v:0?"]
    if info.audio:
        args += ["-map", f"0:{info.audio['index']}" if "index" in info.audio else "0:a:0?"]
    return args + ["-sn", "-dn"]


def _decide_cast(info: ProbeInfo, nvenc: Optional[bool]) -> PlaybackPlan:
    container = _container(info)
    if info.kind == "image":
        return _image_plan(info, "cast", {"mjpeg", "png", "gif", "webp"})
    if info.kind == "audio":
        return _audio_plan(info, "cast", CAST_AUDIO, container)

    v = info.video or {}
    a = info.audio
    vc, ac = v.get("codec_name"), (a or {}).get("codec_name")
    v_ok = vc in CAST_VIDEO and not _video_too_big(v) and (vc != "h264" or _is_8bit(v))
    a_ok = a is None or ac in CAST_AUDIO
    if v_ok and a_ok and container in CAST_DIRECT_MIME:
        if container == "webm" or vc not in ("vp8",):
            return PlaybackPlan("cast", "direct", CAST_DIRECT_MIME[container], "video",
                                video_action=None, reason=f"{container} {vc}/{ac}")
    # MP4 output can't carry vorbis reliably; opus/flac are fine in fMP4.
    out = _map_args(info)
    va = "copy" if v_ok and vc in ("h264", "hevc", "av1", "vp9") else "encode"
    aa = None if a is None else ("copy" if ac in {"aac", "mp3", "opus", "flac"} else "encode")
    out += ["-c:v", "copy"] if va == "copy" else video_encode_args(v, nvenc)
    if va == "copy" and vc == "hevc":
        out += ["-tag:v", "hvc1"]
    if aa == "copy":
        out += ["-c:a", "copy"]
    elif aa == "encode":
        out += AUDIO_ENC
    mode: Mode = "remux" if va == "copy" and aa in (None, "copy") else "transcode"
    return PlaybackPlan("cast", mode, "video/mp4", "video", container="mp4", video_action=va, audio_action=aa,
                        ffmpeg_out=out + FMP4, reason=f"{container} {vc}/{ac} → fmp4 ({va}/{aa})")


def _decide_dlna(info: ProbeInfo, nvenc: Optional[bool]) -> PlaybackPlan:
    container = _container(info)
    if info.kind == "image":
        return _image_plan(info, "dlna", {"mjpeg", "png"})
    if info.kind == "audio":
        return _audio_plan(info, "dlna", DLNA_AUDIO, container)

    v = info.video or {}
    a = info.audio
    vc, ac = v.get("codec_name"), (a or {}).get("codec_name")
    v_ok = vc in DLNA_VIDEO and not _video_too_big(v)
    a_ok = a is None or ac in DLNA_AUDIO
    if v_ok and a_ok and container in DLNA_DIRECT_MIME:
        return PlaybackPlan("dlna", "direct", DLNA_DIRECT_MIME[container], "video", reason=f"{container} {vc}/{ac}")
    out = _map_args(info)
    va = "copy" if v_ok and vc in ("h264", "hevc", "mpeg2video") else "encode"
    aa = None if a is None else ("copy" if ac in {"aac", "ac3", "eac3", "mp3", "mp2"} else "encode")
    out += ["-c:v", "copy"] if va == "copy" else video_encode_args(v, nvenc)
    if aa == "copy":
        out += ["-c:a", "copy"]
    elif aa == "encode":
        out += AUDIO_ENC
    mode: Mode = "remux" if va == "copy" and aa in (None, "copy") else "transcode"
    return PlaybackPlan("dlna", mode, "video/mpeg", "video", container="mpegts", video_action=va, audio_action=aa,
                        ffmpeg_out=out + MPEGTS, reason=f"{container} {vc}/{ac} → mpegts ({va}/{aa})")


def plan_for_url_guess(path: str, target: Target) -> PlaybackPlan:
    """Cheap fallback when ffprobe fails: guess from the extension, transcode unknowns."""
    ext = os.path.splitext(path.split("?", 1)[0])[1].lower()
    guess = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg",
             ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    if target == "dlna":
        guess.update({".mkv": "video/x-mkv", ".ts": "video/mpeg"})
    if ext in guess:
        mime = guess[ext]
        return PlaybackPlan(target, "direct", mime, mime.split("/")[0] if mime.split("/")[0] in ("audio", "image") else "video",  # type: ignore[arg-type]
                            reason="extension guess")
    info = ProbeInfo(format_name="unknown", video={"codec_name": "unknown"}, audio={"codec_name": "unknown"})
    return decide(info, target)


async def probe_and_decide(path: str, target: Target, external_subs: Optional[list[str]] = None) -> PlaybackPlan:
    data = await run_ffprobe_async(path)
    return decide(parse_probe(data, external_subs), target)
