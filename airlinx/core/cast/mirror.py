"""Screen mirroring to a Google Cast receiver (Cast Streaming, app "Chrome Mirroring").

Flow: TLS CASTv2 → GET_STATUS (busy guard) → LAUNCH 0F5096E8 → CONNECT(transportId) →
OFFER on ``urn:x-cast:com.google.cast.webrtc`` → ANSWER (udpPort, sendIndexes→ssrcs) →
ffmpeg (capture.py) → AES-CTR → Cast RTP (paced) → UDP; RTCP SR every 250 ms; receiver
feedback → ACK tracking / retransmits / keyframe requests. ``stop()`` always STOPs the app.

See spikes/cast_streaming/PROTOCOL.md for the verified wire details.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import random
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..audio import AudioPipeline, spec_from
from ..capture import (AudioCapture, EncodedFrame, VideoCapture, VideoParams, default_audio_monitor,
                       hevc_encoder_from_caps, output_size, probe_encoders)
from ..models import (AirlinxError, Device, MirrorOptions, PermissionPendingError, Source, Stats,
                      TvBusyError, TvNotFoundError)
from ..sources import clamp_to_screen, screen_size, window_geometry
from . import castv2
from .castv2 import (MIRRORING_APP_ID, NS_CONNECTION, NS_RECEIVER, NS_REMOTING, NS_WEBRTC, CastChannel,
                     CastError, ChannelClosed)
from .rtp import AUDIO_PT, VIDEO_PT, Pacer, RtcpRouter, StreamSender

log = logging.getLogger("airlinx.cast.mirror")

SR_INTERVAL = 0.25
STATS_INTERVAL = 0.5
KEYFRAME_MIN_INTERVAL = 1.0
STALL_TIMEOUT_LIVE = 5.0
STALL_TIMEOUT_START = 8.0
RECONNECT_BACKOFF = (1.0, 2.0, 4.0)
WINDOW_POLL = 1.0
MIN_PACE_BPS = 80e6
AUDIO_BITRATE = 128_000
CAPS_TIMEOUT = 2.0        # GET_CAPABILITIES reply (only asked when HEVC is a candidate)
CAPTURE_GRAB_MS = 3.0     # x11grab XShm copy estimate (added to half a frame interval)

CODEC_NAMES = {"h264": "H.264", "hevc": "H.265"}
STATES = ("connecting", "waiting_tv", "live", "reconnecting", "stopped", "error")
LAUNCH_GUARD_S = 90.0     # after a failed start, STOP a late-appearing mirroring app (late consent)
LAUNCH_GUARD_POLL = 2.0
APP_GONE_PL = "Mirroring zakończono na telewizorze."
WINDOW_GONE_POLLS = 2     # consecutive polls without the shared window → end the session
WINDOW_GONE_PL = "Udostępniane okno zostało zamknięte lub zminimalizowane."
NO_RETRY_CODES = ("tv_cert_changed", "tv_auth_failed")

# TVs (pin key) that accepted an HEVC OFFER but never showed it (no feedback, or the encoder died): "auto" does
# not try HEVC on them again until the app restarts. An explicit "hevc" setting ignores this.
_hevc_bad: set[str] = set()

# Late-launch guards per TV (pin key), not per MirrorSession: the controller creates a new session
# object for every start, and a guard left by a cancelled start must not STOP the next one.
_launch_guards: dict[str, asyncio.Task] = {}


class _Stopped(Exception):
    """stop() was requested while an operation was in flight."""


class _AppGone(Exception):
    """Reconnect found that our mirroring session no longer runs on the TV."""


async def _shielded(coro) -> None:  # noqa: ANN001
    """Run cleanup to completion even if the caller gets cancelled again."""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await task
        raise


# --------------------------------------------------------------------------------------
# Presets / negotiation (pure helpers, unit-tested)
# --------------------------------------------------------------------------------------
@dataclass
class Effective:
    target_delay_ms: int
    bitrate: int
    fps: int
    max_height: int
    audio: bool
    gop_s: float
    codec: str = "h264"                # the user's choice (auto | h264 | hevc), not what is negotiated
    audio_key: tuple = ("system",)     # what is captured (AudioSpec.key); change → restart audio only


def effective_options(o: MirrorOptions) -> Effective:
    akey = spec_from(o).key()
    if o.preset == "cinema":
        return Effective(target_delay_ms=max(400, o.target_delay_ms),
                         bitrate=min(int(o.bitrate * 1.5), 40_000_000), fps=o.fps,
                         max_height=o.max_height, audio=o.audio, gop_s=4.0, codec=o.codec, audio_key=akey)
    return Effective(target_delay_ms=max(50, min(o.target_delay_ms, 400)), bitrate=o.bitrate, fps=o.fps,
                     max_height=o.max_height, audio=o.audio, gop_s=2.0, codec=o.codec, audio_key=akey)


@dataclass
class StreamKeys:
    ssrc: int
    key: bytes
    iv: bytes

    @classmethod
    def random(cls) -> "StreamKeys":
        return cls(random.randint(1, 0x7FFFFFFF), os.urandom(16), os.urandom(16))


def hevc_level(width: int, height: int, fps: int) -> str:
    """Lowest HEVC level (main tier) that covers the picture size and sample rate."""
    px = width * height
    if px <= 2_228_224 and px * fps <= 133_693_440:
        return "4.1"
    if px <= 8_912_896 and px * fps <= 267_386_880:
        return "5.0"
    return "5.1"


def build_offer(*, video: StreamKeys, audio: Optional[StreamKeys], width: int, height: int, fps: int,
                bitrate: int, target_delay_ms: int, audio_bitrate: int = AUDIO_BITRATE,
                codec: str = "h264") -> dict:
    """OFFER message (without seqNum; CastChannel.request adds it).

    HEVC: ``codecName "hevc"``, profile main and the level for the picture. ``codecParameter`` is left out on
    purpose: it is optional, Chrome does not send it, and openscreen rejects anything not starting with "hev1"."""
    streams = [{
        "index": 0, "type": "video_source", "codecName": codec, "rtpProfile": "cast",
        "rtpPayloadType": VIDEO_PT, "ssrc": video.ssrc, "maxFrameRate": f"{fps * 1000}/1000",
        "timeBase": "1/90000", "maxBitRate": bitrate, "profile": "main",
        "level": hevc_level(width, height, fps) if codec == "hevc" else "4",
        "targetDelay": target_delay_ms, "aesKey": video.key.hex(), "aesIvMask": video.iv.hex(),
        "receiverRtcpEventLog": True, "channels": 1,
        "resolutions": [{"width": width, "height": height}],
    }]
    if audio is not None:
        streams.append({
            "index": 1, "type": "audio_source", "codecName": "opus", "rtpProfile": "cast",
            "rtpPayloadType": AUDIO_PT, "ssrc": audio.ssrc, "targetDelay": target_delay_ms,
            "aesKey": audio.key.hex(), "aesIvMask": audio.iv.hex(), "bitRate": audio_bitrate,
            "sampleRate": 48000, "timeBase": "1/48000", "channels": 2, "receiverRtcpEventLog": True})
    return {"type": "OFFER",
            "offer": {"castMode": "mirroring", "receiverGetStatus": True, "supportedStreams": streams}}


@dataclass
class AnswerInfo:
    udp_port: int
    receiver_ssrcs: dict[int, int]   # offered stream index -> receiver SSRC
    display: dict


def parse_answer(ans: dict) -> AnswerInfo:
    """Validate an ANSWER and map ``sendIndexes`` to receiver SSRCs (pairwise, by position)."""
    if ans.get("type") != "ANSWER":
        raise CastError(f"unexpected reply to OFFER: {ans.get('type')}")
    if ans.get("result") != "ok":
        err = ans.get("error") or {}
        raise CastError(f"ANSWER error: {err.get('description') or err or ans.get('result')}")
    a = ans.get("answer") or {}
    idx, ssrcs = a.get("sendIndexes") or [], a.get("ssrcs") or []
    if len(idx) != len(ssrcs):
        raise CastError(f"ANSWER sendIndexes/ssrcs length mismatch: {idx} vs {ssrcs}")
    port = a.get("udpPort")
    if not isinstance(port, int) or not 0 < port < 65536:
        raise CastError(f"ANSWER without valid udpPort: {port!r}")
    mapping = {int(i): int(s) for i, s in zip(idx, ssrcs)}
    if 0 not in mapping:
        raise CastError("receiver rejected the video stream")
    return AnswerInfo(udp_port=port, receiver_ssrcs=mapping, display=a.get("display") or {})


def is_structural_change(a: Effective, b: Effective) -> bool:
    """Changes that need a new OFFER (resolution/fps/stream set/codec)."""
    return a.audio != b.audio or a.max_height != b.max_height or a.fps != b.fps or a.codec != b.codec


def parse_tv_codecs(reply: Optional[dict]) -> Optional[set[str]]:
    """Media capabilities (``mediaCaps``) from a GET_CAPABILITIES reply; None when absent or malformed."""
    caps = reply.get("capabilities") if isinstance(reply, dict) else None
    media = caps.get("mediaCaps") if isinstance(caps, dict) else None
    if not isinstance(media, list):
        return None
    return {c for c in media if isinstance(c, str)}


def plan_video(pref: str, caps: dict[str, bool], tv_codecs: Optional[set[str]],
               exclude: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, str, bool]]:
    """Ordered (codec, ffmpeg encoder, gpu_scale) candidates to offer, best first; the last one is the fallback.

    * ``h264``: H.264 only (NVENC, else libx264).
    * ``auto``: HEVC first only when ffmpeg can encode it AND the TV lists "hevc" (unknown → H.264).
    * ``hevc``: HEVC first when ffmpeg can, unless the TV's own list lacks it (unknown → try).
    H.264 stays the fallback whenever an H.264 encoder exists."""
    plan: list[tuple[str, str, bool]] = []
    hevc = hevc_encoder_from_caps(caps)
    if pref in ("auto", "hevc") and "hevc" not in exclude and hevc is not None:
        if pref == "hevc":
            tv_ok = tv_codecs is None or "hevc" in tv_codecs
        else:
            tv_ok = tv_codecs is not None and "hevc" in tv_codecs
        if tv_ok:
            plan.append(("hevc", hevc[0], hevc[1]))
    if caps.get("nvenc"):
        plan.append(("h264", "h264_nvenc", bool(caps.get("cuda_scale"))))
    elif caps.get("x264"):
        plan.append(("h264", "libx264", False))
    if not plan:
        raise AirlinxError("no_encoder", "Brak kodera H.264 ani H.265 (ffmpeg z NVENC, libx264 lub libx265).")
    return plan


# --------------------------------------------------------------------------------------
# UDP
# --------------------------------------------------------------------------------------
class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, float], None]):
        self.on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        self.on_datagram(data, time.monotonic())

    def error_received(self, exc: Exception) -> None:
        log.debug("udp error: %s", exc)


@dataclass
class _Media:
    transport: asyncio.DatagramTransport
    video: StreamSender
    audio: Optional[StreamSender]
    router: RtcpRouter
    pacer: Pacer
    wake: asyncio.Event
    vcap: VideoCapture
    acap: Optional[AudioCapture]
    eff: Effective
    out_size: tuple[int, int]
    geometry: tuple[int, int, int, int]
    started: float
    tasks: list[asyncio.Task] = dataclasses.field(default_factory=list)
    first_ack: bool = False
    pipeline: Optional[AudioPipeline] = None   # audio routing (app mode) owned by this media instance
    codec: str = "h264"                        # what was negotiated: "h264" | "hevc"
    can_fall_back: bool = False                # HEVC stream with an H.264 encoder available as plan B


async def _cancel_launch_guard(key: str) -> None:
    task = _launch_guards.pop(key, None)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task})   # unlike `await task`, our own cancellation still propagates


# --------------------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------------------
class MirrorSession:
    live_audio_switch = True   # update() can change the audio source without a new OFFER

    def __init__(self, device: Device, source: Source, opts: MirrorOptions,
                 on_stats: Callable[[Stats], None], on_state: Callable[[str, Optional[str]], None]):
        self.device = device
        self.source = source
        self.opts = opts
        self.on_stats = on_stats
        self.on_state = on_state
        self.state = "stopped"
        self._ch: Optional[CastChannel] = None
        self._app: Optional[dict] = None
        self._media: Optional[_Media] = None
        self._events: asyncio.Queue = asyncio.Queue()
        self._supervisor: Optional[asyncio.Task] = None
        self._follower: Optional[asyncio.Task] = None
        self._stopping = False
        self._suppress_channel_events = False
        self._kf_last = 0.0
        self._kf_task: Optional[asyncio.Task] = None
        self._op_lock = asyncio.Lock()
        self._launch_sent = False
        self._app_pending = False
        self._window_gone = False   # sticky: the "fatal" event alone can be dropped by a reconnect
        self._no_hevc = False       # HEVC failed on this session (even when forced): reconnects use H.264
        self.on_notice: Optional[Callable[[str], None]] = None   # Polish user notices (audio fallbacks)

    # ---- public API ------------------------------------------------------------------
    async def start(self, force: bool = False) -> None:
        """Start mirroring. Safe to cancel: cleans up and STOPs any app it launched."""
        if self.state not in ("stopped", "error"):
            raise AirlinxError("mirror_running", "Mirroring już działa.")
        self._stopping = False
        self._launch_sent = False
        self._app_pending = False
        self._window_gone = False
        self._events = asyncio.Queue()
        self._set_state("connecting")
        prewarm = asyncio.ensure_future(probe_encoders())  # ~1 s of ffmpeg probes, overlaps LAUNCH
        try:
            await self._connect_and_launch(force=force)
            await prewarm
            self._checkpoint()
            await self._start_media()
            self._checkpoint()
        except TvBusyError:
            prewarm.cancel()
            await _shielded(self._close_channel())
            self._set_state("stopped", None)
            raise
        except BaseException as e:
            prewarm.cancel()
            await _shielded(self._abort_start())
            if isinstance(e, (asyncio.CancelledError, _Stopped)):
                if self.state != "stopped":
                    self._set_state("stopped")
                if isinstance(e, asyncio.CancelledError):
                    raise
                return
            err = self._to_airlinx(e)
            self._set_state("error", err.message_pl)
            raise err from e
        self._supervisor = asyncio.create_task(self._supervise(), name="mirror-supervisor")
        self._restart_follower()

    async def stop(self) -> None:
        """Stop mirroring; always STOPs our app on the TV. Waits for in-flight update/switch."""
        self._stopping = True
        for t in (self._supervisor, self._follower, self._kf_task):
            if t and t is not asyncio.current_task() and not t.done():
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        async with self._op_lock:
            await _shielded(self._teardown(stop_app=True))
        if self.state != "stopped":
            self._set_state("stopped")

    def _checkpoint(self) -> None:
        if self._stopping:
            raise _Stopped()

    async def _abort_start(self) -> None:
        """Failed/cancelled start(): stop media, STOP our app and every mirroring session that
        a LAUNCH we sent may have created; keep watching for a late user consent."""
        launched = self._launch_sent
        await self._stop_media()
        await self._stop_app()
        if launched:
            await self._stop_mirroring_apps()
        await self._close_channel()
        if launched and self._app_pending:
            key = self._pin_key
            old = _launch_guards.get(key)
            if old is not None and not old.done():
                old.cancel()
            task = asyncio.create_task(self._guard_late_launch(), name="mirror-launch-guard")
            _launch_guards[key] = task
            task.add_done_callback(lambda t: _launch_guards.get(key) is t and _launch_guards.pop(key, None))

    async def _open_aux_channel(self) -> CastChannel:
        return await CastChannel.open(self.device.ip, self.device.cast_port or 8009, timeout=5,
                                      heartbeat=False, pin_key=self._pin_key)

    async def _stop_mirroring_apps(self, ch: Optional[CastChannel] = None) -> int:
        """GET_STATUS and STOP every MIRRORING_APP_ID session. Returns how many were stopped."""
        own = ch is None
        stopped = 0
        try:
            if ch is None:
                ch = self._ch if self._ch is not None and self._ch.alive else await self._open_aux_channel()
                own = ch is not self._ch
            status = await ch.get_status(timeout=4)
            for app in status.get("applications") or []:
                if app.get("appId") == MIRRORING_APP_ID and app.get("sessionId"):
                    await ch.stop_app(app["sessionId"], timeout=4)
                    log.info("STOP sent for leftover mirroring session %s", app["sessionId"])
                    stopped += 1
        except Exception as e:  # noqa: BLE001
            log.warning("cleanup of mirroring sessions failed: %s", e)
        finally:
            if own and ch is not None and ch is not self._ch:
                with contextlib.suppress(Exception):
                    await ch.close()
        return stopped

    async def _guard_late_launch(self) -> None:
        """The TV may show a consent dialog; if the user accepts after we gave up, STOP the app."""
        deadline = time.monotonic() + LAUNCH_GUARD_S
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(LAUNCH_GUARD_POLL)
                if await self._stop_mirroring_apps():
                    return
        except asyncio.CancelledError:
            pass

    async def update(self, opts: MirrorOptions) -> None:
        async with self._op_lock:
            old = effective_options(self.opts)
            self.opts = opts
            new = effective_options(opts)
            m = self._media
            if m is None or self._stopping:
                return
            if is_structural_change(old, new):
                await self._renegotiate()
                return
            m.eff = new
            if new.audio and new.audio_key != old.audio_key and m.audio is not None:
                await self._restart_audio(m)
            if new.target_delay_ms != old.target_delay_ms:
                for s in (m.video, m.audio):
                    if s:
                        s.set_target_delay(new.target_delay_ms)
            if new.bitrate != old.bitrate or new.gop_s != old.gop_s:
                m.pacer.set_rate(max(MIN_PACE_BPS, 4 * new.bitrate))
                self._kf_last = time.monotonic()
                await m.vcap.restart(bitrate=new.bitrate, gop_s=new.gop_s)

    async def switch_source(self, source: Source) -> None:
        async with self._op_lock:
            self.source = source
            m = self._media
            if m is None or self._stopping:
                return
            geom = clamp_to_screen(source.geometry, await screen_size())
            if output_size(geom[2], geom[3], m.eff.max_height) == m.out_size:
                m.geometry = geom
                self._kf_last = time.monotonic()
                await m.vcap.restart(geometry=geom)
            else:
                await self._renegotiate()
            self._restart_follower()

    @property
    def _pin_key(self) -> str:
        return self.device.id or self.device.ip

    # ---- state helpers ---------------------------------------------------------------
    def _set_state(self, state: str, detail: Optional[str] = None) -> None:
        self.state = state
        log.info("mirror state: %s%s", state, f" ({detail})" if detail else "")
        try:
            self.on_state(state, detail)
        except Exception:  # noqa: BLE001
            log.exception("on_state callback failed")

    @staticmethod
    def _to_airlinx(e: BaseException) -> AirlinxError:
        if isinstance(e, AirlinxError):
            return e
        if isinstance(e, (OSError, asyncio.TimeoutError)) and not isinstance(e, ConnectionError):
            return TvNotFoundError(str(e) or type(e).__name__)
        return AirlinxError("mirror_failed", f"Nie udało się rozpocząć mirroringu: {e}")

    # ---- control channel -------------------------------------------------------------
    async def _connect_and_launch(self, force: bool, reconnect: bool = False) -> None:
        port = self.device.cast_port or 8009
        try:
            ch = await CastChannel.open(self.device.ip, port, pin_key=self._pin_key)
        except (OSError, asyncio.TimeoutError) as e:
            raise TvNotFoundError(f"{self.device.ip}:{port}: {e or type(e).__name__}") from e
        self._ch = ch
        ch.add_listener(self._on_cast_message)
        asyncio.create_task(self._watch_channel(ch))
        self._checkpoint()
        running = await ch.get_running_app()
        self._checkpoint()
        if reconnect:
            # Only re-attach to OUR session; never LAUNCH again (would wake a TV in standby).
            sid = (self._app or {}).get("sessionId")
            if not (running and running.get("appId") == MIRRORING_APP_ID and sid
                    and running.get("sessionId") == sid and running.get("transportId")):
                raise _AppGone()
            self._app = running
            await ch.connect_virtual(running["transportId"])
            log.info("re-attached to session %s", sid)
            return
        if running and not force:
            raise TvBusyError(running.get("displayName") or running.get("appId") or "")
        self._set_state("waiting_tv")

        def progress(status: str) -> None:
            log.info("launch progress: %s", status)
            if "PENDING" in status.upper():
                self._set_state("waiting_tv", PermissionPendingError().message_pl)

        await _cancel_launch_guard(self._pin_key)   # it would STOP the session we are launching
        self._launch_sent = True
        self._app_pending = True
        try:
            app = await ch.launch(MIRRORING_APP_ID, timeout=45, on_progress=progress)
        except CastError as e:
            if "DENIED" in str(e).upper() or "CANCEL" in str(e).upper():
                self._app_pending = False
                raise AirlinxError("tv_denied", "Telewizor odrzucił połączenie.") from e
            raise
        self._app_pending = False
        self._app = app
        log.info("launched %s session=%s transport=%s", app.get("displayName"), app.get("sessionId"),
                 app.get("transportId"))
        self._checkpoint()
        await ch.connect_virtual(app["transportId"])

    async def _watch_channel(self, ch: CastChannel) -> None:
        await ch.closed.wait()
        if ch is self._ch and not self._stopping and not self._suppress_channel_events:
            self._events.put_nowait(("channel_lost", ch.close_reason))

    def _on_cast_message(self, m: castv2.CastMessage, p: Optional[dict]) -> None:
        if p is None or self._app is None:
            return
        sid, tid = self._app.get("sessionId"), self._app.get("transportId")
        if m.namespace == NS_RECEIVER and p.get("type") == "RECEIVER_STATUS":
            apps = (p.get("status") or {}).get("applications") or []
            if not any(a.get("sessionId") == sid for a in apps):
                self._events.put_nowait(("app_gone", None))
        elif m.namespace == NS_CONNECTION and p.get("type") == "CLOSE" and m.source_id == tid:
            self._events.put_nowait(("app_gone", None))
        elif m.namespace == NS_WEBRTC and p.get("type") not in ("ANSWER",):
            log.debug("webrtc message: %s", p)

    async def _close_channel(self) -> None:
        ch, self._ch = self._ch, None
        if ch is not None:
            self._suppress_channel_events = True
            try:
                await ch.close()
            finally:
                self._suppress_channel_events = False

    async def _stop_app(self) -> None:
        app, self._app = self._app, None
        if not app or not app.get("sessionId"):
            return
        sid = app["sessionId"]
        try:
            ch = self._ch
            if ch is None or not ch.alive:
                ch = await self._open_aux_channel()
                try:
                    await ch.stop_app(sid, timeout=4)
                finally:
                    await ch.close()
            else:
                await ch.stop_app(sid, timeout=4)
            log.info("STOP sent for session %s", sid)
        except Exception as e:  # noqa: BLE001
            log.warning("STOP failed: %s", e)

    # ---- media -----------------------------------------------------------------------
    async def _start_media(self) -> None:
        assert self._ch is not None and self._app is not None
        eff = effective_options(self.opts)
        geom = clamp_to_screen(self.source.geometry, await screen_size())
        self._checkpoint()
        if geom[2] < 2 or geom[3] < 2:
            raise AirlinxError("bad_source", "Wybrane źródło jest poza ekranem lub za małe.")
        out_w, out_h = output_size(geom[2], geom[3], eff.max_height)
        caps = await probe_encoders()
        self._checkpoint()
        pref = eff.codec
        exclude: set[str] = set()
        if self._no_hevc or (pref == "auto" and self._pin_key in _hevc_bad):
            exclude.add("hevc")
        tv_codecs: Optional[set[str]] = None
        if pref in ("auto", "hevc") and "hevc" not in exclude and hevc_encoder_from_caps(caps) is not None:
            tv_codecs = await self._query_tv_codecs()
            self._checkpoint()
        plan = plan_video(pref, caps, tv_codecs, exclude)
        log.info("video plan: %s (preference %s, TV codecs %s)", [(c, e) for c, e, _ in plan], pref,
                 sorted(tv_codecs) if tv_codecs is not None else "unknown")
        want_audio = eff.audio
        if want_audio and eff.audio_key == ("system",):
            want_audio = bool(await default_audio_monitor())   # no sound server → no audio stream at all
            self._checkpoint()
            if not want_audio:
                log.warning("no audio monitor source found; mirroring without audio")
        vkeys = StreamKeys.random()
        akeys = StreamKeys.random() if want_audio else None
        info: Optional[AnswerInfo] = None
        for n, (codec, encoder, gpu) in enumerate(plan):
            offer = build_offer(video=vkeys, audio=akeys, width=out_w, height=out_h, fps=eff.fps,
                                bitrate=eff.bitrate, target_delay_ms=eff.target_delay_ms, codec=codec)
            log.info("OFFER %s %dx%d@%d %d b/s delay=%d audio=%s", codec, out_w, out_h, eff.fps, eff.bitrate,
                     eff.target_delay_ms, bool(akeys))
            try:
                ans = await self._ch.request(NS_WEBRTC, offer, self._app["transportId"], id_field="seqNum",
                                             timeout=10)
                self._checkpoint()
                info = parse_answer(ans)
            except ChannelClosed:
                raise
            except (CastError, asyncio.TimeoutError) as e:
                if n + 1 >= len(plan):
                    raise
                log.warning("receiver did not accept %s (%s); offering %s", codec, e, plan[n + 1][0])
                self._notice(f"Telewizor nie przyjął {CODEC_NAMES[codec]}. Używam {CODEC_NAMES[plan[n + 1][0]]}.")
                continue
            break
        assert info is not None
        can_fall_back = n + 1 < len(plan)
        log.info("ANSWER port=%d ssrcs=%s display=%s (%s via %s)", info.udp_port, info.receiver_ssrcs,
                 info.display, codec, encoder)
        if akeys and 1 not in info.receiver_ssrcs:
            log.warning("receiver rejected the audio stream")
            akeys = None

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        with contextlib.suppress(OSError):  # DSCP AF41 (interactive video)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x88)
        sock.bind(("0.0.0.0", 0))
        sock.setblocking(False)
        sock.connect((self.device.ip, info.udp_port))
        holder: dict = {}
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(lambda d, rx: self._on_datagram(holder.get("m"), d, rx)), sock=sock)
        if self._stopping:
            transport.close()
            raise _Stopped()

        origin = time.monotonic() - 1.0   # shared session zero (audio cap may precede "now")
        video = StreamSender(kind="video", ssrc=vkeys.ssrc, receiver_ssrc=info.receiver_ssrcs[0], pt=VIDEO_PT,
                             key=vkeys.key, iv_mask=vkeys.iv, timebase=90000, origin=origin,
                             target_delay_ms=eff.target_delay_ms, seq_start=random.randint(0, 0xFFFF))
        audio = None
        if akeys:
            audio = StreamSender(kind="audio", ssrc=akeys.ssrc, receiver_ssrc=info.receiver_ssrcs[1], pt=AUDIO_PT,
                                 key=akeys.key, iv_mask=akeys.iv, timebase=48000, origin=origin,
                                 target_delay_ms=eff.target_delay_ms, seq_start=random.randint(0, 0xFFFF))
        router = RtcpRouter()
        router.add(video)
        if audio:
            router.add(audio)
        pacer = Pacer(max(MIN_PACE_BPS, 4 * eff.bitrate))
        wake = asyncio.Event()
        params = VideoParams(geometry=geom, out_w=out_w, out_h=out_h, fps=eff.fps, bitrate=eff.bitrate,
                             gop_s=eff.gop_s, encoder=encoder, gpu_scale=gpu)
        vcap = VideoCapture(params, lambda fr: self._on_video_frame(holder.get("m"), fr),
                            lambda msg: self._on_video_fatal(holder.get("m"), msg))
        m = _Media(transport=transport, video=video, audio=audio, router=router, pacer=pacer, wake=wake,
                   vcap=vcap, acap=None, eff=eff, out_size=(out_w, out_h), geometry=geom,
                   started=time.monotonic(), codec=codec, can_fall_back=can_fall_back and codec == "hevc")
        holder["m"] = m
        self._media = m
        m.tasks += [asyncio.create_task(self._pacer_loop(m), name="mirror-pacer"),
                    asyncio.create_task(self._sr_loop(m), name="mirror-sr"),
                    asyncio.create_task(self._stats_loop(m), name="mirror-stats")]
        try:
            await vcap.start()
            self._checkpoint()
            if audio is not None:
                await self._start_audio(m)
            self._checkpoint()
        except OSError as e:
            raise AirlinxError("ffmpeg_missing", f"Nie można uruchomić ffmpeg: {e}") from e

    async def _query_tv_codecs(self) -> Optional[set[str]]:
        """Ask the TV which media types it decodes (``remoting`` GET_CAPABILITIES; the reply arrives on
        ``webrtc``). None when it does not answer in time: callers then stay on the proven codec."""
        ch, app = self._ch, self._app
        assert ch is not None and app is not None
        waiter = asyncio.ensure_future(ch.wait_for(
            lambda m, p: p is not None and isinstance(p.get("capabilities"), dict), timeout=CAPS_TIMEOUT))
        try:
            await asyncio.sleep(0)   # let the waiter register its listener
            await ch.send(NS_REMOTING, {"type": "GET_CAPABILITIES", "seqNum": ch.next_id()}, app["transportId"])
            _, p = await waiter
            return parse_tv_codecs(p)
        except ChannelClosed:
            raise
        except (CastError, asyncio.TimeoutError, OSError) as e:
            log.info("TV capabilities not available: %s", e)
            return None
        finally:
            waiter.cancel()

    def _hevc_failed(self, m: _Media, why: str) -> bool:
        """HEVC does not work on this TV/PC: remember it and ask for a reconnect that uses H.264.
        False when there is no H.264 plan B (the caller treats it as an ordinary failure)."""
        if m.codec != "hevc" or not m.can_fall_back:
            return False
        log.warning("HEVC failed (%s); falling back to H.264", why)
        self._no_hevc = True
        _hevc_bad.add(self._pin_key)
        self._notice("H.265 nie działa z tym telewizorem. Przełączam na H.264.")
        return True

    def _on_video_fatal(self, m: Optional[_Media], msg: str) -> None:
        if m is not None and m is self._media and self._hevc_failed(m, msg):
            self._events.put_nowait(("stall", f"HEVC encoder failed: {msg}"))
        else:
            self._events.put_nowait(("fatal", msg))

    async def _stop_media(self) -> None:
        m, self._media = self._media, None
        if m is None:
            return
        await asyncio.gather(m.vcap.stop(), self._stop_audio(m), return_exceptions=True)
        for t in m.tasks:
            t.cancel()
        for t in m.tasks:
            with contextlib.suppress(BaseException):
                await t
        m.pacer.clear()
        m.transport.close()

    # ---- audio ------------------------------------------------------------------------
    def _notice(self, msg: str) -> None:
        log.info("notice: %s", msg)
        if self.on_notice:
            try:
                self.on_notice(msg)
            except Exception:  # noqa: BLE001
                log.exception("on_notice callback failed")

    async def _start_audio(self, m: _Media) -> None:
        """Open the audio pipeline (routing for app mode) and start the Opus capture into ``m.audio``."""
        spec = spec_from(self.opts)
        pipe = AudioPipeline(spec, monitor=default_audio_monitor, on_notice=self._notice)
        m.pipeline = pipe   # registered before open(): _stop_media closes it whatever happens next
        sources = await pipe.open()
        if m is not self._media or m.pipeline is not pipe:
            await _shielded(pipe.close())
            return
        if not sources:
            log.warning("no audio source available; continuing without audio")
            return
        acap = AudioCapture(sources, lambda pk, cap: self._on_audio_packet(m, pk, cap),
                            lambda msg: self._on_audio_fatal(m, msg), bitrate=AUDIO_BITRATE)
        m.acap = acap
        await acap.start()
        log.info("audio: %s from %s", spec.source, sources)

    async def _stop_audio(self, m: _Media) -> None:
        acap, m.acap = m.acap, None
        pipe, m.pipeline = m.pipeline, None
        try:
            if acap is not None:
                await acap.stop()
        finally:
            if pipe is not None:
                await _shielded(pipe.close())

    async def _restart_audio(self, m: _Media) -> None:
        """New audio source on the running stream: same RTP sender, new capture (short gap)."""
        log.info("audio source change -> restarting audio capture only")
        await self._stop_audio(m)
        if m is self._media and not self._stopping:
            await self._start_audio(m)

    def _on_audio_fatal(self, m: _Media, msg: str) -> None:
        log.error("audio disabled: %s", msg)
        self._notice("Dźwięk na TV przestał działać. Obraz leci dalej.")
        if m.acap is not None or m.pipeline is not None:   # never leave an app muted on the PC
            asyncio.ensure_future(self._stop_audio(m))

    async def _teardown(self, stop_app: bool) -> None:
        await self._stop_media()
        if stop_app:
            await self._stop_app()
        else:
            self._app = None
        await self._close_channel()

    async def _renegotiate(self) -> None:
        """New OFFER on the same app session (resolution / fps / audio change)."""
        log.info("renegotiating stream")
        await self._stop_media()
        try:
            self._checkpoint()
            await self._start_media()
        except _Stopped:
            await _shielded(self._stop_media())
        except BaseException as e:
            await _shielded(self._stop_media())
            if isinstance(e, asyncio.CancelledError):
                raise
            log.warning("renegotiation failed: %s", e)
            self._events.put_nowait(("stall", f"renegotiation failed: {e}"))

    # ---- media callbacks -------------------------------------------------------------
    def _on_video_frame(self, m: Optional[_Media], fr: EncodedFrame) -> None:
        if m is None or m is not self._media:
            return
        pkts = m.video.build_frame(fr.data, fr.key, fr.cap, now=fr.arrival)
        if pkts:
            m.pacer.push(pkts)
            m.wake.set()
        if m.video.keyframe_wanted:
            self._request_keyframe(m)

    def _on_audio_packet(self, m: Optional[_Media], pkt: bytes, cap: float) -> None:
        if m is None or m is not self._media or m.audio is None:
            return
        pkts = m.audio.build_frame(pkt, True, cap)
        if pkts:
            m.pacer.push(pkts, priority=True)
            m.wake.set()

    def _on_datagram(self, m: Optional[_Media], data: bytes, rx: float) -> None:
        if m is None or m is not self._media:
            return
        routed = m.router.route(data)
        if routed is None:
            return
        sender, r = routed
        resend = sender.handle_rtcp(r, rx)
        if resend:
            m.pacer.push(resend, priority=True)
            m.wake.set()
        if sender is m.video:
            if not m.first_ack and sender.checkpoint >= 0:
                m.first_ack = True
                self._set_state("live")
            if sender.keyframe_wanted:
                self._request_keyframe(m)

    def _request_keyframe(self, m: _Media) -> None:
        now = time.monotonic()
        if now - self._kf_last < KEYFRAME_MIN_INTERVAL or (self._kf_task and not self._kf_task.done()):
            return
        m.video.keyframe_wanted = False
        self._kf_last = now
        log.info("keyframe requested (pli=%d lag=%d) -> encoder restart", m.video.st["pli"], m.video.lag)
        self._kf_task = asyncio.create_task(m.vcap.restart())

    # ---- loops -----------------------------------------------------------------------
    async def _pacer_loop(self, m: _Media) -> None:
        tr = m.transport
        while True:
            pkts = m.pacer.pop_ready()
            if pkts:
                now = time.monotonic()
                for p in pkts:
                    tr.sendto(p.data)
                    p.sender.on_sent(p, now)
            d = m.pacer.delay_until_next()
            if d is None:
                m.wake.clear()
                await m.wake.wait()
            elif d <= 0:
                await asyncio.sleep(0)
            else:
                await asyncio.sleep(max(d, 0.0005))

    async def _sr_loop(self, m: _Media) -> None:
        while True:
            for s in (m.video, m.audio):
                if s is not None and s.sr_started:
                    m.transport.sendto(s.sender_report())
            await asyncio.sleep(SR_INTERVAL)

    async def _stats_loop(self, m: _Media) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL)
            now = time.monotonic()
            last = m.video.last_rtcp
            if m.first_ack and last is not None and now - last > STALL_TIMEOUT_LIVE:
                self._events.put_nowait(("stall", f"no RTCP for {now - last:.1f}s"))
                return
            if not m.first_ack and now - m.started > STALL_TIMEOUT_START:
                self._hevc_failed(m, "no feedback from the receiver")
                self._events.put_nowait(("stall", "no feedback from receiver"))
                return
            try:
                self.on_stats(self._make_stats(m, now))
            except Exception:  # noqa: BLE001
                log.exception("on_stats callback failed")

    def _make_stats(self, m: _Media, now: float) -> Stats:
        v = m.video.snapshot(now)
        a = m.audio.snapshot(now) if m.audio else None
        target = float(v["playout_delay_ms"] or m.eff.target_delay_ms)
        rtt = v["rtt_ms"]
        fps = v["fps"]
        cap_ms = 500.0 / max(1, m.eff.fps) + CAPTURE_GRAB_MS
        # capture-estimate → first packet on the wire: encoder latency + pacing
        enc_ms = v["cap2send_ms"] if v["cap2send_ms"] is not None else (m.vcap.encode_latency_ms or 0.0)
        send2ack = v["send2ack_ms"]
        half_rtt = (rtt or 0.0) / 2
        net_ms = max(half_rtt, (send2ack - half_rtt) if send2ack is not None else half_rtt)
        tv_ms = max(0.0, target - enc_ms - net_ms)
        latency = cap_ms + enc_ms + net_ms + tv_ms if m.first_ack else None
        loss = v["fraction_lost"] if v["fraction_lost"] is not None else v["retx_ratio"]
        w, h = m.out_size
        return Stats(latency_ms=latency, fps=round(fps, 1),
                     bitrate=v["bitrate"] + (a["bitrate"] if a else 0.0), width=w, height=h,
                     rtt_ms=rtt, loss=loss, protocol="cast",
                     encoder=m.vcap.encoder_label,
                     stages={"capture": round(cap_ms, 1), "encode": round(enc_ms, 1),
                             "network": round(net_ms, 1), "tv": round(tv_ms, 1)})

    # ---- supervision -----------------------------------------------------------------
    async def _supervise(self) -> None:
        while True:
            kind, detail = await self._events.get()
            if self._stopping:
                return
            log.info("supervisor event: %s %s", kind, detail or "")
            if kind == "app_gone":
                await self._teardown(stop_app=False)
                self._set_state("stopped", APP_GONE_PL)
                return
            if kind == "fatal":
                await self._teardown(stop_app=True)
                self._set_state("error", detail)
                return
            if kind in ("channel_lost", "stall"):
                if not await self._reconnect(detail):
                    return
                self._events = asyncio.Queue()  # drop stale events from the old session
                if self._window_gone:           # ...but not the end of the shared window
                    self._events.put_nowait(("fatal", WINDOW_GONE_PL))

    async def _reconnect(self, reason: Optional[str]) -> bool:
        """Re-attach to our running session (new channel + new OFFER). Never re-LAUNCHes."""
        self._set_state("reconnecting", reason)
        for delay in RECONNECT_BACKOFF:
            if self._stopping:
                return False
            await self._stop_media()
            await self._close_channel()   # keep self._app: we re-attach to the same session
            await asyncio.sleep(delay)
            try:
                await self._connect_and_launch(force=False, reconnect=True)
                if await self._window_missing():
                    self._window_gone = True
                    await self._teardown(stop_app=True)
                    self._set_state("error", WINDOW_GONE_PL)
                    return False
                await self._start_media()
                return True
            except _Stopped:
                await _shielded(self._stop_media())
                return False
            except _AppGone:
                log.info("our mirroring session is gone; not relaunching")
                await self._teardown(stop_app=False)
                self._set_state("stopped", APP_GONE_PL)
                return False
            except AirlinxError as e:
                if e.code in NO_RETRY_CODES:
                    await self._teardown(stop_app=False)
                    self._set_state("error", e.message_pl)
                    return False
                log.warning("reconnect attempt failed: %s", e)
            except Exception as e:  # noqa: BLE001
                log.warning("reconnect attempt failed: %s", e)
        await self._teardown(stop_app=True)
        self._set_state("error", "Utracono połączenie z telewizorem.")
        return False

    # ---- window following ------------------------------------------------------------
    def _restart_follower(self) -> None:
        if self._follower and not self._follower.done():
            self._follower.cancel()
        self._follower = None
        if self.source.kind == "window" and self.source.xid:
            self._follower = asyncio.create_task(self._follow_window(self.source.xid), name="mirror-follow")

    async def _window_missing(self) -> bool:
        """The shared window is gone (closed/minimised): capturing its rectangle would leak the screen."""
        if self._window_gone:
            return True
        if self.source.kind != "window" or not self.source.xid:
            return False
        return await window_geometry(self.source.xid) is None

    async def _follow_window(self, xid: int) -> None:
        missing = 0
        while not self._stopping:
            await asyncio.sleep(WINDOW_POLL)
            m = self._media
            if m is None:
                continue
            g = await window_geometry(xid)
            if g is None:
                # closed or minimised: x11grab would keep sending whatever lies under the window
                missing += 1
                if missing >= WINDOW_GONE_POLLS:
                    self._window_gone = True
                    self._events.put_nowait(("fatal", WINDOW_GONE_PL))
                    return
                continue
            missing = 0
            g = clamp_to_screen(g, await screen_size())
            if g != m.geometry and g[2] >= 2 and g[3] >= 2 and self._media is m:
                log.info("window %#x moved/resized %s -> %s", xid, m.geometry, g)
                m.geometry = g
                self.source = dataclasses.replace(self.source, geometry=g)
                self._kf_last = time.monotonic()
                # output size stays fixed for the session (resize → scaled into the same frame)
                await m.vcap.restart(geometry=g)
