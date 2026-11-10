"""DLNA playback (UPnP AVTransport/RenderingControl) via async_upnp_client, plus DlnaMirror fallback.

DlnaPlayer polls GetTransportInfo/GetPositionInfo every second (no GENA subscriptions — no
callback server needed) and maps them to PlaybackState.

Subtitles on DLNA: Samsung renderers accept an SRT URL via the `sec:CaptionInfoEx` DIDL element
and the `CaptionInfo.sec` HTTP header (MediaServer sends the header for DLNA items with text subs).
Bitmap subtitles are not delivered (burn-in = future work).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape, quoteattr

from .media_server import scrub_urls
from .media_server import LiveSource, MediaServer, dlna_content_features, fmt_npt, is_stream_url, start_of, url_kind, with_start
from .models import AirlinxError, Device, MediaItem, PlaybackState, TvBusyError, TvNotFoundError

log = logging.getLogger(__name__)

UPNP_CLASS = {"video": "object.item.videoItem", "audio": "object.item.audioItem.musicTrack",
              "image": "object.item.imageItem.photo"}


def protocol_info(url: str, mime: str) -> str:
    return f"http-get:*:{mime}:{dlna_content_features(url_kind(url, mime), mime)}"


def build_didl(item: MediaItem, url: str, mime: str, *, poster_url: Optional[str] = None,
               subtitle_url: Optional[str] = None, duration: Optional[float] = None) -> str:
    dur = duration if duration is not None else item.duration
    if dur and is_stream_url(url):
        dur = max(dur - start_of(url), 0)
    res_attrs = f"protocolInfo={quoteattr(protocol_info(url, mime))}"
    if dur:
        res_attrs += f" duration={quoteattr(fmt_npt(dur))}"
    parts = [
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/" '
        'xmlns:sec="http://www.sec.co.kr/">',
        f'<item id={quoteattr(item.id or "0")} parentID="-1" restricted="1">',
        f"<dc:title>{escape(item.title or 'AirLinx')}</dc:title>",
        f"<upnp:class>{UPNP_CLASS.get(item.kind, UPNP_CLASS['video'])}</upnp:class>",
    ]
    if poster_url:
        parts.append(f'<upnp:albumArtURI dlna:profileID="PNG_TN">{escape(poster_url)}</upnp:albumArtURI>')
    parts.append(f"<res {res_attrs}>{escape(url)}</res>")
    if subtitle_url:
        parts.append(f'<sec:CaptionInfoEx sec:type="srt">{escape(subtitle_url)}</sec:CaptionInfoEx>')
        parts.append(f'<sec:CaptionInfo sec:type="srt">{escape(subtitle_url)}</sec:CaptionInfo>')
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)


def map_transport_state(transport: Optional[str], prev: str, user_stopped: bool) -> str:
    t = (transport or "").upper()
    if t == "PLAYING":
        return "playing"
    if t == "PAUSED_PLAYBACK" or t == "PAUSED_RECORDING":
        return "paused"
    if t == "TRANSITIONING":
        return "buffering"
    if t == "STOPPED":
        if prev in ("playing", "paused", "buffering") and not user_stopped:
            return "ended"
        return "ended" if prev == "ended" else "idle"
    if t == "NO_MEDIA_PRESENT":
        return "idle"
    return prev if prev in ("loading",) else "idle"


class DlnaPlayer:
    def __init__(self, device: Device, on_state: Optional[Callable[[PlaybackState], None]] = None, *,
                 dmr: Any = None, poll_interval: float = 1.0):
        self.device = device
        self.on_state: Callable[[PlaybackState], None] = on_state or (lambda s: None)
        self._dmr = dmr
        self._requester = None
        self.poll_interval = poll_interval
        self._poll: Optional[asyncio.Task] = None
        self._item: Optional[MediaItem] = None
        self._url = ""
        self._mime = ""
        self._offset = 0.0
        self._extra: dict = {}
        self._user_stopped = False
        self._grace_until = 0.0
        self.state = PlaybackState("idle")

    async def connect(self) -> None:
        if self._dmr is not None:
            return
        if not self.device.dlna_location:
            raise TvNotFoundError("brak adresu DLNA")
        from async_upnp_client.aiohttp import AiohttpRequester
        from async_upnp_client.client_factory import UpnpFactory
        from async_upnp_client.profiles.dlna import DmrDevice

        self._requester = AiohttpRequester(timeout=5)
        try:
            dev = await UpnpFactory(self._requester, non_strict=True).async_create_device(self.device.dlna_location)
        except Exception as e:
            raise TvNotFoundError(scrub_urls(str(e))) from e
        self._dmr = DmrDevice(dev, None)
        with contextlib.suppress(Exception):
            await self._dmr.async_update(do_ping=False)

    # ---------------------------------------------------------- protocol
    async def load(self, item: MediaItem, url: str, mime: str, start: float = 0, *, force: bool = False,
                   poster_url: Optional[str] = None, subtitle_url: Optional[str] = None) -> None:
        await self.connect()
        dmr = self._dmr
        ts = _tstate(dmr)
        current = getattr(dmr, "current_track_uri", None) or ""
        ours = bool(self._url) and current.split("?")[0] == self._url.split("?")[0]
        if ts in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING") and not force and not ours and current:
            raise TvBusyError("inne odtwarzanie DLNA")
        if is_stream_url(url) and start > 0:
            url = with_start(url, start)
        self._item, self._url, self._mime = item, url, mime
        self._offset = start_of(url) if is_stream_url(url) else 0.0
        self._extra = {"poster_url": poster_url, "subtitle_url": subtitle_url}
        self._emit(PlaybackState("loading", position=start, duration=item.duration, item_id=item.id,
                                 volume=self.state.volume))
        await self._set_and_play(start)
        self._ensure_poll()

    async def _set_and_play(self, start: float) -> None:
        dmr = self._dmr
        if _tstate(dmr) in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"):
            with contextlib.suppress(Exception):
                await dmr.async_stop()
        assert self._item
        didl = build_didl(self._item, self._url, self._mime, **self._extra)
        try:
            await dmr.async_set_transport_uri(self._url, self._item.title, didl)
            with contextlib.suppress(Exception):
                await dmr.async_wait_for_can_play(5)
            await _action(dmr, "Play", Speed="1")
        except Exception as e:
            self._emit(PlaybackState("error", item_id=self._item.id, error="Telewizor odrzucił plik (DLNA).",
                                     volume=None))  # unknown: the controller keeps the user's volume
            raise AirlinxError("dlna_load_failed", "Telewizor nie mógł odtworzyć tego pliku (DLNA).") from e
        self._user_stopped = False
        self._grace_until = asyncio.get_running_loop().time() + 6
        if start > 0 and not is_stream_url(self._url):
            with contextlib.suppress(Exception):
                await asyncio.sleep(1.0)
                await _action(self._dmr, "Seek", Unit="REL_TIME", Target=fmt_npt(start).split(".")[0])

    async def play(self) -> None:
        await _action(self._dmr, "Play", Speed="1")
        self._user_stopped = False
        self._ensure_poll()

    async def pause(self) -> None:
        await _action(self._dmr, "Pause")

    async def stop(self) -> None:
        self._user_stopped = True
        if self._dmr is not None:
            with contextlib.suppress(Exception):
                await _action(self._dmr, "Stop")
        if self._poll:
            self._poll.cancel()
            self._poll = None
        self._emit(PlaybackState("idle", volume=self.state.volume))

    async def seek(self, seconds: float) -> None:
        if is_stream_url(self._url) and self._item:
            self._url = with_start(self._url, seconds)
            self._offset = start_of(self._url)
            self._emit(PlaybackState("buffering", position=seconds, duration=self._item.duration,
                                     item_id=self._item.id, volume=self.state.volume))
            await self._set_and_play(0)
        else:
            await _action(self._dmr, "Seek", Unit="REL_TIME", Target=fmt_npt(seconds).split(".")[0])

    async def set_volume(self, v: float) -> None:
        v = min(max(v, 0.0), 1.0)
        await self._dmr.async_set_volume_level(v)
        self.state.volume = v
        self._emit(self.state)

    async def close(self) -> None:
        if self._poll:
            self._poll.cancel()
        if self._requester is not None and hasattr(self._requester, "async_close"):
            with contextlib.suppress(Exception):
                await self._requester.async_close()

    # ---------------------------------------------------------- polling
    def _ensure_poll(self) -> None:
        if self._poll is None or self._poll.done():
            self._poll = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        errors = 0
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.poll_once()
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                errors += 1
                log.debug("dlna poll failed: %s", scrub_urls(str(e)))
                if errors == 5:
                    self._emit(PlaybackState("error", item_id=self._item.id if self._item else None,
                                             error="Utracono połączenie z telewizorem (DLNA).",
                                             volume=None))  # unknown: the controller keeps the user's volume

    async def poll_once(self) -> PlaybackState:
        dmr = self._dmr
        await dmr.async_update(do_ping=False)
        ts = _tstate(dmr)
        prev = self.state.state
        in_grace = asyncio.get_running_loop().time() < self._grace_until
        if in_grace and (ts in ("STOPPED", "NO_MEDIA_PRESENT", "")):
            st = prev  # renderer still switching
        else:
            st = map_transport_state(ts, prev, self._user_stopped)
        pos = getattr(dmr, "media_position", None)
        dur = (self._item.duration if self._item and self._item.duration else getattr(dmr, "media_duration", None))
        if st == "ended" and dur:
            pos, self._offset = dur, 0.0
        vol = getattr(dmr, "volume_level", None)
        muted = bool(getattr(dmr, "is_volume_muted", None))
        # Samsung DMRs with fixed volume answer GetVolume=0 whatever the real level is: a 0 that is
        # not an explicit mute is "unknown", never a real value.
        vol = None if vol is None or (float(vol) == 0.0 and not muted) else float(vol)
        state = PlaybackState(st, position=float(pos or 0) + self._offset, duration=dur,
                              volume=vol, muted=muted,
                              item_id=self._item.id if self._item else None)
        self._emit(state)
        if st in ("ended", "idle") and not in_grace and self._poll and prev == st:
            self._poll.cancel()  # nothing to track anymore
        return state

    def _emit(self, state: PlaybackState) -> None:
        self.state = state
        try:
            self.on_state(state)
        except Exception:
            log.exception("on_state callback failed")


def _tstate(dmr: Any) -> str:
    ts = getattr(dmr, "transport_state", None)
    return getattr(ts, "value", ts) or ""


async def _action(dmr: Any, name: str, **kwargs) -> None:
    """Call an AVTransport action directly (works on renderers that don't advertise CurrentTransportActions)."""
    action = dmr._action("AVT", name) if hasattr(dmr, "_action") else None
    if action is None:
        fn = {"Play": "async_play", "Pause": "async_pause", "Stop": "async_stop"}.get(name)
        if fn and hasattr(dmr, fn):
            await getattr(dmr, fn)()
            return
        if name == "Seek" and hasattr(dmr, "async_seek_rel_time"):
            h, m, s = (int(x) for x in kwargs["Target"].split(":"))
            await dmr.async_seek_rel_time(timedelta(hours=h, minutes=m, seconds=s))
            return
        raise AirlinxError("dlna_unsupported", f"Telewizor nie obsługuje akcji {name}.")
    await action.async_call(InstanceID=0, **kwargs)


class DlnaMirror:
    """Fallback screen mirroring over DLNA: live MPEG-TS from `source` → MediaServer → renderer.
    Expect several seconds of latency (TV buffering); intended only when Cast mirroring is unavailable."""

    NAME = "mirror"

    def __init__(self, device: Device, server: MediaServer, source: LiveSource, *,
                 title: str = "AirLinx — ekran", on_state: Optional[Callable[[PlaybackState], None]] = None,
                 player: Optional[DlnaPlayer] = None):
        self.device, self.server, self.source, self.title = device, server, source, title
        self.player = player or DlnaPlayer(device, on_state)
        self.url: Optional[str] = None

    async def start(self, force: bool = False) -> str:
        self.url = self.server.register_live(self.NAME, self.source, target_ip=self.device.ip)
        item = MediaItem(id="mirror", path_or_url=self.url, title=self.title, duration=None, kind="video")
        await self.player.load(item, self.url, "video/mpeg", force=force)
        return self.url

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.player.stop()
        await self.player.close()
        self.server.unregister_live(self.NAME)
