"""aiohttp media server for Cast and DLNA renderers.

URL layout (tokens are unguessable, `secrets.token_urlsafe`):
  /f/<token>/<name>          direct file — HTTP Range, HEAD, DLNA OP=01 (byte seek)
  /s/<token>/<name>[?t=sec]  ffmpeg remux/transcode streamed on the fly — no Range;
                             DLNA OP=10 (TimeSeekRange.dlna.org: npt=... honoured), `t` = start offset
  /i/<token>/<name>          converted image (cached bytes, Range ok)
  /p/<token>/poster.png      MediaItem.thumbnail_png
  /sub/<token>/<n>.vtt|.srt  subtitle track n (WebVTT for Cast, SRT for Samsung DLNA)  [?t=sec shifts]
  /live/<token>/<name>       live MPEG-TS (register_live) — DLNA OP=00, no seek

A token works only for the receiver it was published for (peer IP == target_ip) and while the playback
session lives (unpublish / unregister_live, plus a sliding idle TTL); see MediaServer.__init__.

Players rely on this layout: a URL with "/s/" is not byte-seekable, so seeking means reloading
with `with_start(url, seconds)`.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import math
import os
import re
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Literal, Optional, Union
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from aiohttp import web

from . import probe
from .probe import redact_url, scrub_urls  # re-exported for other modules
from .models import MediaItem

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
STDERR_TAIL_LINES = 200
TOKEN_IDLE_TTL = 6 * 3600.0   # a paused film may sit idle for hours; explicit unpublish is the normal end

# DLNA.ORG_FLAGS bits
F_SENDER_PACED = 1 << 31
F_TIME_SEEK = 1 << 30
F_BYTE_SEEK = 1 << 29
F_STREAMING = 1 << 24
F_INTERACTIVE = 1 << 23
F_BACKGROUND = 1 << 22
F_STALL = 1 << 21
F_V15 = 1 << 20

DLNA_PN = {"image/jpeg": "JPEG_LRG", "image/png": "PNG_LRG", "audio/mpeg": "MP3"}


def dlna_flags(bits: int) -> str:
    return f"{bits:08X}" + "0" * 24


def dlna_content_features(kind: Literal["file", "stream", "live", "image"], mime: str = "") -> str:
    """Value for contentFeatures.dlna.org header and the 4th field of DIDL protocolInfo."""
    pn = DLNA_PN.get(mime)
    parts = [f"DLNA.ORG_PN={pn}"] if pn else []
    if kind == "image":
        parts += ["DLNA.ORG_OP=01", "DLNA.ORG_CI=0", "DLNA.ORG_FLAGS=" + dlna_flags(F_INTERACTIVE | F_BACKGROUND | F_V15)]
    elif kind == "file":
        parts += ["DLNA.ORG_OP=01", "DLNA.ORG_CI=0",
                  "DLNA.ORG_FLAGS=" + dlna_flags(F_STREAMING | F_BACKGROUND | F_STALL | F_V15)]
    elif kind == "stream":
        parts += ["DLNA.ORG_OP=10", "DLNA.ORG_CI=1",
                  "DLNA.ORG_FLAGS=" + dlna_flags(F_STREAMING | F_BACKGROUND | F_STALL | F_V15)]
    else:  # live
        parts += ["DLNA.ORG_OP=00", "DLNA.ORG_CI=1", "DLNA.ORG_FLAGS=" + dlna_flags(F_STREAMING | F_STALL | F_V15)]
    return ";".join(parts)


def url_kind(url: str, mime: str = "") -> Literal["file", "stream", "live", "image"]:
    path = urlparse(url).path
    if path.startswith("/live/"):
        return "live"
    if path.startswith("/s/"):
        return "stream"
    if path.startswith("/i/") or mime.startswith("image/"):
        return "image"
    return "file"


def is_stream_url(url: str) -> bool:
    return urlparse(url).path.startswith("/s/")


def with_start(url: str, seconds: float) -> str:
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if seconds > 0.5:
        q["t"] = f"{seconds:.3f}"
    else:
        q.pop("t", None)
    return urlunparse(u._replace(query=urlencode(q)))


def start_of(url: str) -> float:
    try:
        return float(parse_qs(urlparse(url).query).get("t", ["0"])[0])
    except ValueError:
        return 0.0


def get_lan_ip(peer: Optional[str] = None) -> str:
    """Source IP the kernel would use to reach `peer` (default: internet route). No packets sent."""
    for target in ([peer] if peer else []) + ["8.8.8.8", "192.168.1.1"]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def _norm_ip(ip: Optional[str]) -> Optional[str]:
    if not ip:
        return None
    return ip[7:] if ip.lower().startswith("::ffff:") else ip


_NPT = re.compile(r"npt\s*=\s*([0-9:.]+)?\s*-")


def parse_npt(header: str) -> Optional[float]:
    m = _NPT.search(header or "")
    if not m or not m.group(1):
        return None
    val = m.group(1)
    if ":" in val:
        secs = 0.0
        for part in val.split(":"):
            secs = secs * 60 + float(part)
        return secs
    return float(val)


def fmt_npt(sec: float) -> str:
    h, rem = divmod(max(sec, 0.0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


@dataclass
class Published:
    token: str
    item: MediaItem
    target: str
    plan: probe.PlaybackPlan
    src: str
    is_remote: bool
    url: str = ""
    host: str = ""                      # LAN IP used in this item's URLs (route towards the renderer)
    image_cache: Optional[bytes] = None
    sub_cache: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    procs: set = field(default_factory=set)   # ffmpeg processes currently streaming this item
    allowed_ip: Optional[str] = None    # only this peer may use the token (None = unrestricted)
    last_seen: float = 0.0              # sliding idle TTL, see MediaServer.token_ttl

    @property
    def text_subtitles(self) -> list[probe.SubtitleTrack]:
        return [s for s in self.plan.subtitles if s.text]


LiveSource = Union[AsyncIterator[bytes], Callable[[], AsyncIterator[bytes]]]


class _Live:
    def __init__(self, name: str, source: LiveSource, mime: str, allowed_ip: Optional[str] = None):
        self.name, self.source, self.mime = name, source, mime
        self.allowed_ip = allowed_ip
        self.queues: set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self.clients: set[asyncio.Task] = set()   # request handlers currently streaming this live
        self.ended = False

    def close(self) -> None:
        """Stop the shared pump and end every client stream (their handlers get cancelled)."""
        if self.task:
            self.task.cancel()
        for t in list(self.clients):
            t.cancel()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self.queues.add(q)
        if self.task is None and not callable(self.source):
            self.task = asyncio.ensure_future(self._pump())
        return q

    async def _pump(self) -> None:
        try:
            async for chunk in self.source:  # type: ignore[union-attr]
                for q in list(self.queues):
                    if q.full():  # slow client: drop oldest chunk rather than stall the producer
                        with contextlib.suppress(asyncio.QueueEmpty):
                            q.get_nowait()
                    q.put_nowait(chunk)
        except Exception:
            log.error("live source %s failed: %s", self.name, scrub_urls(repr(sys.exc_info()[1])))
        finally:
            self.ended = True
            for q in list(self.queues):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)


class MediaServer:
    def __init__(self, lan_ip: Optional[str] = None, ffmpeg: str = "ffmpeg", *,
                 bind_to_target: Optional[bool] = None, token_ttl: float = TOKEN_IDLE_TTL,
                 clock: Callable[[], float] = time.monotonic):
        """DLNA renderers can't authenticate, so a URL is valid only for the receiver it was published for
        (`bind_to_target`: peer IP must equal `target_ip`; env AIRLINX_MEDIA_IP_BIND=0 disables it for a TV that
        fetches from a different address than it advertises) and for the playback session (`token_ttl` = idle
        seconds, sliding; unpublish/unregister_live invalidate at once)."""
        if bind_to_target is None:
            bind_to_target = os.environ.get("AIRLINX_MEDIA_IP_BIND", "1") != "0"
        self.bind_to_target, self.token_ttl, self._clock = bind_to_target, token_ttl, clock
        # lan_ip given explicitly = fixed override (tests, manual config). Otherwise the IP is
        # resolved per publish from the route towards the renderer (VPN / network changes).
        self._lan_override = lan_ip
        self.lan_ip = lan_ip
        self.port: int = 0
        self.ffmpeg = ffmpeg
        self.items: dict[str, Published] = {}
        self.lives: dict[str, _Live] = {}
        self._live_names: dict[str, str] = {}
        self._procs: set[asyncio.subprocess.Process] = set()
        self._runner: Optional[web.AppRunner] = None
        self.app = self._make_app()

    # ------------------------------------------------------------ lifecycle
    def _make_app(self) -> web.Application:
        app = web.Application(middlewares=[self._cors_mw])
        r = app.router
        r.add_route("*", "/f/{token}/{name}", self._h_file)
        r.add_route("*", "/s/{token}/{name}", self._h_stream)
        r.add_route("*", "/i/{token}/{name}", self._h_image)
        r.add_route("*", "/p/{token}/{name}", self._h_poster)
        r.add_route("*", "/sub/{token}/{name}", self._h_sub)
        r.add_route("*", "/live/{token}/{name}", self._h_live)
        return app

    async def start(self, host: str = "0.0.0.0", port: int = 0) -> None:
        if self._lan_override is None:
            self.lan_ip = get_lan_ip()
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        sock = site._server.sockets[0]  # type: ignore[union-attr]
        self.port = sock.getsockname()[1]
        log.info("media server on %s:%d (lan %s)", host, self.port, self.lan_ip)

    async def stop(self) -> None:
        for p in list(self._procs):
            with contextlib.suppress(ProcessLookupError):
                p.kill()
        for lv in list(self.lives.values()):
            lv.close()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    def lan_ip_for(self, target_ip: Optional[str] = None) -> str:
        if self._lan_override:
            return self._lan_override
        ip = get_lan_ip(target_ip)
        self.lan_ip = ip
        return ip

    def base_url_for(self, target_ip: Optional[str] = None) -> str:
        return f"http://{self.lan_ip_for(target_ip)}:{self.port}"

    def _base(self, host: str) -> str:
        return f"http://{host}:{self.port}"

    @property
    def base_url(self) -> str:
        """Base URL via the default route (no renderer known). Prefer base_url_for(device.ip)."""
        return self.base_url_for(None)

    # ------------------------------------------------------------ publishing
    def publish(self, item: MediaItem, target: Literal["cast", "dlna"], start: float = 0,
                target_ip: Optional[str] = None) -> tuple[str, str]:
        """Synchronous (runs ffprobe in-line, ~0.1 s). Prefer `publish_async` from the event loop."""
        src = item.path_or_url
        try:
            data = probe.run_ffprobe(src)
            plan = probe.decide(probe.parse_probe(data, item.subtitles), target)
        except Exception as e:
            log.warning("ffprobe failed for %s (%s) — guessing", redact_url(src), scrub_urls(str(e)))
            plan = probe.plan_for_url_guess(src, target)
        return self._register(item, target, plan, start, target_ip)

    async def publish_async(self, item: MediaItem, target: Literal["cast", "dlna"], start: float = 0,
                            target_ip: Optional[str] = None) -> tuple[str, str]:
        src = item.path_or_url
        try:
            plan = await probe.probe_and_decide(src, target, item.subtitles)
        except Exception as e:
            log.warning("ffprobe failed for %s (%s) — guessing", redact_url(src), scrub_urls(str(e)))
            plan = probe.plan_for_url_guess(src, target)
        return self._register(item, target, plan, start, target_ip)

    def publish_plan(self, item: MediaItem, plan: probe.PlaybackPlan, start: float = 0,
                     target_ip: Optional[str] = None) -> tuple[str, str]:
        """Publish with a precomputed plan (tests, or callers that already probed)."""
        return self._register(item, plan.target, plan, start, target_ip)

    def _register(self, item: MediaItem, target: str, plan: probe.PlaybackPlan, start: float,
                  target_ip: Optional[str] = None) -> tuple[str, str]:
        src = item.path_or_url
        if src.startswith("file://"):
            src = urlparse(src).path
        remote = bool(re.match(r"^[a-z]+://", src))
        if remote and plan.mode == "direct" and (target == "cast" or src.startswith("http://")):
            return src, plan.mime  # renderer fetches it itself
        if remote and plan.mode == "direct":  # https on DLNA → proxy through ffmpeg copy
            plan = probe.PlaybackPlan(target, "remux", "video/mpeg", plan.kind, plan.duration, "mpegts",
                                      "copy", "copy", ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy"] + probe.MPEGTS,
                                      plan.subtitles, "https proxy")
        token = secrets.token_urlsafe(16)
        pub = Published(token, item, target, plan, src, remote, host=self.lan_ip_for(target_ip),
                        allowed_ip=_norm_ip(target_ip) if self.bind_to_target else None, last_seen=self._clock())
        ext = {"video/mp4": "mp4", "video/mpeg": "ts", "video/webm": "webm", "video/x-mkv": "mkv",
               "video/x-msvideo": "avi", "audio/mpeg": "mp3", "image/jpeg": "jpg", "image/png": "png"}.get(plan.mime, "bin")
        name = quote(f"media.{ext}")
        if plan.kind == "image" and plan.mode != "direct":
            prefix = "i"
        elif plan.mode == "direct":
            prefix = "f"
        else:
            prefix = "s"
        url = f"{self._base(pub.host)}/{prefix}/{token}/{name}"
        if prefix == "s":
            url = with_start(url, start)
        pub.url = url
        self.items[token] = pub
        log.info("published %s for %s: %s (%s)", item.title, target, plan.mode, plan.reason)
        return url, plan.mime

    def unpublish(self, url_or_token: str) -> bool:
        """Invalidate the token (404 from now on), drop cached image/subtitle bytes and kill
        ffmpeg processes still streaming this item. Returns False if it wasn't published."""
        pub = self.info(url_or_token)
        if not pub:
            return False
        self.items.pop(pub.token, None)
        pub.image_cache = None
        pub.sub_cache.clear()
        for proc in list(pub.procs):
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        return True

    def info(self, url_or_token: str) -> Optional[Published]:
        if url_or_token in self.items:
            return self.items[url_or_token]
        parts = urlparse(url_or_token).path.split("/")
        return self.items.get(parts[2]) if len(parts) > 2 else None

    def poster_url(self, url_or_token: str) -> Optional[str]:
        pub = self.info(url_or_token)
        if not pub or not pub.item.thumbnail_png:
            return None
        return f"{self._base(pub.host)}/p/{pub.token}/poster.png"

    def subtitle_tracks(self, url_or_token: str, fmt: Literal["vtt", "srt"] = "vtt") -> list[dict]:
        """[{id, url, label, language, mime}] for text subtitle tracks of a published item."""
        pub = self.info(url_or_token)
        if not pub:
            return []
        t = start_of(url_or_token) if "://" in url_or_token else 0
        out = []
        for n, s in enumerate(pub.text_subtitles):
            url = f"{self._base(pub.host)}/sub/{pub.token}/{n}.{fmt}"
            out.append({"id": n + 1, "url": with_start(url, t), "label": s.label, "language": s.language or "und",
                        "mime": "text/vtt" if fmt == "vtt" else "application/x-subrip"})
        return out

    def register_live(self, name: str, source: LiveSource, mime: str = "video/mpeg",
                      target_ip: Optional[str] = None) -> str:
        """Expose a live byte stream (MPEG-TS). `source` = async iterator (fanned out to all clients)
        or a zero-arg callable returning a fresh async iterator per client."""
        self.unregister_live(name)
        token = secrets.token_urlsafe(16)
        self.lives[token] = _Live(name, source, mime, _norm_ip(target_ip) if self.bind_to_target else None)
        self._live_names[name] = token
        return f"{self.base_url_for(target_ip)}/live/{token}/{quote(name)}.ts"

    def unregister_live(self, name: str) -> None:
        token = self._live_names.pop(name, None)
        lv = self.lives.pop(token, None) if token else None
        if lv:
            lv.close()

    # ------------------------------------------------------------ http helpers
    @web.middleware
    async def _cors_mw(self, request: web.Request, handler):
        if request.method == "OPTIONS":
            resp: web.StreamResponse = web.Response(status=204)
        else:
            resp = await handler(request)
        h = resp.headers
        h["Access-Control-Allow-Origin"] = "*"
        h["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        h["Access-Control-Allow-Headers"] = "Content-Type, Range, Accept-Encoding, getcontentFeatures.dlna.org"
        h["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
        return resp

    def _peer_allowed(self, request: web.Request, allowed_ip: Optional[str]) -> bool:
        if allowed_ip is None:
            return True
        peer = _norm_ip(request.remote)
        # a receiver on this machine (virtual TV) reaches our LAN address from that same address
        return peer == allowed_ip or (allowed_ip.startswith("127.") and peer in ("127.0.0.1", self.lan_ip))

    def _get(self, request: web.Request) -> Published:
        pub = self.items.get(request.match_info["token"])
        if not pub:
            raise web.HTTPNotFound()
        now = self._clock()
        if now - pub.last_seen > self.token_ttl:
            self.unpublish(pub.token)
            raise web.HTTPNotFound()
        if not self._peer_allowed(request, pub.allowed_ip):
            # 404, same as an unknown token: don't confirm that the token exists
            log.warning("media request from %s refused (published for another receiver)", request.remote)
            raise web.HTTPNotFound()
        pub.last_seen = now
        if request.method not in ("GET", "HEAD"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])
        return pub

    @staticmethod
    def _dlna_headers(kind: Literal["file", "stream", "live", "image"], mime: str) -> dict:
        return {
            "transferMode.dlna.org": "Interactive" if kind == "image" else "Streaming",
            "contentFeatures.dlna.org": dlna_content_features(kind, mime),
            "Content-Type": mime,
        }

    def _caption_header(self, pub: Published) -> dict:
        if pub.target == "dlna" and pub.text_subtitles:
            return {"CaptionInfo.sec": f"{self._base(pub.host)}/sub/{pub.token}/0.srt"}
        return {}

    async def _bytes_response(self, request: web.Request, body: bytes, headers: dict) -> web.Response:
        size = len(body)
        try:
            rng = request.http_range
        except ValueError:   # malformed Range ("bytes=abc", "bytes=5-2"): unsatisfiable, not a server error
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        headers = dict(headers, **{"Accept-Ranges": "bytes"})
        if request.headers.get("Range"):
            start = rng.start or 0
            stop = size if rng.stop is None else min(rng.stop, size)
            if rng.start is not None and rng.start < 0:  # suffix
                start, stop = max(size + rng.start, 0), size
            if start >= size:
                return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{stop - 1}/{size}"
            return web.Response(status=206, body=b"" if request.method == "HEAD" else body[start:stop],
                                headers=headers)
        if request.method == "HEAD":
            headers["Content-Length"] = str(size)
            return web.Response(status=200, headers=headers)
        return web.Response(body=body, headers=headers)

    @staticmethod
    def _query_seconds(request: web.Request) -> float:
        """`?t=` start offset: a finite number >= 0, otherwise 400 (never an unhandled ValueError)."""
        raw = request.query.get("t", "0") or "0"
        try:
            t = float(raw)
        except ValueError:
            raise web.HTTPBadRequest(text="bad t")
        if not math.isfinite(t) or t < 0:
            raise web.HTTPBadRequest(text="bad t")
        return t

    # ------------------------------------------------------------ handlers
    async def _h_file(self, request: web.Request) -> web.StreamResponse:
        pub = self._get(request)
        if not os.path.isfile(pub.src):
            raise web.HTTPNotFound()
        kind = "image" if pub.plan.kind == "image" else "file"
        headers = self._dlna_headers(kind, pub.plan.mime)
        headers.update(self._caption_header(pub))
        return web.FileResponse(pub.src, chunk_size=CHUNK, headers=headers)

    async def _h_image(self, request: web.Request) -> web.StreamResponse:
        pub = self._get(request)
        if pub.image_cache is None:
            pub.image_cache = await self._ffmpeg_bytes(["-i", pub.src] + pub.plan.ffmpeg_out)
        return await self._bytes_response(request, pub.image_cache, self._dlna_headers("image", pub.plan.mime))

    async def _h_poster(self, request: web.Request) -> web.StreamResponse:
        pub = self._get(request)
        if not pub.item.thumbnail_png:
            raise web.HTTPNotFound()
        return await self._bytes_response(request, pub.item.thumbnail_png, self._dlna_headers("image", "image/png"))

    async def _h_sub(self, request: web.Request) -> web.StreamResponse:
        pub = self._get(request)
        m = re.fullmatch(r"(\d+)\.(vtt|srt)", request.match_info["name"])
        subs = pub.text_subtitles
        if not m or int(m.group(1)) >= len(subs):
            raise web.HTTPNotFound()
        n, fmt = int(m.group(1)), m.group(2)
        t = self._query_seconds(request)
        key = (n, fmt, round(t, 1))
        if key not in pub.sub_cache:
            pub.sub_cache[key] = await self._ffmpeg_bytes(subtitle_args(pub.src, subs[n], fmt, t, pub.plan.start_time))
        mime = "text/vtt; charset=utf-8" if fmt == "vtt" else "application/x-subrip; charset=utf-8"
        return await self._bytes_response(request, pub.sub_cache[key], {"Content-Type": mime})

    async def _h_stream(self, request: web.Request) -> web.StreamResponse:
        pub = self._get(request)
        start = self._query_seconds(request)
        headers = self._dlna_headers("stream", pub.plan.mime)
        headers.update(self._caption_header(pub))
        headers["Cache-Control"] = "no-cache"
        npt = parse_npt(request.headers.get("TimeSeekRange.dlna.org", ""))
        if npt is not None:
            # The TV counts time from the URL's ?t= offset (build_didl: duration = dur - t), so npt is
            # relative to it: ffmpeg starts at t + npt, the advertised range is dur - t.
            t0 = start
            start = t0 + npt
            dur = pub.plan.duration
            rest = max(dur - t0, 0.0) if dur else 0.0
            headers["TimeSeekRange.dlna.org"] = f"npt={fmt_npt(npt)}-" + (f"{fmt_npt(rest)}/{fmt_npt(rest)}" if dur else "*")
        if request.headers.get("Range") and not request.headers.get("Range", "").startswith("bytes=0-"):
            # Can't byte-seek a live transcode.
            return web.Response(status=416, headers=headers)
        resp = web.StreamResponse(status=200, headers=headers)
        if request.method == "HEAD":
            await resp.prepare(request)
            return resp
        args = (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-i", pub.src] + pub.plan.ffmpeg_out
        await self._pipe_ffmpeg(request, resp, args, pub)
        return resp

    async def _h_live(self, request: web.Request) -> web.StreamResponse:
        lv = self.lives.get(request.match_info["token"])
        if not lv or not self._peer_allowed(request, lv.allowed_ip):
            raise web.HTTPNotFound()
        headers = self._dlna_headers("live", lv.mime)
        headers["Cache-Control"] = "no-cache"
        resp = web.StreamResponse(status=200, headers=headers)
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        if request.method == "HEAD":
            return resp
        me = asyncio.current_task()
        if me is not None:
            lv.clients.add(me)
        try:
            if callable(lv.source):
                it = lv.source()
                try:
                    async for chunk in it:
                        await resp.write(chunk)
                finally:   # a cancelled/disconnected client must not leave its ffmpeg generator running
                    aclose = getattr(it, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.shield(aclose())
            else:
                q = lv.subscribe()
                try:
                    while True:
                        chunk = await q.get()
                        if chunk is None:
                            break
                        await resp.write(chunk)
                finally:
                    lv.queues.discard(q)
        except (ConnectionResetError, asyncio.CancelledError):
            log.debug("live client left")
        finally:
            lv.clients.discard(me)  # type: ignore[arg-type]
        return resp

    # ------------------------------------------------------------ ffmpeg
    def _cmd(self, args: list[str]) -> list[str]:
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"] + args + ["pipe:1"]

    async def _ffmpeg_bytes(self, args: list[str]) -> bytes:
        proc = await asyncio.create_subprocess_exec(*self._cmd(args), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        self._procs.add(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 60)
        except asyncio.TimeoutError:
            log.warning("ffmpeg timed out")
            raise web.HTTPGatewayTimeout()
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            self._procs.discard(proc)
        if proc.returncode != 0:
            log.warning("ffmpeg failed: %s", scrub_urls(err.decode(errors="replace"))[:300])
            raise web.HTTPInternalServerError()
        return out

    async def _pipe_ffmpeg(self, request: web.Request, resp: web.StreamResponse, args: list[str],
                           pub: Optional[Published] = None) -> None:
        cmd = self._cmd(args)
        log.debug("ffmpeg: %s", " ".join(redact_url(a) for a in cmd))
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        self._procs.add(proc)
        if pub is not None:
            pub.procs.add(proc)
        # Drain stderr continuously (keeps only the tail) — a full stderr pipe would block ffmpeg.
        tail: collections.deque = collections.deque(maxlen=STDERR_TAIL_LINES)
        drain = asyncio.ensure_future(_drain_lines(proc.stderr, tail))
        try:
            resp.enable_chunked_encoding()
            await resp.prepare(request)
            assert proc.stdout
            while True:
                chunk = await proc.stdout.read(CHUNK)
                if not chunk:
                    break
                await resp.write(chunk)
            # stdout closed: ffmpeg is exiting on its own — give it a moment to report its status
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), 3)
        except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
            log.debug("stream client disconnected")
        finally:
            killed = proc.returncode is None
            if killed:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(drain, 2)
            if not drain.done():
                drain.cancel()
            if not killed and proc.returncode not in (0, None) and tail:
                log.warning("ffmpeg stream error (rc=%s): %s", proc.returncode,
                            scrub_urls("\n".join(tail))[-800:])
            self._procs.discard(proc)
            if pub is not None:
                pub.procs.discard(proc)


async def _drain_lines(stream: Optional[asyncio.StreamReader], tail: collections.deque) -> None:
    if stream is None:
        return
    buf = b""
    while True:
        chunk = await stream.read(4096)   # read() not readline(): no LimitOverrunError on huge lines
        if not chunk:
            if buf:
                tail.append(buf.decode(errors="replace").rstrip())
            return
        buf += chunk.replace(b"\r", b"\n")
        *lines, buf = buf.split(b"\n")
        buf = buf[-4096:]
        for ln in lines:
            if ln.strip():
                tail.append(ln.decode(errors="replace").rstrip())


def subtitle_args(src: str, track: probe.SubtitleTrack, fmt: str, start: float = 0,
                  container_start: float = 0) -> list[str]:
    inp = track.external_path or src
    args: list[str] = []
    if not track.external_path and container_start > 0:
        # remuxed/transcoded output starts at 0, embedded cues are relative to container start_time
        args += ["-itsoffset", f"{-container_start:.3f}"]
    args += ["-i", inp]
    if track.external_path:
        args += ["-map", "0:s:0"]
    else:
        args += ["-map", f"0:{track.index}"]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    return args + ["-c:s", "webvtt" if fmt == "vtt" else "srt", "-f", "webvtt" if fmt == "vtt" else "srt"]
