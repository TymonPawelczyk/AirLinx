"""CastMediaPlayer — media playback via the Default Media Receiver (CC1AD845).

Everything (LAUNCH, LOAD, PLAY/PAUSE/SEEK/STOP, volume, MEDIA_STATUS) goes through ONE castv2.CastChannel,
the one that passed Cast device-auth and identity pinning. There is deliberately no second connection (the
former pychromecast one skipped certificate checks, so a MITM could slip in between verification and use).
A dropped channel is re-opened, and verified again, on the next command.

Seeking: direct files (byte-range) use Cast SEEK; streamed remux/transcode URLs ("/s/" in the
MediaServer layout) are reloaded with `?t=<seconds>` and positions are reported with that offset.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..media_server import is_stream_url, scrub_urls, start_of, with_start
from ..models import AirlinxError, Device, MediaItem, PlaybackState, TvBusyError, TvNotFoundError
from .castv2 import NS_MEDIA, NS_RECEIVER, RECEIVER_ID, CastChannel, CastError

log = logging.getLogger(__name__)

DMR_APP_ID = "CC1AD845"
BACKDROP_APP_ID = "E8C28D3C"
IDLE_APPS = {None, "", BACKDROP_APP_ID, DMR_APP_ID}

_LOAD_FAILURES = ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST")
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


@dataclass
class _MediaStatus:
    """The bits of a MEDIA_STATUS entry the player needs (position is extrapolated while playing)."""
    player_state: Optional[str]
    idle_reason: Optional[str]
    current_time: float
    duration: Optional[float]
    rate: float
    at: float

    @property
    def adjusted_current_time(self) -> float:
        if self.player_state == "PLAYING":
            return self.current_time + (time.monotonic() - self.at) * self.rate
        return self.current_time


class CastMediaPlayer:
    def __init__(self, device: Device, on_state: Optional[Callable[[PlaybackState], None]] = None, *,
                 connect_timeout: float = 10.0):
        self.device = device
        self.on_state: Callable[[PlaybackState], None] = on_state or (lambda s: None)
        self._timeout = connect_timeout
        self._ch: Optional[CastChannel] = None
        self._unlisten: Optional[Callable[[], None]] = None
        self._conn_lock = asyncio.Lock()
        self._apps: list[dict] = []
        self._volume: Optional[float] = None
        self._muted: Optional[bool] = None
        self._transport: Optional[str] = None      # DMR transportId (destination of media messages)
        self._session: Optional[str] = None        # DMR sessionId (receiver STOP)
        self._attached = False                     # virtual connection to _transport open on the current channel
        self._media_session: Optional[int] = None
        self._status: Optional[_MediaStatus] = None
        self._item: Optional[MediaItem] = None
        self._url: str = ""
        self._mime: str = ""
        self._offset: float = 0.0
        self._load_kw: dict = {}
        self._ticker: Optional[asyncio.Task] = None
        self.state = PlaybackState("idle")

    # ---------------------------------------------------------- connection
    async def connect(self) -> None:
        async with self._conn_lock:
            if self._ch is not None and self._ch.alive:
                return
            await self._open()

    async def _open(self) -> None:
        """Open + authenticate the channel (Cast device-auth and identity pinning, same pin key as mirroring;
        TvAuthFailedError / TvCertChangedError propagate), then read the receiver status."""
        self._drop_channel()
        port = self.device.cast_port or 8009
        try:
            ch = await CastChannel.open(self.device.ip, port, timeout=self._timeout,
                                        pin_key=self.device.id or self.device.ip)
        except (OSError, asyncio.TimeoutError, CastError) as e:
            raise TvNotFoundError(f"{self.device.ip}:{port}: {e or type(e).__name__}") from e
        self._ch = ch
        self._unlisten = ch.add_listener(self._on_message)
        self._attached = False
        try:
            await self._refresh_status()
            if self._transport is not None:
                if any(a.get("transportId") == self._transport for a in self._apps):
                    await self._attach(self._transport)
                    await ch.send(NS_MEDIA, {"type": "GET_STATUS", "requestId": ch.next_id()}, self._transport)
                else:
                    self._forget_session()
        except (OSError, asyncio.TimeoutError, CastError) as e:
            self._drop_channel(close=True)
            raise TvNotFoundError(f"{self.device.ip}:{port}: {e or type(e).__name__}") from e

    def _drop_channel(self, close: bool = False) -> None:
        if self._unlisten:
            self._unlisten()
            self._unlisten = None
        ch, self._ch = self._ch, None
        self._attached = False
        if ch is not None and close:
            asyncio.ensure_future(ch.close(graceful=False))

    async def _channel(self) -> Optional[CastChannel]:
        """The live channel; re-opened (and re-verified) when the TV dropped it. None if never connected/closed."""
        if self._ch is None:
            return None
        if not self._ch.alive:
            await self.connect()
        return self._ch

    async def _refresh_status(self) -> None:
        assert self._ch is not None
        self._apply_receiver_status(await self._ch.get_status(self._timeout))

    async def _attach(self, transport: str) -> None:
        assert self._ch is not None
        await self._ch.connect_virtual(transport)
        self._attached = True

    def running_app(self) -> tuple[Optional[str], str]:
        if not self._apps:
            return None, ""
        app = self._apps[0]
        return app.get("appId"), app.get("displayName") or ""

    # ---------------------------------------------------------- receiver / media messages
    def _on_message(self, m: Any, p: Optional[dict]) -> None:
        if not p:
            return
        typ = p.get("type")
        if m.namespace == NS_MEDIA and typ == "MEDIA_STATUS":
            self._on_media_payload(p)
        elif m.namespace == NS_RECEIVER and typ == "RECEIVER_STATUS":
            self._apply_receiver_status(p.get("status") or {}, from_event=True)

    def _apply_receiver_status(self, status: dict, *, from_event: bool = False) -> None:
        self._apps = [a for a in (status.get("applications") or []) if isinstance(a, dict)]
        vol = status.get("volume") or {}
        if vol.get("level") is not None:
            self._volume = float(vol["level"])
        if vol.get("muted") is not None:
            self._muted = bool(vol["muted"])
        if from_event and self._transport and not any(a.get("transportId") == self._transport for a in self._apps):
            log.info("media receiver app is gone on the TV")
            self._forget_session()
            self._cancel_ticker()
            if self.state.state != "idle":
                self._emit(PlaybackState("idle", volume=self.state.volume, muted=self.state.muted))

    def _forget_session(self) -> None:
        self._transport = self._session = self._media_session = self._status = None
        self._attached = False

    def _on_media_payload(self, p: dict) -> None:
        entries = p.get("status") or []
        if not entries or not isinstance(entries[0], dict):
            return
        e = entries[0]
        if e.get("mediaSessionId") is not None:
            self._media_session = e["mediaSessionId"]
        dur = (e.get("media") or {}).get("duration")
        if dur is None and self._status is not None:
            dur = self._status.duration
        st = _MediaStatus(e.get("playerState"), e.get("idleReason"), float(e.get("currentTime") or 0),
                          float(dur) if dur else None, float(e.get("playbackRate") or 1), time.monotonic())
        self._on_media_status(st)

    # ---------------------------------------------------------- MediaPlayer protocol
    async def load(self, item: MediaItem, url: str, mime: str, start: float = 0, *, force: bool = False,
                   subtitles: Optional[list[dict]] = None, poster_url: Optional[str] = None,
                   active_subtitle: Optional[int] = None) -> None:
        await self.connect()
        ch = self._ch
        assert ch is not None
        try:
            await self._refresh_status()
        except (OSError, asyncio.TimeoutError, CastError) as e:
            raise TvNotFoundError(scrub_urls(str(e))) from e
        app_id, app_name = self.running_app()
        if app_id not in IDLE_APPS and not force:
            raise TvBusyError(app_name or app_id or "")
        await self._ensure_receiver_app(ch)
        if is_stream_url(url) and start > 0:
            url = with_start(url, start)
        self._item, self._mime = item, mime
        self._url = url
        self._offset = start_of(url) if is_stream_url(url) else 0.0
        self._load_kw = {"subtitles": subtitles, "poster_url": poster_url, "active_subtitle": active_subtitle}
        self._emit(PlaybackState("loading", position=start, duration=item.duration, item_id=item.id,
                                 volume=self._vol(), muted=self._mut()))
        msg = build_load_message(item, url, mime, start, **self._load_kw)
        await self._send_load(msg)
        self._ensure_ticker()

    async def _ensure_receiver_app(self, ch: CastChannel) -> None:
        """Use the running Default Media Receiver, or LAUNCH it (replacing whatever ran when forced)."""
        app = next((a for a in self._apps if a.get("appId") == DMR_APP_ID and a.get("transportId")), None)
        if app is None:
            try:
                app = await ch.launch(DMR_APP_ID, timeout=30)
            except (CastError, asyncio.TimeoutError, OSError) as e:
                log.warning("DMR launch failed: %s", scrub_urls(str(e)))
                raise AirlinxError("cast_launch_failed", "Telewizor nie uruchomił odtwarzacza.") from e
            self._apps = [app]
        tid = app["transportId"]
        if tid != self._transport:
            self._media_session = self._status = None
            self._attached = False
        self._transport, self._session = tid, app.get("sessionId")
        if not self._attached:
            await self._attach(tid)

    async def _send_load(self, msg: dict) -> None:
        ch = await self._channel()
        assert ch is not None and self._transport is not None
        msg = dict(msg, sessionId=self._session) if self._session else msg
        try:
            resp = await ch.request(NS_MEDIA, msg, self._transport, timeout=25)
        except asyncio.TimeoutError:
            log.warning("no LOAD response within 25 s")
            return
        except (OSError, CastError) as e:
            raise TvNotFoundError(scrub_urls(str(e))) from e
        if resp.get("type") in _LOAD_FAILURES:
            self._emit(PlaybackState("error", item_id=self._item.id if self._item else None,
                                     error=f"Telewizor nie odtworzył pliku ({resp.get('type')})."))
            raise AirlinxError("cast_load_failed", "Telewizor nie mógł odtworzyć tego pliku.")

    async def _media_cmd(self, typ: str, **extra: Any) -> None:
        if self._transport is None or self._media_session is None:
            return
        ch = await self._channel()
        if ch is None:
            return
        await ch.send(NS_MEDIA, {"type": typ, "mediaSessionId": self._media_session,
                                 "requestId": ch.next_id(), **extra}, self._transport)

    async def play(self) -> None:
        await self._media_cmd("PLAY")

    async def pause(self) -> None:
        await self._media_cmd("PAUSE")

    async def stop(self) -> None:
        self._cancel_ticker()
        if self._ch is None:
            return
        try:
            await self._media_cmd("STOP")
        except Exception as e:  # noqa: BLE001
            log.debug("media stop: %s", scrub_urls(str(e)))
        app_id, _ = self.running_app()
        if self._session and (app_id == DMR_APP_ID or app_id is None):
            try:
                ch = await self._channel()
                if ch is not None:
                    await ch.stop_app(self._session)
            except Exception as e:  # noqa: BLE001
                log.debug("quit app: %s", scrub_urls(str(e)))
        self._forget_session()
        self._emit(PlaybackState("idle", volume=self._vol(), muted=self._mut()))

    async def seek(self, seconds: float) -> None:
        if self._item and is_stream_url(self._url):
            url = with_start(self._url, seconds)
            self._url, self._offset = url, start_of(url)
            subs = self._load_kw.get("subtitles")
            if subs:
                self._load_kw["subtitles"] = [dict(s, url=with_start(s["url"], seconds)) for s in subs]
            msg = build_load_message(self._item, url, self._mime, seconds, **self._load_kw)
            self._emit(PlaybackState("buffering", position=seconds, duration=self._item.duration,
                                     item_id=self._item.id, volume=self._vol(), muted=self._mut()))
            await self._send_load(msg)
        else:
            await self._media_cmd("SEEK", currentTime=seconds, resumeState="PLAYBACK_START")

    async def set_volume(self, v: float) -> None:
        v = min(max(v, 0.0), 1.0)
        await self._receiver_cmd("SET_VOLUME", volume={"level": v})
        self.state.volume = v
        self._volume = v
        self._emit(self.state)

    async def set_muted(self, muted: bool) -> None:
        await self._receiver_cmd("SET_VOLUME", volume={"muted": muted})

    async def _receiver_cmd(self, typ: str, **extra: Any) -> None:
        ch = await self._channel()
        if ch is None:
            return
        await ch.send(NS_RECEIVER, {"type": typ, "requestId": ch.next_id(), **extra}, RECEIVER_ID)

    async def set_subtitle(self, track_id: Optional[int]) -> None:
        await self._media_cmd("EDIT_TRACKS_INFO", activeTrackIds=[] if track_id is None else [track_id])

    async def close(self) -> None:
        self._cancel_ticker()
        ch = self._ch
        self._drop_channel()
        self._forget_session()
        if ch is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ch.close(), 5)

    # ---------------------------------------------------------- status
    def _vol(self) -> float:
        return float((self.state.volume if self._volume is None else self._volume) or 0.0)

    def _mut(self) -> bool:
        return self.state.muted if self._muted is None else self._muted

    def _on_media_status(self, status: _MediaStatus) -> None:
        self._status = status
        st = map_media_status(status.player_state, status.idle_reason)
        if st == "idle" and self.state.state in ("loading", "buffering"):
            return  # transient IDLE between LOAD and first frame
        self._emit(self._state_from(status, st))

    def _state_from(self, status: _MediaStatus, st: Any) -> PlaybackState:
        pos = float(status.adjusted_current_time or 0)
        dur = self._item.duration if self._item and self._item.duration else status.duration
        if st == "ended" and dur:
            pos, self._offset = float(dur), 0.0
        return PlaybackState(st, position=pos + self._offset, duration=dur, volume=self._vol(), muted=self._mut(),
                             item_id=self._item.id if self._item else None)

    def _emit(self, state: PlaybackState) -> None:
        self.state = state
        try:
            self.on_state(state)
        except Exception:
            log.exception("on_state callback failed")

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self._ticker = asyncio.ensure_future(self._tick())

    def _cancel_ticker(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.state.state != "playing" or self._status is None:
                continue
            self._emit(self._state_from(self._status, "playing"))
