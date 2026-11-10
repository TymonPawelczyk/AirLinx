"""CastMediaPlayer — media playback via the Default Media Receiver (CC1AD845) using pychromecast.

pychromecast is thread based; every blocking call goes through asyncio.to_thread and status
callbacks are marshalled back to the event loop with call_soon_threadsafe.

Seeking: direct files (byte-range) use Cast SEEK; streamed remux/transcode URLs ("/s/" in the
MediaServer layout) are reloaded with `?t=<seconds>` and positions are reported with that offset.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid as uuidlib
from typing import Any, Callable, Optional

from ..media_server import is_stream_url, scrub_urls, start_of, with_start
from ..models import AirlinxError, Device, MediaItem, PlaybackState, TvBusyError, TvNotFoundError

log = logging.getLogger(__name__)

DMR_APP_ID = "CC1AD845"
BACKDROP_APP_ID = "E8C28D3C"
IDLE_APPS = {None, "", BACKDROP_APP_ID, DMR_APP_ID}

_STATE_MAP = {"PLAYING": "playing", "PAUSED": "paused", "BUFFERING": "buffering", "LOADING": "loading"}


def map_media_status(player_state: Optional[str], idle_reason: Optional[str]) -> str:
    if player_state in _STATE_MAP:
        return _STATE_MAP[player_state]
    if player_state == "IDLE":
        if idle_reason == "FINISHED":
            return "ended"
        if idle_reason == "ERROR":
            return "error"
        return "idle"
    return "idle"


def build_load_message(item: MediaItem, url: str, mime: str, start: float = 0, *,
                       poster_url: Optional[str] = None, subtitles: Optional[list[dict]] = None,
                       active_subtitle: Optional[int] = None, duration: Optional[float] = None) -> dict:
    """Cast LOAD payload (urn:x-cast:com.google.cast.media)."""
    kind = item.kind
    stream_type = "BUFFERED"
    metadata: dict[str, Any] = {"metadataType": 4 if kind == "image" else (3 if kind == "audio" else 0),
                                "title": item.title}
    if poster_url:
        metadata["images"] = [{"url": poster_url}]
    media: dict[str, Any] = {"contentId": url, "contentUrl": url, "streamType": stream_type,
                             "contentType": mime, "metadata": metadata}
    dur = duration if duration is not None else item.duration
    if dur and is_stream_url(url):
        media["duration"] = max(dur - start_of(url), 0)
    msg: dict[str, Any] = {"type": "LOAD", "media": media, "autoplay": True, "customData": {}}
    if subtitles:
        media["tracks"] = [{
            "trackId": s["id"], "trackContentId": s["url"], "trackContentType": s.get("mime", "text/vtt"),
            "type": "TEXT", "subtype": "SUBTITLES", "name": s.get("label") or f"Napisy {s['id']}",
            "language": s.get("language") or "und",
        } for s in subtitles]
        media["textTrackStyle"] = {"backgroundColor": "#00000000", "edgeType": "OUTLINE",
                                   "edgeColor": "#000000FF", "foregroundColor": "#FFFFFFFF", "fontScale": 1.0}
        if active_subtitle is not None:
            msg["activeTrackIds"] = [active_subtitle]
    if start > 0 and not is_stream_url(url):
        msg["currentTime"] = start
    return msg


class CastMediaPlayer:
    def __init__(self, device: Device, on_state: Optional[Callable[[PlaybackState], None]] = None, *,
                 cast_factory: Optional[Callable[[Device], Any]] = None, connect_timeout: float = 10.0):
        self.device = device
        self.on_state: Callable[[PlaybackState], None] = on_state or (lambda s: None)
        self._factory = cast_factory or _default_factory
        self._timeout = connect_timeout
        self._cast: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._item: Optional[MediaItem] = None
        self._url: str = ""
        self._mime: str = ""
        self._offset: float = 0.0
        self._load_kw: dict = {}
        self._ticker: Optional[asyncio.Task] = None
        self.state = PlaybackState("idle")

    # ---------------------------------------------------------- connection
    async def connect(self) -> None:
        if self._cast is not None:
            return
        self._loop = asyncio.get_running_loop()
        await self._verify_receiver()

        def _connect():
            cast = self._factory(self.device)
            cast.wait(timeout=self._timeout)
            return cast

        try:
            self._cast = await asyncio.to_thread(_connect)
        except Exception as e:
            raise TvNotFoundError(scrub_urls(str(e))) from e
        player = self

        class _Listener:
            def new_media_status(self, status):
                player._threadsafe(player._on_media_status, status)

            def load_media_failed(self, item, error_code):
                player._threadsafe(player._on_load_failed, error_code)

        self._cast.media_controller.register_status_listener(_Listener())

    async def _verify_receiver(self) -> None:
        """pychromecast checks neither the TLS certificate nor the Cast device certificate, and the
        media URL carries an access token. Run the same device-auth + identity pinning as mirroring
        (castv2.CastChannel.open, same pin key) first: TvAuthFailedError / TvCertChangedError propagate."""
        from .castv2 import CastChannel

        port = self.device.cast_port or 8009
        try:
            ch = await CastChannel.open(self.device.ip, port, timeout=self._timeout, heartbeat=False,
                                        pin_key=self.device.id or self.device.ip)
        except (OSError, asyncio.TimeoutError) as e:
            raise TvNotFoundError(f"{self.device.ip}:{port}: {e or type(e).__name__}") from e
        await ch.close()

    def _threadsafe(self, fn, *args) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    def running_app(self) -> tuple[Optional[str], str]:
        st = getattr(self._cast, "status", None)
        if st is None:
            return None, ""
        return getattr(st, "app_id", None), getattr(st, "display_name", "") or ""

    # ---------------------------------------------------------- MediaPlayer protocol
    async def load(self, item: MediaItem, url: str, mime: str, start: float = 0, *, force: bool = False,
                   subtitles: Optional[list[dict]] = None, poster_url: Optional[str] = None,
                   active_subtitle: Optional[int] = None) -> None:
        await self.connect()
        app_id, app_name = self.running_app()
        if app_id not in IDLE_APPS and not force:
            raise TvBusyError(app_name or app_id or "")
        if is_stream_url(url) and start > 0:
            url = with_start(url, start)
        self._item, self._mime = item, mime
        self._url = url
        self._offset = start_of(url) if is_stream_url(url) else 0.0
        self._load_kw = {"subtitles": subtitles, "poster_url": poster_url, "active_subtitle": active_subtitle}
        self._emit(PlaybackState("loading", position=start, duration=item.duration, item_id=item.id,
                                 volume=self.state.volume, muted=self.state.muted))
        msg = build_load_message(item, url, mime, start, **self._load_kw)
        await self._send_load(msg)
        self._ensure_ticker()

    async def _send_load(self, msg: dict) -> None:
        mc = self._cast.media_controller
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def cb(*args):
            if not fut.done():
                fut.set_result(args[0] if args else None)

        def send():
            # send_message launches the DMR app first when it isn't running.
            mc.send_message(msg, inc_session_id=True, callback_function=cb)

        await asyncio.to_thread(send)
        try:
            resp = await asyncio.wait_for(asyncio.wrap_future(fut), 25)
        except asyncio.TimeoutError:
            log.warning("no LOAD response within 25 s")
            return
        if isinstance(resp, dict) and resp.get("type") in ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"):
            self._emit(PlaybackState("error", item_id=self._item.id if self._item else None,
                                     error=f"Telewizor nie odtworzył pliku ({resp.get('type')})."))
            raise AirlinxError("cast_load_failed", "Telewizor nie mógł odtworzyć tego pliku.")

    async def play(self) -> None:
        await self._mc_call("play")

    async def pause(self) -> None:
        await self._mc_call("pause")

    async def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        if self._cast is None:
            return
        app_id, _ = self.running_app()

        def _stop():
            try:
                self._cast.media_controller.stop()
            except Exception as e:
                log.debug("media stop: %s", scrub_urls(str(e)))
            if app_id == DMR_APP_ID or app_id is None:
                self._cast.quit_app()

        await asyncio.to_thread(_stop)
        self._emit(PlaybackState("idle", volume=self.state.volume, muted=self.state.muted))

    async def seek(self, seconds: float) -> None:
        if self._item and is_stream_url(self._url):
            url = with_start(self._url, seconds)
            self._url, self._offset = url, start_of(url)
            subs = self._load_kw.get("subtitles")
            if subs:
                self._load_kw["subtitles"] = [dict(s, url=with_start(s["url"], seconds)) for s in subs]
            msg = build_load_message(self._item, url, self._mime, seconds, **self._load_kw)
            self._emit(PlaybackState("buffering", position=seconds, duration=self._item.duration,
                                     item_id=self._item.id, volume=self.state.volume, muted=self.state.muted))
            await self._send_load(msg)
        else:
            await self._mc_call("seek", seconds)

    async def set_volume(self, v: float) -> None:
        v = min(max(v, 0.0), 1.0)
        await asyncio.to_thread(self._cast.set_volume, v)
        self.state.volume = v
        self._emit(self.state)

    async def set_muted(self, muted: bool) -> None:
        await asyncio.to_thread(self._cast.set_volume_muted, muted)

    async def set_subtitle(self, track_id: Optional[int]) -> None:
        if track_id is None:
            await self._mc_call("disable_subtitle")
        else:
            await self._mc_call("enable_subtitle", track_id)

    async def close(self) -> None:
        if self._ticker:
            self._ticker.cancel()
        if self._cast is not None:
            cast, self._cast = self._cast, None
            await asyncio.to_thread(cast.disconnect, 5)

    async def _mc_call(self, name: str, *args) -> None:
        if self._cast is None:
            return
        await asyncio.to_thread(getattr(self._cast.media_controller, name), *args)

    # ---------------------------------------------------------- status
    def _on_media_status(self, status) -> None:
        st = map_media_status(getattr(status, "player_state", None), getattr(status, "idle_reason", None))
        if st == "idle" and self.state.state in ("loading", "buffering"):
            return  # transient IDLE between LOAD and first frame
        self._emit(self._state_from(status, st))

    def _state_from(self, status, st: str) -> PlaybackState:
        try:
            pos = float(status.adjusted_current_time or 0)
        except Exception:
            pos = float(getattr(status, "current_time", 0) or 0)
        dur = self._item.duration if self._item and self._item.duration else getattr(status, "duration", None)
        if st == "ended" and dur:
            pos, self._offset = float(dur), 0.0
        cs = getattr(self._cast, "status", None)
        vol = getattr(cs, "volume_level", None)
        muted = getattr(cs, "volume_muted", None)
        return PlaybackState(st, position=pos + self._offset, duration=dur,
                             volume=self.state.volume if vol is None else float(vol),
                             muted=self.state.muted if muted is None else bool(muted),
                             item_id=self._item.id if self._item else None)

    def _on_load_failed(self, error_code) -> None:
        self._emit(PlaybackState("error", item_id=self._item.id if self._item else None,
                                 error=f"Telewizor nie odtworzył pliku (kod {error_code})."))

    def _emit(self, state: PlaybackState) -> None:
        self.state = state
        try:
            self.on_state(state)
        except Exception:
            log.exception("on_state callback failed")

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.ensure_future(self._tick())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.state.state != "playing" or self._cast is None:
                continue
            status = getattr(self._cast.media_controller, "status", None)
            if status is not None:
                self._emit(self._state_from(status, "playing"))


def _default_factory(device: Device):
    import pychromecast

    try:
        u = uuidlib.UUID(device.id)
    except (ValueError, TypeError):
        u = None
    return pychromecast.get_chromecast_from_host(
        (device.ip, device.cast_port or 8009, u, device.model or None, device.name or None),
        tries=2, timeout=8, retry_wait=1)
