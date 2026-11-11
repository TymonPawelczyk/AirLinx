"""Application orchestrator: owns AppState and implements the UI API (bridge methods).

All public coroutine methods run on the background asyncio loop (runtime.py).
Core modules are imported lazily and composed only through the interfaces in
docs/ARCHITECTURE.md; tests inject fakes via `Factories`.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import unquote, urlparse

from .core.models import (
    AirlinxError,
    AppState,
    Device,
    MediaItem,
    MirrorOptions,
    MirrorStatus,
    PlaybackState,
    Source,
    Stats,
    TvBusyError,
    TvNotFoundError,
    to_json,
)
from . import __version__
from .core.audio import AudioSpec, audio_label, spec_from
from .core.sources import is_wayland_session
from .core.capture import hevc_encoder_from_caps
from .settings import CODECS, Settings

log = logging.getLogger(__name__)

VERSION = __version__
STATS_MIN_INTERVAL = 0.5  # mirror.stats ≤ 2/s

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa", ".sub"}

Listener = Callable[[str, Any], None]


# --------------------------------------------------------------------------- factories
def _default_discovery(on_change: Callable[[list[Device]], None]) -> Any:
    from .core.discovery import Discovery

    return Discovery(on_change)


async def _default_list_sources(with_thumbnails: bool = True) -> list[Source]:
    from .core.sources import list_sources

    return await list_sources(with_thumbnails=with_thumbnails)


async def _default_probe_encoders() -> dict[str, bool]:
    from .core.capture import probe_encoders

    return await probe_encoders()


def _default_mirror(protocol: str, device: Device, source: Source, opts: MirrorOptions,
                    on_stats: Callable[[Stats], None], on_state: Callable[[str, Optional[str]], None],
                    ensure_server: Optional[Callable[[], Awaitable[Any]]] = None) -> Any:
    if protocol == "cast":
        from .core.cast.mirror import MirrorSession

        return MirrorSession(device, source, opts, on_stats, on_state)
    assert ensure_server is not None
    return DlnaMirrorAdapter(device, source, opts, on_stats, on_state, ensure_server)


def encoder_from_caps(caps: dict[str, bool]) -> str:
    """H.264 encoder for core.capture.probe_encoders() results: NVENC first, then libx264."""
    if caps.get("nvenc"):
        return "h264_nvenc"
    if caps.get("x264"):
        return "libx264"
    raise AirlinxError("no_encoder", "Brak kodera H.264 (ffmpeg z NVENC lub libx264).")


class DlnaMirrorAdapter:
    """MirrorSession-shaped wrapper over core.dlna.DlnaMirror (live MPEG-TS, high latency fallback).

    Builds the live source itself: ffmpeg x11grab(region) → h264_nvenc|libx264 (+pulse audio) → mpegts.
    The encoder comes from the same probe as Cast (core.capture.probe_encoders): NVENC when it works,
    otherwise libx264. AIRLINX_DLNA_ENCODER overrides the detection.
    The audio source (core/audio.py) is fixed for the whole session: a change applies at the next start.
    """
    live_audio_switch = False

    def __init__(self, device: Device, source: Source, opts: MirrorOptions,
                 on_stats: Callable[[Stats], None], on_state: Callable[[str, Optional[str]], None],
                 ensure_server: Callable[[], Awaitable[Any]], pipeline_factory: Optional[Callable[..., Any]] = None,
                 encoder_probe: Optional[Callable[[], Awaitable[dict[str, bool]]]] = None):
        self.device, self.source, self.opts = device, source, opts
        self.on_stats, self.on_state = on_stats, on_state
        self.ensure_server = ensure_server
        self.on_notice: Optional[Callable[[str], None]] = None
        self._pipeline_factory = pipeline_factory
        self._encoder_probe = encoder_probe
        self.encoder: str = os.environ.get("AIRLINX_DLNA_ENCODER") or "h264_nvenc"   # final value set in start()
        self._pipeline: Any = None
        self._audio_sources: list[str] = []
        self._mirror: Any = None
        self._procs: set[asyncio.subprocess.Process] = set()
        self._stats_task: Optional[asyncio.Task] = None
        self._stopped = False

    def ffmpeg_cmd(self) -> list[str]:
        x, y, w, h = self.source.geometry
        w -= w % 2
        h -= h % 2
        disp = os.environ.get("DISPLAY", ":0")
        out_h = min(self.opts.max_height, h)
        out_h -= out_h % 2
        enc = self.encoder
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab", "-framerate", str(min(self.opts.fps, 60)),
               "-video_size", f"{w}x{h}", "-i", f"{disp}+{x},{y}"]
        audio = bool(self.opts.audio and self._audio_sources)
        if audio:
            from .core.capture import pulse_audio_args

            args, amap = pulse_audio_args(self._audio_sources, first_index=1)
            cmd += args + ["-map", "0:v"] + amap
        cmd += ["-vf", f"scale=-2:{out_h},format=yuv420p", "-c:v", enc]
        if enc == "h264_nvenc":
            cmd += ["-preset", "p2", "-tune", "ll", "-rc", "cbr", "-bf", "0"]
        else:
            cmd += ["-preset", "ultrafast", "-tune", "zerolatency"]
        br = self.opts.bitrate
        cmd += ["-b:v", str(br), "-maxrate", str(br), "-bufsize", str(br // 2), "-g", str(self.opts.fps)]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        cmd += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
        return cmd

    async def _live(self) -> Any:
        proc = await asyncio.create_subprocess_exec(*self.ffmpeg_cmd(), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        self._procs.add(proc)
        try:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(1 << 16)
                if not chunk:
                    break
                yield chunk
        finally:
            self._procs.discard(proc)
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    def _on_playback(self, st: PlaybackState) -> None:
        mapping = {"loading": "waiting_tv", "buffering": "waiting_tv", "playing": "live",
                   "paused": "live", "ended": "stopped", "error": "error"}
        state = mapping.get(st.state)
        if state in ("stopped", "error") and not self._stopped:
            # the controller drops us without stop(): release live URL, stats, player and audio routing
            asyncio.ensure_future(self.stop(notify=False))
        if state:
            self.on_state(state, st.error)

    async def _open_audio(self) -> None:
        if not self.opts.audio:
            return
        from .core.audio import spec_from

        factory = self._pipeline_factory
        if factory is None:
            from .core.audio import AudioPipeline
            from .core.capture import default_audio_monitor

            def factory(spec: Any, **kw: Any) -> Any:
                return AudioPipeline(spec, monitor=default_audio_monitor, **kw)

        self._pipeline = factory(spec_from(self.opts), on_notice=lambda m: self.on_notice and self.on_notice(m))
        self._audio_sources = list(await self._pipeline.open())

    async def _close_audio(self) -> None:
        pipe, self._pipeline = self._pipeline, None
        self._audio_sources = []
        if pipe is not None:
            try:
                await asyncio.shield(pipe.close())
            except Exception as e:
                log.warning("closing audio pipeline failed: %s", e)

    async def _pick_encoder(self) -> str:
        forced = os.environ.get("AIRLINX_DLNA_ENCODER")
        if forced:
            return forced
        probe = self._encoder_probe
        if probe is None:
            from .core.capture import probe_encoders as probe
        return encoder_from_caps(await probe())

    async def start(self, force: bool = False) -> None:
        from .core.dlna import DlnaMirror

        self.encoder = await self._pick_encoder()
        server = await self.ensure_server()
        self.on_state("connecting", None)
        try:
            await self._open_audio()
            self._mirror = DlnaMirror(self.device, server, self._live, title="AirLinx — ekran",
                                      on_state=self._on_playback)
            await self._mirror.start(force=force)
        except BaseException:
            await self.stop(notify=False)
            raise
        self._stats_task = asyncio.get_running_loop().create_task(self._stats_loop())

    async def _stats_loop(self) -> None:
        _x, _y, w, h = self.source.geometry
        out_h = min(self.opts.max_height, h)
        while True:
            await asyncio.sleep(1.0)
            self.on_stats(Stats(latency_ms=None, fps=float(self.opts.fps), bitrate=float(self.opts.bitrate),
                                width=int(w * out_h / max(h, 1)), height=out_h, rtt_ms=None, loss=0.0,
                                protocol="dlna-ts", encoder=self.encoder,
                                stages={}))

    async def stop(self, notify: bool = True) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._stats_task:
            self._stats_task.cancel()
        for p in list(self._procs):
            if p.returncode is None:
                p.kill()
        try:
            if self._mirror is not None:
                await self._mirror.stop()
        finally:
            await self._close_audio()
        if notify:
            self.on_state("stopped", None)

    async def update(self, opts: MirrorOptions) -> None:
        self.opts = opts  # applies to the next client connection (TV re-request)

    async def switch_source(self, source: Source) -> None:
        self.source = source
        for p in list(self._procs):  # TV reconnects → new ffmpeg with new region
            if p.returncode is None:
                p.kill()


def _default_player(protocol: str, device: Device) -> Any:
    if protocol == "cast":
        from .core.cast.media import CastMediaPlayer

        return CastMediaPlayer(device)
    from .core.dlna import DlnaPlayer

    return DlnaPlayer(device)


def _default_media_server() -> Any:
    from .core.media_server import MediaServer

    return MediaServer()


class DefaultAudioService:
    """core.audio over the real pactl (lazy import keeps the controller importable without it)."""

    async def list(self) -> dict:
        from .core.audio import list_devices

        return await list_devices()

    async def resolve(self, spec: AudioSpec) -> tuple[AudioSpec, Optional[str]]:
        from .core.audio import resolve

        return await resolve(spec)

    async def cleanup(self) -> int:
        from .core.audio import cleanup_orphans

        return await cleanup_orphans()


@dataclass
class Factories:
    discovery: Callable[[Callable[[list[Device]], None]], Any] = _default_discovery
    list_sources: Callable[..., Awaitable[list[Source]]] = _default_list_sources
    mirror: Callable[..., Any] = _default_mirror
    player: Callable[[str, Device], Any] = _default_player
    media_server: Callable[[], Any] = _default_media_server
    audio: Any = None            # DefaultAudioService-shaped (list/resolve/cleanup); None → real pactl
    encoders: Callable[[], Awaitable[dict[str, bool]]] = _default_probe_encoders   # core.capture.probe_encoders


@dataclass
class UiHooks:
    """GTK-side services; each is called from the asyncio thread and must be thread-safe."""
    pick_files: Optional[Callable[[], Awaitable[list[str]]]] = None
    hide_window: Optional[Callable[[], None]] = None
    quit: Optional[Callable[[], None]] = None
    set_autostart: Optional[Callable[[bool], None]] = None


# --------------------------------------------------------------------------- helpers
def _dev_cast_root() -> bool:
    """True while the developer-mode TEST Cast root CA is trusted in this process (never from settings)."""
    try:
        from .core.cast.castv2 import dev_trust_active
    except ImportError:
        return False
    return dev_trust_active()


async def _call_maybe_force(fn: Callable[..., Any], *args: Any, force: bool = False, **kw: Any) -> Any:
    """Call fn(*args, force=force) when fn accepts `force`; otherwise fn(*args)."""
    if force:
        try:
            params = inspect.signature(fn).parameters
            if "force" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
                kw["force"] = True
            else:
                owner = getattr(fn, "__self__", None)
                if owner is not None:
                    setattr(owner, "force", True)
        except (TypeError, ValueError):
            pass
    res = fn(*args, **kw)
    if inspect.isawaitable(res):
        res = await res
    return res


async def _maybe_await(res: Any) -> Any:
    if inspect.isawaitable(res):
        return await res
    return res


def media_kind(path: str) -> Optional[str]:
    ext = os.path.splitext(urlparse(path).path if "://" in path else path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    mt, _ = mimetypes.guess_type(path)
    if mt:
        for k in ("video", "audio", "image"):
            if mt.startswith(k + "/"):
                return k
    return None


def _title_for(path: str) -> str:
    if "://" in path:
        p = urlparse(path)
        name = unquote(os.path.basename(p.path)) or p.netloc
    else:
        name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    return stem if ext.lower() in VIDEO_EXT | AUDIO_EXT | IMAGE_EXT else name


def _find_sidecar_subs(path: str) -> list[str]:
    p = Path(path)
    if not p.parent.is_dir():
        return []
    out = []
    try:
        for f in sorted(p.parent.iterdir()):
            if f.suffix.lower() in SUB_EXT and f.name.startswith(p.stem):
                out.append(str(f))
    except OSError:
        pass
    return out


def _opts_from(base: MirrorOptions, patch: Optional[dict]) -> MirrorOptions:
    if not patch:
        return replace(base)
    known = {k: v for k, v in patch.items() if k in MirrorOptions.__dataclass_fields__}
    if known.get("codec", base.codec) not in CODECS:
        del known["codec"]          # an unknown codec in an RPC or old file never reaches the session
    return replace(base, **known)


# MirrorOptions.audio_* ⇄ settings "audio" section (the on/off switch stays mirror.audio)
AUDIO_FIELDS = {"audio_source": "source", "audio_mic": "mic", "audio_app": "app",
                "audio_mic_name": "mic_name", "audio_app_name": "app_name"}


def _mirror_json(opts: MirrorOptions) -> dict:
    """MirrorOptions for the settings "mirror" section (without the audio source choice)."""
    return {k: v for k, v in to_json(opts).items() if k not in AUDIO_FIELDS}


def _audio_json(opts: MirrorOptions) -> dict:
    return {sk: getattr(opts, f) for f, sk in AUDIO_FIELDS.items()}


def _with_audio(opts: MirrorOptions, audio: Optional[dict]) -> MirrorOptions:
    if not isinstance(audio, dict):
        return opts
    return replace(opts, **{f: audio.get(sk, getattr(opts, f)) for f, sk in AUDIO_FIELDS.items()})


def _with_spec(opts: MirrorOptions, spec: AudioSpec) -> MirrorOptions:
    return replace(opts, audio_source=spec.source, audio_mic=spec.mic, audio_app=spec.app,
                   audio_mic_name=spec.mic_name, audio_app_name=spec.app_name)


def _audio_of(opts: MirrorOptions) -> dict:
    return {f: getattr(opts, f) for f in AUDIO_FIELDS}


# --------------------------------------------------------------------------- controller
class Controller:
    def __init__(self, settings: Settings, factories: Optional[Factories] = None,
                 hooks: Optional[UiHooks] = None):
        self.settings = settings
        self.f = factories or Factories()
        self.hooks = hooks or UiHooks()
        self._listeners: list[Listener] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self.devices: list[Device] = []
        self.selected_device_id: Optional[str] = settings.get("last_device_id")
        self._selection_explicit = False
        self.discovering = False
        self.mode = "idle"
        self.mirror = MirrorStatus(options=_with_audio(_opts_from(MirrorOptions(), settings.get("mirror")),
                                                       settings.get("audio")))
        self.audio_svc: Any = self.f.audio if self.f.audio is not None else DefaultAudioService()
        self._session_opts: Optional[MirrorOptions] = None   # what the running session got (resolved audio)
        self.queue: list[MediaItem] = []
        self.playback = PlaybackState(state="idle", volume=float(settings.get("media.volume", 1.0)))
        self.media_protocol: Optional[str] = None
        self.encoder: Optional[str] = None      # "h264_nvenc" | "libx264" | "" (none) | None (not probed yet)
        self.hevc_encoder: Optional[str] = None  # "hevc_nvenc" | "libx265" | "" (none) | None (not probed yet)
        self._encoder_task: Optional[asyncio.Task] = None
        self.wayland = is_wayland_session(os.environ)
        if self.wayland:
            log.warning("Wayland session: x11grab will likely capture a black screen (use an Xorg session)")

        self._discovery: Any = None
        self._sources: list[Source] = []
        self._session: Any = None
        self._session_token = 0
        self._player: Any = None
        self._player_key: Optional[tuple[str, str]] = None
        self._server: Any = None
        self._last_stats_emit = 0.0
        self._advancing = False
        self._stopping_media = False
        self._op_lock: Optional[asyncio.Lock] = None
        self._start_task: Optional[asyncio.Task] = None
        self._quiet_token: Optional[int] = None
        self._published_url: Optional[str] = None
        self._closed = False

    # ------------------------------------------------------------------ events
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def emit(self, event: str, payload: Any = None) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, payload)
            except Exception:
                log.exception("listener failed for %s", event)

    def toast(self, message: str) -> None:
        self.emit("toast", {"message": message})

    def error(self, code: str, message: str, **extra: Any) -> None:
        self.emit("error", {"code": code, "message": message, **extra})

    def emit_state(self) -> None:
        self.emit("state", self.state())

    def state(self) -> AppState:
        return AppState(
            devices=list(self.devices),
            selected_device_id=self.selected_device_id,
            discovering=self.discovering,
            mode=self.mode,
            protocol=self.settings.get("protocol", "auto"),
            mirror=self.mirror,
            queue=list(self.queue),
            playback=self.playback,
            media_protocol=self.media_protocol,
            settings=self.settings.as_dict(),
            version=VERSION,
            encoder=self.encoder,
            wayland=self.wayland,
            dev_cast_root=_dev_cast_root(),
            hevc_encoder=self.hevc_encoder,
        )

    def _on_loop(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run fn on the controller loop (callbacks from core may come from any thread)."""
        loop = self._loop
        if loop is None:
            fn(*args)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            fn(*args)
        else:
            loop.call_soon_threadsafe(fn, *args)

    @property
    def lock(self) -> asyncio.Lock:
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        return self._op_lock

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:  # a crashed previous run may have left an app routed into our null sink (= muted)
            n = await self.audio_svc.cleanup()
            if n:
                log.warning("restored audio routing left by a previous run (%d module(s))", n)
        except Exception as e:
            log.warning("audio cleanup failed: %s", e)
        self._encoder_task = self._loop.create_task(self._detect_encoder())
        try:
            self._discovery = self.f.discovery(lambda devs: self._on_loop(self._on_devices, devs))
        except Exception as e:
            log.warning("discovery unavailable: %s", e)
            self._discovery = None
        if self._discovery is not None:
            self.discovering = True
            try:
                await self._discovery.start()
            except Exception:
                log.exception("discovery.start failed")
            for ip in self.settings.get("manual_devices", []) or []:
                try:
                    await self._discovery.add_manual(ip)
                except Exception as e:
                    log.warning("manual device %s failed: %s", ip, e)
            self._on_devices(list(getattr(self._discovery, "devices", []) or []))
            self._loop.call_later(6.0, self._discovery_settled)
        self.emit_state()

    async def _detect_encoder(self) -> None:
        """Probe ffmpeg once in the background so Settings can name the encoder mirroring will use."""
        try:
            caps = await self.f.encoders()
        except Exception as e:
            log.warning("encoder probe failed: %s", e)
            return
        try:
            self.encoder = encoder_from_caps(caps)
        except AirlinxError:
            self.encoder = ""
        hevc = hevc_encoder_from_caps(caps)
        self.hevc_encoder = hevc[0] if hevc else ""
        self.emit_state()

    def _discovery_settled(self) -> None:
        if self.discovering:
            self.discovering = False
            self.emit_state()

    SHUTDOWN_STOP_TIMEOUT = 8.0     # STOP on TV (mirror + media, in parallel)
    SHUTDOWN_SERVICE_TIMEOUT = 3.0  # media server + discovery
    SHUTDOWN_TOTAL = 5.0 + SHUTDOWN_STOP_TIMEOUT + SHUTDOWN_SERVICE_TIMEOUT  # incl. cancelling a pending start

    async def shutdown(self) -> None:
        """Stop everything (STOP on TV first) — used by app.quit. Bounded by SHUTDOWN_TOTAL seconds."""
        if self._closed:
            return
        self._closed = True
        if self._encoder_task is not None and not self._encoder_task.done():
            self._encoder_task.cancel()        # ffmpeg probe still running: not worth waiting for
        await self._cancel_start()
        stops = []
        if self._session is not None:
            stops.append(self._stop_session())
        if self._player is not None:
            stops.append(self._stop_player())
        if stops:
            try:
                await asyncio.wait_for(asyncio.gather(*stops, return_exceptions=True), self.SHUTDOWN_STOP_TIMEOUT)
            except Exception as e:
                log.warning("shutdown: stopping sessions failed: %s", e)
        services = [obj for obj in (self._server, self._discovery) if obj is not None]
        if services:
            try:
                res = await asyncio.wait_for(asyncio.gather(*(_maybe_await(o.stop()) for o in services),
                                                            return_exceptions=True), self.SHUTDOWN_SERVICE_TIMEOUT)
                for o, r in zip(services, res):
                    if isinstance(r, BaseException):
                        log.warning("shutdown: %s.stop failed: %s", type(o).__name__, r)
            except Exception as e:
                log.warning("shutdown: stopping services failed: %s", e)

    # ------------------------------------------------------------------ dispatch
    METHODS = {
        "app.getState": "get_state",
        "app.quit": "app_quit",
        "devices.list": "devices_list",
        "devices.rescan": "devices_rescan",
        "devices.addManual": "devices_add_manual",
        "devices.select": "devices_select",
        "devices.trustAgain": "devices_trust_again",
        "sources.list": "sources_list",
        "mirror.start": "mirror_start",
        "mirror.stop": "mirror_stop",
        "mirror.update": "mirror_update",
        "mirror.switchSource": "mirror_switch_source",
        "media.add": "media_add",
        "media.pickFiles": "media_pick_files",
        "media.remove": "media_remove",
        "media.reorder": "media_reorder",
        "media.play": "media_play",
        "media.pause": "media_pause",
        "media.resume": "media_resume",
        "media.seek": "media_seek",
        "media.volume": "media_volume",
        "media.next": "media_next",
        "media.prev": "media_prev",
        "media.stop": "media_stop",
        "media.subtitles": "media_subtitles",
        "audio.list": "audio_list",
        "settings.get": "settings_get",
        "settings.set": "settings_set",
        "window.minimizeToTray": "window_minimize",
    }

    async def dispatch(self, method: str, params: Optional[dict] = None) -> Any:
        name = self.METHODS.get(method)
        if name is None:
            raise AirlinxError("unknown_method", f"Nieznana metoda: {method}")
        params = params or {}
        if not isinstance(params, dict):
            raise AirlinxError("bad_params", "Nieprawidłowe parametry wywołania.")
        return await _maybe_await(getattr(self, name)(**params))

    # ------------------------------------------------------------------ app
    async def get_state(self) -> AppState:
        return self.state()

    async def app_quit(self) -> dict:
        if self.hooks.quit:
            self.hooks.quit()
        return {"ok": True}

    async def window_minimize(self) -> dict:
        if self.hooks.hide_window:
            self.hooks.hide_window()
        return {"ok": True}

    # ------------------------------------------------------------------ devices
    def _on_devices(self, devices: list[Device]) -> None:
        self.devices = list(devices)
        if devices:
            self.discovering = False
        ids = {d.id for d in self.devices}
        last = self.settings.get("last_device_id")
        if self.selected_device_id not in ids or not self._selection_explicit:
            if last in ids:
                self.selected_device_id = last
            elif self.selected_device_id not in ids and len(self.devices) == 1 and not last:
                self.selected_device_id = self.devices[0].id
        self.emit("devices", list(self.devices))
        self.emit_state()

    def device(self) -> Device:
        dev = next((d for d in self.devices if d.id == self.selected_device_id), None)
        if dev is None:
            if self.selected_device_id:
                raise TvNotFoundError("telewizor jest offline")
            raise AirlinxError("no_device", "Najpierw wybierz telewizor.")
        return dev

    async def devices_list(self) -> list[Device]:
        return list(self.devices)

    async def devices_rescan(self) -> dict:
        if self._discovery is None:
            raise AirlinxError("discovery_unavailable", "Wyszukiwanie urządzeń jest niedostępne.")
        self.discovering = True
        self.emit_state()
        try:
            await self._discovery.rescan()
        finally:
            self._on_devices(list(getattr(self._discovery, "devices", []) or []))
            if self._loop:
                self._loop.call_later(4.0, self._discovery_settled)
        return {"ok": True}

    async def devices_add_manual(self, ip: str) -> Device:
        ip = (ip or "").strip()
        if not ip:
            raise AirlinxError("bad_params", "Podaj adres IP telewizora.")
        if self._discovery is None:
            raise AirlinxError("discovery_unavailable", "Wyszukiwanie urządzeń jest niedostępne.")
        dev = await self._discovery.add_manual(ip)
        if dev is None:
            raise TvNotFoundError(ip)
        manual = self.settings.get("manual_devices", []) or []
        if ip not in manual:
            self.settings.set("manual_devices", manual + [ip])
        if all(d.id != dev.id for d in self.devices):
            self.devices.append(dev)
        await self.devices_select(dev.id)
        self.emit("devices", list(self.devices))
        return dev

    async def devices_select(self, id: str) -> AppState:
        if all(d.id != id for d in self.devices):
            raise TvNotFoundError()
        self.selected_device_id = id
        self._selection_explicit = True
        self.settings.set("last_device_id", id)
        self.emit_state()
        return self.state()

    async def devices_trust_again(self, id: str) -> dict:
        """Forget the pinned TLS certificate of a TV (after tv_cert_changed); next connect re-pins it."""
        try:
            from .core.cast.castv2 import forget_tv_cert
        except ImportError as e:
            raise AirlinxError("unavailable", "Ta funkcja jest niedostępna.") from e
        keys = [id]
        dev = next((d for d in self.devices if d.id == id), None)
        if dev is not None and dev.ip:
            keys.append(dev.ip)
        removed = False
        for k in keys:
            removed = bool(forget_tv_cert(k)) or removed
        name = dev.name if dev else "telewizor"
        self.toast(f"Zaufano ponownie: {name}. Połącz się jeszcze raz." if removed
                   else f"{name}: brak zapamiętanego certyfikatu.")
        return {"ok": True, "removed": removed}

    # ------------------------------------------------------------------ sources
    async def sources_list(self, withThumbnails: bool = True) -> list[Source]:
        try:
            self._sources = await self.f.list_sources(with_thumbnails=withThumbnails)
        except ImportError as e:
            raise AirlinxError("unavailable", "Lista źródeł jest niedostępna.") from e
        return list(self._sources)

    async def _source(self, source_id: Optional[str]) -> Source:
        if not self._sources or (source_id and all(s.id != source_id for s in self._sources)):
            await self.sources_list(withThumbnails=False)
        if source_id is None:
            src = next((s for s in self._sources if s.kind == "monitor"), None)
        else:
            src = next((s for s in self._sources if s.id == source_id), None)
        if src is None:
            raise AirlinxError("source_not_found", "Nie znaleziono wybranego ekranu lub okna.")
        return src

    # ------------------------------------------------------------------ mirror
    CAST_UNVERIFIED_PL = ("Nie udało się potwierdzić tożsamości telewizora przez Google Cast. "
                          "Wysłać przez DLNA bez weryfikacji?")

    def _protocols(self, dev: Device, purpose: str, protocol: Optional[str] = None) -> list[str]:
        if protocol is not None:   # explicit choice (retry after cast_unverified)
            if protocol not in ("cast", "dlna"):
                raise AirlinxError("bad_params", "Nieznany protokół.")
            return [protocol]
        pref = self.settings.get("protocol", "auto")
        cast_cap = "cast_mirror" if purpose == "mirror" else "cast_media"
        has_cast = cast_cap in dev.caps or (not dev.caps and dev.cast_port is not None)
        has_dlna = "dlna" in dev.caps or dev.dlna_location is not None
        if pref == "cast":
            return ["cast"]
        if pref == "dlna":
            return ["dlna"]
        out = []
        if has_cast:
            out.append("cast")
        if has_dlna:
            out.append("dlna")
        return out or ["cast"]

    @staticmethod
    def _cast_pinned(dev: Device) -> bool:
        """The TV's Cast identity is pinned (known_tvs.json, keys as in devices.trustAgain): DLNA has no
        verification at all, so a failing Cast must not silently downgrade to DLNA on the same host."""
        try:  # fail closed: an unreadable pin store must not switch the protection off
            from .core.cast.castv2 import known_tvs_path, load_pins_strict

            pins = load_pins_strict(known_tvs_path())
        except (ImportError, OSError, ValueError) as e:
            log.warning("cannot check pinned Cast identities (%s); treating %s as pinned", e, dev.ip)
            return True
        return any(k and k in pins for k in (dev.id, dev.ip))

    def _dlna_needs_consent(self, dev: Device, protocols: list[str], protocol: Optional[str]) -> bool:
        """Auto mode picked DLNA only (no Cast caps: e.g. spoofed or missing mDNS) for a TV whose Cast
        identity is pinned: ask first. An explicit `protocol` or the "DLNA" setting is the user's consent."""
        return (protocol is None and self.settings.get("protocol", "auto") == "auto"
                and "cast" not in protocols and self._cast_pinned(dev))

    def _cast_unverified(self, method: str, params: dict) -> dict:
        msg = self.CAST_UNVERIFIED_PL
        self.error("cast_unverified", msg, retry={"method": method, "params": dict(params, protocol="dlna")})
        return {"ok": False, "code": "cast_unverified", "message": msg}

    def _set_mirror(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.mirror, k, v)
        self.emit("mirror.state", self.mirror)
        self.emit_state()

    def _make_session_callbacks(self, token: int) -> tuple[Callable[[Stats], None], Callable[[str, Optional[str]], None]]:
        def on_stats(stats: Stats) -> None:
            self._on_loop(self._handle_stats, token, stats)

        def on_state(state: str, detail: Optional[str] = None) -> None:
            self._on_loop(self._handle_mirror_state, token, state, detail)

        return on_stats, on_state

    def _handle_stats(self, token: int, stats: Stats) -> None:
        if token != self._session_token:
            return
        self.mirror.stats = stats
        now = time.monotonic()
        if now - self._last_stats_emit >= STATS_MIN_INTERVAL:
            self._last_stats_emit = now
            self.emit("mirror.stats", stats)

    def _handle_mirror_state(self, token: int, state: str, detail: Optional[str]) -> None:
        if token != self._session_token:
            return
        if state == "live" and self.mirror.started_at is None:
            self.mirror.started_at = time.time()
        if state in ("stopped", "error"):
            self._session = None
            self.mode = "idle" if self.mode == "mirror" else self.mode
            self.mirror.stats = None
            self.mirror.started_at = None
            self.mirror.audio = None
        if state == "error" and token == self._quiet_token:
            log.info("mirror attempt error suppressed (fallback pending): %s", detail)
            return
        self._set_mirror(state=state, detail=detail)
        if state == "error":
            self.error("mirror_failed", detail or "Udostępnianie ekranu zostało przerwane.")

    STOP_FALLBACK_CODES = {"permission_pending", "tv_denied", "tv_cert_changed", "tv_auth_failed"}

    async def mirror_start(self, sourceId: Optional[str] = None, options: Optional[dict] = None,
                           force: bool = False, protocol: Optional[str] = None) -> dict:
        """Start mirroring. The (possibly long) TV handshake runs in a cancellable task that
        mirror.stop / shutdown cancel immediately, without waiting for the operation lock."""
        await self._cancel_start()
        task = asyncio.get_running_loop().create_task(self._mirror_start_impl(sourceId, options, force, protocol))
        self._start_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                task.cancel()  # the caller itself was cancelled → abort the start too
                raise
            return {"ok": False, "code": "cancelled", "message": "Anulowano."}
        finally:
            if self._start_task is task:
                self._start_task = None

    async def _cancel_start(self, timeout: float = 5.0) -> bool:
        """Cancel a pending mirror start; waits (bounded) so the session can send STOP. True if cancelled."""
        task = self._start_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.wait({task}, timeout=timeout)
        return True

    async def _mirror_start_impl(self, sourceId: Optional[str], options: Optional[dict], force: bool,
                                 protocol: Optional[str] = None) -> dict:
        async with self.lock:
            dev = self.device()
            protocols = self._protocols(dev, "mirror", protocol)
            src = await self._source(sourceId)
            base = _with_audio(_opts_from(self.mirror.options, self.settings.get("mirror")), self.settings.get("audio"))
            opts = _opts_from(base, options)
            if self._dlna_needs_consent(dev, protocols, protocol):
                params: dict[str, Any] = {"sourceId": src.id, "options": to_json(opts)}
                if force:
                    params["force"] = True
                return self._cast_unverified("mirror.start", params)
            if self._player is not None:
                await self._stop_player()
            if self._session is not None:
                await self._stop_session()
        session_opts, audio_info = await self._resolve_audio(opts)
        last_exc: Optional[BaseException] = None
        for i, proto in enumerate(protocols):
            more = i + 1 < len(protocols)
            self._session_token += 1
            token = self._session_token
            self._quiet_token = token if more else None  # a failed attempt with fallback left stays silent
            on_stats, on_state = self._make_session_callbacks(token)
            self.mode = "mirror"
            self._set_mirror(state="connecting", detail=None, device_id=dev.id, source_id=src.id,
                             protocol=proto, options=opts, stats=None, started_at=None, audio=audio_info)
            session = None
            try:
                session = self.f.mirror(proto, dev, src, session_opts, on_stats, on_state, self._ensure_server)
                self._session = session
                self._session_opts = session_opts
                self._attach_notice(session, token)
                await _call_maybe_force(session.start, force=force)
            except asyncio.CancelledError:
                # session.start() sends STOP to the TV itself when cancelled
                if self._session is session:
                    self._session = None
                self._session_token += 1
                self._quiet_token = None
                if self.mode == "mirror":
                    self.mode = "idle"
                self._set_mirror(state="idle", detail="Anulowano", stats=None, started_at=None)
                raise
            except TvBusyError as e:
                self._session = None
                self._session_token += 1
                self.mode = "idle"
                self._set_mirror(state="idle", detail=None)
                self.error("tv_busy", e.message_pl, app=e.app_name,
                           retry={"method": "mirror.start",
                                  "params": {"sourceId": src.id, "options": to_json(opts), "force": True}})
                return {"ok": False, "code": "tv_busy", "message": e.message_pl}
            except Exception as e:
                self._session = None
                self._session_token += 1  # late on_state("error") from this attempt is ignored
                last_exc = e
                log.warning("mirror via %s failed: %s", proto, e)
                if isinstance(e, AirlinxError) and e.code in self.STOP_FALLBACK_CODES:
                    break
                if more and proto == "cast" and self._cast_pinned(dev):
                    self._quiet_token = None
                    self.mode = "idle"
                    self._set_mirror(state="idle", detail=None)
                    params: dict[str, Any] = {"sourceId": src.id, "options": to_json(opts)}
                    if force:
                        params["force"] = True
                    return self._cast_unverified("mirror.start", params)
                if more:
                    self.toast("Google Cast nie odpowiada — próbuję DLNA.")
                    continue
                break
            else:
                self._quiet_token = None
                self.settings.update({"last_source_id": src.id, "mirror": _mirror_json(opts),
                                      "audio": _audio_json(opts)})
                if self.mirror.state == "connecting":
                    self._set_mirror(state="waiting_tv")
                return {"ok": True, "protocol": proto}
        self._quiet_token = None
        self.mode = "idle"
        self._set_mirror(state="error", detail=getattr(last_exc, "message_pl", None) or (str(last_exc) if last_exc else None))
        self._report_cert_changed(last_exc, dev)
        if isinstance(last_exc, AirlinxError):
            raise last_exc
        raise AirlinxError("mirror_failed", "Nie udało się rozpocząć udostępniania ekranu.") from last_exc

    def _report_cert_changed(self, exc: Optional[BaseException], dev: Device) -> None:
        if isinstance(exc, AirlinxError) and exc.code == "tv_cert_changed":
            key = getattr(exc, "key", "") or dev.id
            self.error("tv_cert_changed", exc.message_pl, device_id=dev.id, device_name=dev.name,
                       retry={"method": "devices.trustAgain", "params": {"id": key}})

    async def _stop_session(self) -> None:
        session, self._session = self._session, None
        self._session_token += 1
        if session is not None:
            try:
                await session.stop()
            except Exception as e:
                log.warning("mirror stop failed: %s", e)
        if self.mode == "mirror":
            self.mode = "idle"
        self.mirror.stats = None
        self.mirror.started_at = None
        self.mirror.audio = None
        self._session_opts = None
        self._set_mirror(state="idle", detail=None)

    async def mirror_stop(self) -> dict:
        cancelled = await self._cancel_start()
        async with self.lock:
            await self._stop_session()
        if cancelled:
            self._set_mirror(detail="Anulowano")
        return {"ok": True, "cancelled": cancelled}

    async def mirror_update(self, options: dict) -> MirrorStatus:
        old = self.mirror.options
        opts = _opts_from(old, options)
        self.mirror.options = opts
        patch: dict[str, Any] = {"mirror": _mirror_json(opts)}
        if any(k in AUDIO_FIELDS for k in options or {}):
            patch["audio"] = _audio_json(opts)
        self.settings.update(patch)
        await self._apply_to_session(old, opts)
        self._set_mirror()
        return self.mirror

    # ------------------------------------------------------------------ audio source
    async def audio_list(self) -> dict:
        """Microphones and applications playing right now (settings → Źródło dźwięku)."""
        try:
            return await self.audio_svc.list()
        except Exception as e:
            log.warning("audio.list failed: %s", e)
            return {"mics": [], "apps": [], "default_mic": None}

    async def _resolve_audio(self, opts: MirrorOptions) -> tuple[MirrorOptions, Optional[dict]]:
        """Check the chosen mic/app exists; fall back to system sound with a toast (never fails)."""
        if not opts.audio:
            return opts, None
        spec = spec_from(opts)
        resolved, note = spec, None
        try:
            resolved, note = await self.audio_svc.resolve(spec)
        except Exception as e:
            log.warning("audio source check failed: %s", e)
        if note:
            self.toast(note)
        info = {"source": resolved.source, "requested": spec.source, "label": audio_label(resolved), "note": note}
        return _with_spec(opts, resolved), info

    async def _apply_to_session(self, old: MirrorOptions, opts: MirrorOptions) -> None:
        """Push new options to the running session. A new audio source is resolved and switched live
        when the session supports it (Cast), otherwise kept for the next start (DLNA)."""
        session = self._session
        if session is None:
            return
        prev = self._session_opts or old
        changed = opts.audio and (not old.audio or spec_from(old).key() != spec_from(opts).key())
        if not opts.audio:
            eff, info = opts, None
        elif not changed:
            eff, info = replace(opts, **_audio_of(prev)), self.mirror.audio
        elif old.audio and not getattr(session, "live_audio_switch", False):
            eff = replace(opts, **_audio_of(prev))
            info = dict(self.mirror.audio or {}, pending=True)
            self.toast("Nowe źródło dźwięku zadziała przy następnym udostępnianiu.")
        else:
            eff, info = await self._resolve_audio(opts)
        self._session_opts = eff
        self.mirror.audio = info
        await session.update(eff)

    def _attach_notice(self, session: Any, token: int) -> None:
        def on_notice(msg: str) -> None:
            self._on_loop(self._handle_notice, token, msg)

        try:
            session.on_notice = on_notice
        except AttributeError:
            pass

    def _handle_notice(self, token: int, msg: str) -> None:
        if token != self._session_token:
            return
        if self.mirror.audio is not None:
            self.mirror.audio = dict(self.mirror.audio, note=msg)
            self._set_mirror()
        self.toast(msg)

    async def mirror_switch_source(self, sourceId: str) -> MirrorStatus:
        src = await self._source(sourceId)
        if self._session is not None:
            await self._session.switch_source(src)
        self.settings.set("last_source_id", src.id)
        self._set_mirror(source_id=src.id)
        return self.mirror

    async def mirror_toggle(self) -> dict:
        """Tray action: stop when mirroring, otherwise start with last source."""
        if self._session is not None or self.mirror.state in ("connecting", "waiting_tv", "live", "reconnecting"):
            return await self.mirror_stop()
        last = self.settings.get("last_source_id")
        try:
            return await self.mirror_start(sourceId=last)
        except AirlinxError as e:
            if e.code == "source_not_found" and last:
                return await self.mirror_start(sourceId=None)
            raise

    # ------------------------------------------------------------------ media queue
    def _emit_queue(self) -> None:
        self.emit("media.queue", list(self.queue))
        self.emit_state()

    def _item(self, id: str) -> MediaItem:
        it = next((q for q in self.queue if q.id == id), None)
        if it is None:
            raise AirlinxError("item_not_found", "Nie ma takiej pozycji w kolejce.")
        return it

    def _expand(self, path: str) -> list[str]:
        if os.path.isdir(path):
            out = []
            for f in sorted(os.listdir(path)):
                full = os.path.join(path, f)
                if os.path.isfile(full) and media_kind(full):
                    out.append(full)
            return out
        return [path]

    async def media_add(self, paths: Optional[list[str]] = None, urls: Optional[list[str]] = None) -> list[MediaItem]:
        added: list[MediaItem] = []
        skipped = 0
        for raw in list(paths or []) + list(urls or []):
            if not isinstance(raw, str) or not raw:
                continue
            if raw.startswith("file://"):
                raw = unquote(urlparse(raw).path)
            is_url = "://" in raw
            for p in ([raw] if is_url else self._expand(raw)):
                if not is_url and not os.path.isfile(p):
                    skipped += 1
                    continue
                kind = media_kind(p) or ("video" if is_url else None)
                if kind is None:
                    skipped += 1
                    continue
                item = MediaItem(id=uuid.uuid4().hex[:12], path_or_url=p, title=_title_for(p),
                                 duration=None, kind=kind,  # type: ignore[arg-type]
                                 subtitles=[] if is_url else _find_sidecar_subs(p))
                self.queue.append(item)
                added.append(item)
        if skipped:
            self.toast(f"Pominięto {skipped} {'plik' if skipped == 1 else 'pliki'} — nieobsługiwany format.")
        if added:
            self._emit_queue()
        return added

    async def media_pick_files(self) -> list[MediaItem]:
        if self.hooks.pick_files is None:
            raise AirlinxError("unavailable", "Wybór plików jest niedostępny.")
        paths = await self.hooks.pick_files()
        if not paths:
            return []
        return await self.media_add(paths=paths)

    async def media_remove(self, id: str) -> list[MediaItem]:
        self._item(id)
        if self.playback.item_id == id and self._player is not None:
            await self._stop_player()
        self.queue = [q for q in self.queue if q.id != id]
        self._emit_queue()
        return list(self.queue)

    async def media_reorder(self, ids: list[str]) -> list[MediaItem]:
        by_id = {q.id: q for q in self.queue}
        new = [by_id[i] for i in ids if i in by_id]
        seen = {q.id for q in new}
        new += [q for q in self.queue if q.id not in seen]
        self.queue = new
        self._emit_queue()
        return list(self.queue)

    # ------------------------------------------------------------------ media playback
    def _on_player_state(self, key: tuple[str, str], st: PlaybackState) -> None:
        if key != self._player_key or self._stopping_media:
            return
        if st.item_id is None:
            st = replace(st, item_id=self.playback.item_id)
        if st.volume is None:  # player cannot read the TV volume — keep the user's setting
            st = replace(st, volume=self.playback.volume)
        prev = self.playback.state
        self.playback = st
        self.emit("media.playback", st)
        if st.state == "ended" and prev != "ended":
            if self.settings.get("media.auto_advance", True) and self._next_item(1) is not None:
                if self._loop:
                    self._loop.create_task(self._auto_advance())
            else:
                self.mode = "idle" if self.mode == "media" else self.mode
                self.emit_state()
        elif st.state == "error":
            self.error("playback_failed", st.error or "Odtwarzanie nie powiodło się.")

    async def _auto_advance(self) -> None:
        if self._advancing:
            return
        self._advancing = True
        try:
            await self.media_next()
        except Exception as e:
            log.warning("auto-advance failed: %s", e)
            self.error(getattr(e, "code", "playback_failed"),
                       getattr(e, "message_pl", "Nie udało się odtworzyć następnej pozycji."))
        finally:
            self._advancing = False

    def _next_item(self, step: int) -> Optional[MediaItem]:
        if not self.queue:
            return None
        idx = next((i for i, q in enumerate(self.queue) if q.id == self.playback.item_id), None)
        if idx is None:
            return self.queue[0] if step > 0 else None
        j = idx + step
        return self.queue[j] if 0 <= j < len(self.queue) else None

    async def _ensure_server(self) -> Any:
        if self._server is None:
            try:
                server = self.f.media_server()
            except ImportError as e:
                raise AirlinxError("unavailable", "Serwer multimediów jest niedostępny.") from e
            await server.start()
            self._server = server
        return self._server

    async def _get_player(self, proto: str, dev: Device) -> Any:
        key = (proto, dev.id)
        if self._player is not None and self._player_key == key:
            return self._player
        if self._player is not None:
            await self._stop_player()
        player = self.f.player(proto, dev)
        self._player = player
        self._player_key = key
        player.on_state = lambda st, k=key: self._on_loop(self._on_player_state, k, st)
        return player

    def _unpublish(self) -> None:
        url, self._published_url = self._published_url, None
        if url and self._server is not None and hasattr(self._server, "unpublish"):
            try:
                self._server.unpublish(url)
            except Exception as e:
                log.debug("unpublish failed: %s", e)

    async def _stop_player(self) -> None:
        self._unpublish()
        player, self._player = self._player, None
        self._player_key = None
        if player is not None:
            self._stopping_media = True
            try:
                await player.stop()
            except Exception as e:
                log.warning("player stop failed: %s", e)
            finally:
                self._stopping_media = False
            if hasattr(player, "close"):
                try:
                    await player.close()
                except Exception as e:
                    log.debug("player close failed: %s", e)
        self.media_protocol = None
        if self.mode == "media":
            self.mode = "idle"
        self.playback = replace(self.playback, state="idle", position=0.0, item_id=None, error=None)
        self.emit("media.playback", self.playback)
        self.emit_state()

    async def _play_item(self, item: MediaItem, force: bool = False, start: float = 0.0,
                         protocol: Optional[str] = None) -> dict:
        await self._cancel_start()   # a pending mirror start would fall back to DLNA and fight the film
        dev = self.device()
        protocols = self._protocols(dev, "media", protocol)
        if self._dlna_needs_consent(dev, protocols, protocol):
            params: dict[str, Any] = {"id": item.id}
            if force:
                params["force"] = True
            return self._cast_unverified("media.play", params)
        if self._session is not None:
            await self._stop_session()
        server = await self._ensure_server()
        last_exc: Optional[BaseException] = None
        for i, proto in enumerate(protocols):
            try:
                self._unpublish()  # previous item (next/prev/auto-advance or failed protocol)
                player = await self._get_player(proto, dev)  # may stop+unpublish an old player first
                url, mime = await self._publish(server, item, proto, dev)
                self._published_url = url
                self.mode = "media"
                self.media_protocol = proto
                self.playback = PlaybackState(state="loading", duration=item.duration,
                                              volume=self.playback.volume, item_id=item.id)
                self.emit("media.playback", self.playback)
                self.emit_state()
                extra = self._load_extras(server, player, item, url, proto)
                await _call_maybe_force(player.load, item, url, mime, start, force=force, **extra)
                await player.play()
                return {"ok": True, "protocol": proto, "itemId": item.id}
            except TvBusyError as e:
                await self._stop_player()
                self.error("tv_busy", e.message_pl, app=e.app_name,
                           retry={"method": "media.play", "params": {"id": item.id, "force": True}})
                return {"ok": False, "code": "tv_busy", "message": e.message_pl}
            except Exception as e:
                last_exc = e
                log.warning("media via %s failed: %s", proto, e)
                await self._stop_player()
                if isinstance(e, AirlinxError) and e.code in self.STOP_FALLBACK_CODES:
                    break
                if i + 1 < len(protocols) and proto == "cast" and self._cast_pinned(dev):
                    params: dict[str, Any] = {"id": item.id}
                    if force:
                        params["force"] = True
                    return self._cast_unverified("media.play", params)
                if i + 1 < len(protocols):
                    self.toast("Google Cast nie odpowiada — próbuję DLNA.")
                    continue
        self._report_cert_changed(last_exc, dev)
        self.playback = replace(self.playback, state="error", item_id=item.id,
                                error=getattr(last_exc, "message_pl", None) or "Odtwarzanie nie powiodło się.")
        self.emit("media.playback", self.playback)
        if isinstance(last_exc, AirlinxError):
            raise last_exc
        raise AirlinxError("playback_failed", "Nie udało się odtworzyć pliku na telewizorze.") from last_exc

    @staticmethod
    async def _publish(server: Any, item: MediaItem, proto: str, dev: Device) -> tuple[str, str]:
        """publish_async (non-blocking ffprobe) with target_ip=TV IP so the LAN address is chosen per TV."""
        fn = getattr(server, "publish_async", None) or server.publish
        kw: dict[str, Any] = {}
        try:
            params = inspect.signature(fn).parameters
            if "target_ip" in params or any(p.kind == p.VAR_KEYWORD for p in params.values()):
                kw["target_ip"] = dev.ip
        except (TypeError, ValueError):
            pass
        return await _maybe_await(fn(item, proto, **kw))

    @staticmethod
    def _load_extras(server: Any, player: Any, item: MediaItem, url: str, proto: str) -> dict:
        """Optional load() kwargs (poster, subtitles) — only those the player's load accepts."""
        try:
            accepted = set(inspect.signature(player.load).parameters)
        except (TypeError, ValueError):
            return {}
        out: dict[str, Any] = {}
        try:
            if "poster_url" in accepted and hasattr(server, "poster_url"):
                out["poster_url"] = server.poster_url(url)
            if hasattr(server, "subtitle_tracks"):
                tracks = server.subtitle_tracks(url, "vtt" if proto == "cast" else "srt") or []
                chosen = None
                if item.subtitle_track:
                    base = os.path.splitext(os.path.basename(item.subtitle_track))[0].lower()
                    chosen = next((t for t in tracks if base and (base in str(t.get("label", "")).lower()
                                                                  or base in str(t.get("url", "")).lower())), None)
                    if chosen is None and item.subtitle_track in item.subtitles and tracks:
                        idx = item.subtitles.index(item.subtitle_track)
                        chosen = tracks[idx] if idx < len(tracks) else None
                if "subtitles" in accepted:
                    out["subtitles"] = tracks
                if "active_subtitle" in accepted:
                    out["active_subtitle"] = chosen.get("id") if chosen else None
                if "subtitle_url" in accepted:
                    out["subtitle_url"] = chosen.get("url") if chosen else None
        except Exception as e:
            log.debug("load extras failed: %s", e)
        return out

    async def media_play(self, id: Optional[str] = None, force: bool = False, protocol: Optional[str] = None) -> dict:
        async with self.lock:
            if id is None:
                item = self._next_item(0) or (self.queue[0] if self.queue else None)
                if item is None:
                    raise AirlinxError("queue_empty", "Kolejka jest pusta.")
            else:
                item = self._item(id)
            return await self._play_item(item, force=force, protocol=protocol)

    def _require_player(self) -> Any:
        if self._player is None:
            raise AirlinxError("not_playing", "Nic teraz nie jest odtwarzane.")
        return self._player

    async def media_pause(self) -> dict:
        await self._require_player().pause()
        return {"ok": True}

    async def media_resume(self) -> dict:
        await self._require_player().play()
        return {"ok": True}

    async def media_seek(self, seconds: float) -> dict:
        await self._require_player().seek(float(seconds))
        return {"ok": True}

    async def media_volume(self, value: float) -> dict:
        v = max(0.0, min(1.0, float(value)))
        self.playback = replace(self.playback, volume=v)
        self.settings.set("media.volume", v)
        if self._player is not None:
            await self._player.set_volume(v)
        self.emit("media.playback", self.playback)
        return {"ok": True, "volume": v}

    async def media_next(self) -> dict:
        async with self.lock:
            item = self._next_item(1)
            if item is None:
                raise AirlinxError("queue_end", "To już ostatnia pozycja w kolejce.")
            return await self._play_item(item)

    async def media_prev(self) -> dict:
        async with self.lock:
            if self.playback.position > 5 and self._player is not None:
                await self._player.seek(0)
                return {"ok": True}
            item = self._next_item(-1)
            if item is None:
                raise AirlinxError("queue_start", "To pierwsza pozycja w kolejce.")
            return await self._play_item(item)

    async def media_stop(self) -> dict:
        async with self.lock:
            await self._stop_player()
        return {"ok": True}

    async def media_subtitles(self, id: str, track: Optional[Any] = None) -> MediaItem:
        item = self._item(id)
        if isinstance(track, int):
            track = item.subtitles[track] if 0 <= track < len(item.subtitles) else None
        if isinstance(track, str) and track and track not in item.subtitles:
            item.subtitles.append(track)
        item.subtitle_track = track or None
        self._emit_queue()
        if self.playback.item_id == id and self._player is not None:
            player = self._player
            if hasattr(player, "set_subtitle") and self.media_protocol == "cast" and self._server is not None:
                # Cast: switch the text track in place (tracks were sent with load()).
                pub_url = getattr(player, "_url", "") or ""
                extras = self._load_extras(self._server, player, item, pub_url, "cast")
                await player.set_subtitle(extras.get("active_subtitle"))
            else:  # DLNA: subtitle URL is part of the load → reload at the current position
                pos = self.playback.position
                async with self.lock:
                    await self._play_item(item, start=pos)
        return item

    # ------------------------------------------------------------------ settings
    async def settings_get(self) -> dict:
        return self.settings.as_dict()

    async def settings_set(self, patch: dict) -> dict:
        before = self.settings.get("autostart")
        data = self.settings.update(patch)
        if "mirror" in patch or "audio" in patch:
            old = self.mirror.options
            self.mirror.options = _with_audio(_opts_from(old, data.get("mirror")), data.get("audio"))
            await self._apply_to_session(old, self.mirror.options)
            self.emit("mirror.state", self.mirror)
        if data.get("autostart") != before and self.hooks.set_autostart:
            try:
                self.hooks.set_autostart(bool(data.get("autostart")))
            except Exception as e:
                log.warning("autostart toggle failed: %s", e)
        self.emit_state()
        return data
