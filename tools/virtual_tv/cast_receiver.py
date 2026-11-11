"""Fake Cast receiver (the TV side of CASTv2): control channel, mirroring, Default Media Receiver.

What it speaks (verified against a real Samsung S85F, see spikes/cast_streaming/PROTOCOL.md):

* TLS on the chosen port with a throw-away self-signed certificate (<= 4 days, like Cast);
* ``tp.connection`` / ``tp.heartbeat`` / ``tp.deviceauth`` (AuthResponse signed by the **test** PKI);
* ``receiver``: GET_STATUS, LAUNCH (0F5096E8 Chrome Mirroring, CC1AD845 Default Media Receiver), STOP,
  SET_VOLUME, with LAUNCH_STATUS (USER_PENDING / USER_ALLOWED);
* ``webrtc``: OFFER -> ANSWER (UDP port, receiver SSRCs), media plane in :mod:`.cast_media`;
* ``media`` (Default Media Receiver): LOAD (downloads the URL), PLAY, PAUSE, SEEK, STOP, GET_STATUS.

Failure modes (``fail=``): ``auth`` (bad device-auth signature), ``cert`` (device identity changes from
the second connection on), ``busy`` (YouTube is running), ``noport`` (nothing listens on the Cast port),
``hang`` (TCP accepted, nothing answered), ``pending`` (LAUNCH waits for the user's consent).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp
from aiohttp import web

from airlinx.core.cast import castv2 as cv  # codec + auth framing only (shared wire format)

from . import pki as pkimod
from .cast_media import AUDIO_PT, VIDEO_PT, MediaPlane, StreamSpec
from .sinks import FfplaySink, StreamRecorder

log = logging.getLogger("vtv.cast")

NS_CONNECTION, NS_HEARTBEAT, NS_RECEIVER = cv.NS_CONNECTION, cv.NS_HEARTBEAT, cv.NS_RECEIVER
NS_MEDIA, NS_WEBRTC, NS_REMOTING, NS_DEVICEAUTH = cv.NS_MEDIA, cv.NS_WEBRTC, cv.NS_REMOTING, cv.NS_DEVICEAUTH
MIRRORING_APP = "0F5096E8"
DMR_APP = "CC1AD845"
YOUTUBE_APP = "233637DE"
FAIL_MODES = ("auth", "cert", "busy", "noport", "hang", "pending")
CHROME_UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 "
             "Safari/537.36 CrKey/1.56.500000")


@dataclass
class Conn:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    index: int
    sender_id: str = "sender-0"
    virtual: set = field(default_factory=set)
    peer: str = ""


@dataclass
class Event:
    t: float
    kind: str
    data: dict


class CastReceiver:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8009, name: str = "Wirtualny TV",
                 model: str = "Virtual TV (AirLinx test)", device_id: Optional[str] = None,
                 fail: Optional[str] = None, out_dir: Optional[Path] = None, show: bool = False,
                 pki: Optional[pkimod.TestPki] = None, pending_s: float = 5.0, launch_delay: float = 0.3,
                 eureka_port: Optional[int] = None, drop: Optional[Callable] = None,
                 media_realtime: bool = True) -> None:
        if fail is not None and fail not in FAIL_MODES:
            raise ValueError(f"unknown failure mode {fail!r}; one of {FAIL_MODES}")
        self.host, self.port, self.name, self.model = host, port, name, model
        self.device_id = device_id or uuid.uuid4().hex
        self.fail = fail
        self.out_dir = out_dir
        self.show = show
        self.pki = pki or pkimod.TestPki(f"VTV-{self.device_id[:8].upper()}")
        self.pending_s = pending_s
        self.launch_delay = launch_delay
        self.eureka_port = eureka_port
        self.drop = drop
        self.media_realtime = media_realtime
        self.tls = pkimod.TlsIdentity.generate(self.device_id, (host,) if host[0].isdigit() else ())
        self._alt_identity: Optional[pkimod.DeviceIdentity] = None
        self.identity = self.pki.identity
        self.server: Optional[asyncio.base_events.Server] = None
        self._eureka: Optional[web.AppRunner] = None
        self.conns: list[Conn] = []
        self.connections_seen = 0
        self.events: list[Event] = []
        self.received: list[tuple[str, str, str, Any]] = []    # (namespace, src, dst, payload)
        self.app: Optional[dict] = (
            self._app_entry(YOUTUBE_APP, "YouTube") if fail == "busy" else None)
        self.volume = 0.5
        self.muted = False
        self.plane: Optional[MediaPlane] = None
        self.planes: list[MediaPlane] = []
        self.recorder = StreamRecorder(out_dir, show)
        self.offers: list[dict] = []
        self.answers: list[dict] = []
        self.dmr: Optional["DmrSession"] = None
        self.dmr_sessions: list["DmrSession"] = []
        self._consent: Optional[asyncio.Future] = None
        self.consent_requested = asyncio.Event()
        self._bg: set[asyncio.Task] = set()
        self._media_counter = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> "CastReceiver":
        if self.fail == "noport":
            if not self.port:           # a port number that is free right now (and so refuses connections)
                import socket
                with socket.socket() as probe:
                    probe.bind((self.host, 0))
                    self.port = probe.getsockname()[1]
        elif self.fail == "hang":
            self.server = await asyncio.start_server(self._hang, self.host, self.port)
            self.port = self.server.sockets[0].getsockname()[1]
        else:
            self.server = await asyncio.start_server(self._client, self.host, self.port, ssl=self.tls.server_context())
            self.port = self.server.sockets[0].getsockname()[1]
        if self.eureka_port is not None:
            await self._start_eureka()
        self._ev("start", host=self.host, port=self.port, fail=self.fail)
        return self

    async def stop(self) -> None:
        if self.fail == "pending" and self._consent and not self._consent.done():
            self._consent.set_result(True)
        for t in list(self._bg):
            t.cancel()
        for t in list(self._bg):
            with contextlib.suppress(BaseException):
                await t
        self._teardown_app()
        for c in list(self.conns):
            c.writer.close()
        if self.server:
            self.server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.wait_closed(), 2)
        if self._eureka:
            await self._eureka.cleanup()
        self.recorder.close()

    async def _hang(self, reader, writer) -> None:  # noqa: ANN001
        self._ev("hang_accept")
        with contextlib.suppress(BaseException):
            await asyncio.sleep(3600)

    async def _start_eureka(self) -> None:
        async def info(request: web.Request) -> web.Response:
            return web.json_response({"name": self.name, "ssdp_udn": str(uuid.UUID(self.device_id)),
                                      "device_info": {"model_name": self.model, "ssdp_udn": str(uuid.UUID(self.device_id))}})
        app = web.Application()
        app.router.add_get("/setup/eureka_info", info)
        self._eureka = web.AppRunner(app, access_log=None)
        await self._eureka.setup()
        site = web.TCPSite(self._eureka, self.host, self.eureka_port)
        await site.start()
        self.eureka_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    # ------------------------------------------------------------------ helpers
    def _ev(self, kind: str, **data: Any) -> None:
        self.events.append(Event(time.time(), kind, data))
        log.info("%s %s", kind, data if data else "")
        if self.out_dir:      # machine-readable trail, also for a TV running in another process (netns scenario)
            with contextlib.suppress(OSError):
                self.out_dir.mkdir(parents=True, exist_ok=True)
                with open(self.out_dir / "events.jsonl", "a") as f:
                    f.write(json.dumps({"t": time.time(), "kind": kind, **data}, default=str) + "\n")

    def events_of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    async def wait_for(self, pred: Callable[[], bool], timeout: float = 10.0, what: str = "condition") -> None:
        end = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > end:
                raise asyncio.TimeoutError(f"virtual TV: timeout waiting for {what}")
            await asyncio.sleep(0.02)

    def spawn(self, coro) -> asyncio.Task:  # noqa: ANN001
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    @staticmethod
    def _app_entry(app_id: str, display: str) -> dict:
        sid = str(uuid.uuid4())
        namespaces = {
            MIRRORING_APP: [NS_WEBRTC, NS_REMOTING, NS_MEDIA],
            DMR_APP: [NS_MEDIA, NS_CONNECTION, NS_HEARTBEAT],
        }.get(app_id, [NS_MEDIA])
        return {"appId": app_id, "displayName": display, "namespaces": [{"name": n} for n in namespaces],
                "sessionId": sid, "statusText": "Casting" if app_id == MIRRORING_APP else "",
                "transportId": sid if app_id == MIRRORING_APP else f"web-{sid[:6]}",
                "universalAppId": app_id, "isIdleScreen": False, "launchedFromCloud": False}

    def receiver_status(self, rid: int = 0) -> dict:
        return {"type": "RECEIVER_STATUS", "requestId": rid,
                "status": {"applications": [self.app] if self.app else [], "isActiveInput": True,
                           "volume": {"level": self.volume, "muted": self.muted, "controlType": "master",
                                      "stepInterval": 0.05}}}

    def _write(self, c: Conn, ns: str, payload: Any, src: str = "receiver-0", binary: bool = False) -> None:
        if binary:
            m = cv.CastMessage(src, c.sender_id, ns, payload_binary=payload)
        else:
            m = cv.CastMessage(src, c.sender_id, ns, payload_utf8=json.dumps(payload, separators=(",", ":")))
        with contextlib.suppress(Exception):
            c.writer.write(cv.frame_message(m))

    def broadcast(self, ns: str, payload: dict, src: str = "receiver-0", dst_virtual: Optional[str] = None) -> None:
        for c in list(self.conns):
            if dst_virtual is None or dst_virtual in c.virtual or "receiver-0" in c.virtual:
                self._write(c, ns, payload, src)

    # ------------------------------------------------------------------ connection loop
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections_seen += 1
        c = Conn(reader, writer, self.connections_seen, peer=str((writer.get_extra_info("peername") or ("?",))[0]))
        self.conns.append(c)
        self._ev("connection", index=c.index, peer=c.peer)
        try:
            while True:
                (ln,) = struct.unpack(">I", await reader.readexactly(4))
                m = cv.decode_message(await reader.readexactly(ln))
                c.sender_id = m.source_id or c.sender_id
                await self._dispatch(c, m)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.CancelledError) as e:
            log.debug("connection %d ended: %r", c.index, e)
        except Exception:  # noqa: BLE001
            log.exception("receiver connection failed")
        finally:
            with contextlib.suppress(ValueError):
                self.conns.remove(c)
            self._ev("disconnect", index=c.index)
            with contextlib.suppress(Exception):
                writer.close()

    async def _dispatch(self, c: Conn, m: cv.CastMessage) -> None:
        p = m.json()
        if m.namespace != NS_HEARTBEAT:
            self.received.append((m.namespace, m.source_id, m.destination_id, p if p is not None else "<binary>"))
        if m.namespace == NS_HEARTBEAT:
            if p and p.get("type") == "PING":
                self._write(c, NS_HEARTBEAT, {"type": "PONG"}, m.destination_id)
        elif m.namespace == NS_CONNECTION:
            if p and p.get("type") == "CONNECT":
                c.virtual.add(m.destination_id)
            elif p and p.get("type") == "CLOSE":
                c.virtual.discard(m.destination_id)
        elif m.namespace == NS_DEVICEAUTH:
            self._on_deviceauth(c, m)
        elif p is None:
            return
        elif m.namespace == NS_RECEIVER:
            await self._on_receiver(c, p)
        elif m.namespace == NS_WEBRTC:
            await self._on_webrtc(c, m, p)
        elif m.namespace == NS_REMOTING:
            if p.get("type") == "GET_CAPABILITIES":     # answered on the webrtc namespace, as on the real TV
                self._write(c, NS_WEBRTC, {"type": "CAPABILITIES_RESPONSE", "seqNum": p.get("seqNum"), "result": "ok",
                            "capabilities": {"mediaCaps": ["aac", "opus", "vp9", "h264", "hevc", "vp8", "audio"],
                                             "remoting": 2}}, m.destination_id)
        elif m.namespace == NS_MEDIA:
            await self._on_media(c, m, p)

    # ------------------------------------------------------------------ device-auth
    def _identity_for(self, index: int) -> pkimod.DeviceIdentity:
        if self.fail == "cert" and index >= 2:
            if self._alt_identity is None:
                self._alt_identity = self.pki.new_identity(f"VTV-OTHER-{uuid.uuid4().hex[:6].upper()}")
            return self._alt_identity
        return self.identity

    def swap_identity(self) -> None:
        """What a replaced (or impersonated) TV looks like: same address, another device certificate."""
        self.identity = self.pki.new_identity(f"VTV-SWAPPED-{uuid.uuid4().hex[:6].upper()}")

    def rotate_tls(self) -> None:
        """New self-signed TLS certificate for future connections (Cast receivers do this every few days)."""
        self.tls = pkimod.TlsIdentity.generate(self.device_id, (self.host,) if self.host[0].isdigit() else ())
        if self.server is not None and self.fail not in ("hang", "noport"):
            raise RuntimeError("rotate_tls() must be called before start() or the server restarted")

    def _on_deviceauth(self, c: Conn, m: cv.CastMessage) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        if m.payload_binary is None:
            return
        try:
            ch = cv._pb_fields(cv._pb_fields(m.payload_binary)[1][0])
            nonce = bytes(ch[2][0])
            hash_alg = ch.get(3, [cv.HASH_SHA256])[-1]
        except (KeyError, IndexError, ValueError):
            self._ev("deviceauth_malformed")
            return
        ident = self._identity_for(c.index)
        h = hashes.SHA256() if hash_alg == cv.HASH_SHA256 else hashes.SHA1()
        sig = ident.key.sign(nonce + self.tls.der, padding.PKCS1v15(), h)
        if self.fail == "auth":
            sig = bytes([sig[0] ^ 0x01]) + sig[1:]
        resp = cv.build_auth_response(signature=sig, client_cert=pkimod.der(ident.cert),
                                      intermediates=[pkimod.der(ident.ica)], nonce=nonce, hash_algorithm=hash_alg)
        self._write(c, NS_DEVICEAUTH, resp, binary=True)
        self._ev("deviceauth", index=c.index, subject=ident.subject, tampered=self.fail == "auth")

    # ------------------------------------------------------------------ receiver namespace
    async def _on_receiver(self, c: Conn, p: dict) -> None:
        t, rid = p.get("type"), p.get("requestId", 0)
        if t == "GET_STATUS":
            self._write(c, NS_RECEIVER, self.receiver_status(rid))
        elif t == "LAUNCH":
            self.spawn(self._launch(c, rid, str(p.get("appId", ""))))
        elif t == "STOP":
            sid = p.get("sessionId")
            if self.app and self.app["sessionId"] == sid:
                self._ev("stop", app=self.app["appId"], session=sid)
                self._close_app_connections()
                self._teardown_app()
                self.broadcast(NS_RECEIVER, self.receiver_status(rid))
            else:
                self._write(c, NS_RECEIVER, {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_SESSION_ID"})
        elif t == "SET_VOLUME":
            vol = p.get("volume") or {}
            if "level" in vol:
                self.volume = max(0.0, min(1.0, float(vol["level"])))
            if "muted" in vol:
                self.muted = bool(vol["muted"])
            self.broadcast(NS_RECEIVER, self.receiver_status(rid))
        elif t == "GET_APP_AVAILABILITY":
            self._write(c, NS_RECEIVER, {"type": "GET_APP_AVAILABILITY", "requestId": rid,
                                         "availability": {a: "APP_AVAILABLE" for a in p.get("appId", [])}})

    def approve(self) -> None:
        """The user pressed OK on the TV (fail='pending')."""
        if self._consent and not self._consent.done():
            self._consent.set_result(True)

    def deny(self) -> None:
        if self._consent and not self._consent.done():
            self._consent.set_result(False)

    async def _launch(self, c: Conn, rid: int, app_id: str) -> None:
        self._ev("launch", app=app_id, running=self.app["appId"] if self.app else None)
        if app_id not in (MIRRORING_APP, DMR_APP):
            self._write(c, NS_RECEIVER, {"type": "LAUNCH_ERROR", "requestId": rid, "reason": "NOT_FOUND"})
            return
        if app_id == MIRRORING_APP and (self.fail == "pending" or self._consent is not None):
            if self._consent is None or self._consent.done():
                self._consent = asyncio.get_running_loop().create_future()
            self._write(c, NS_RECEIVER, {"type": "LAUNCH_STATUS", "requestId": rid, "status": "USER_PENDING"})
            self.consent_requested.set()
            self._ev("consent_pending")
            try:
                ok = await asyncio.wait_for(asyncio.shield(self._consent), self.pending_s if self.fail == "pending"
                                            else None)
            except asyncio.TimeoutError:
                ok = True        # the CLI auto-approves after --pending-seconds
            self._consent = None
            if not ok:
                self._ev("consent_denied")
                self._write(c, NS_RECEIVER, {"type": "LAUNCH_ERROR", "requestId": rid, "reason": "USER_DENIED"})
                return
            self._ev("consent_allowed")
        if app_id == MIRRORING_APP:
            # Only the mirroring app announces itself first. For the Default Media Receiver the first reply
            # with this requestId must be the RECEIVER_STATUS: pychromecast drops its launch callback on
            # the first answer and only proceeds when that is a RECEIVER_STATUS.
            self._write(c, NS_RECEIVER, {"type": "LAUNCH_STATUS", "requestId": rid, "status": "USER_ALLOWED"})
        await asyncio.sleep(self.launch_delay)
        if self.app:
            self._close_app_connections()
            self._teardown_app()
        name = "Chrome Mirroring" if app_id == MIRRORING_APP else "Default Media Receiver"
        self.app = self._app_entry(app_id, name)
        self._ev("launched", app=app_id, session=self.app["sessionId"])
        self.broadcast(NS_RECEIVER, self.receiver_status(rid))

    def hold_next_launch(self) -> None:
        """Programmatic variant of fail='pending': the next mirroring LAUNCH waits for approve()/deny()."""
        self._consent = asyncio.get_running_loop().create_future()

    def _close_app_connections(self) -> None:
        if not self.app:
            return
        tid = self.app["transportId"]
        for c in list(self.conns):
            if tid in c.virtual:
                c.virtual.discard(tid)
                self._write(c, NS_CONNECTION, {"type": "CLOSE"}, src=tid)

    def _teardown_app(self) -> None:
        if self.plane:
            self.plane.close()
            self.plane = None
        self.recorder.flush()
        if self.dmr:
            self.dmr.close()
            self.dmr = None
        self.app = None

    async def kill_app(self) -> None:
        """The user pressed Home on the real remote: the app disappears without any STOP from the sender."""
        if self.app:
            self._ev("app_killed", app=self.app["appId"])
            self._close_app_connections()
            self._teardown_app()
            self.broadcast(NS_RECEIVER, self.receiver_status(0))

    # ------------------------------------------------------------------ mirroring (webrtc)
    async def _on_webrtc(self, c: Conn, m: cv.CastMessage, p: dict) -> None:
        if not (self.app and self.app["appId"] == MIRRORING_APP and m.destination_id == self.app["transportId"]):
            return
        if p.get("type") != "OFFER":
            return          # GET_STATUS on webrtc gets no reply on the real TV either
        seq = p.get("seqNum")
        self.offers.append(p)
        try:
            ans = await self._build_answer(p)
        except ValueError as e:
            ans = {"type": "ANSWER", "seqNum": seq, "result": "error", "error": {"code": 1, "description": str(e)}}
            self._ev("offer_rejected", reason=str(e))
        else:
            ans["seqNum"] = seq
        self.answers.append(ans)
        self._write(c, NS_WEBRTC, ans, src=self.app["transportId"])

    async def _build_answer(self, p: dict) -> dict:
        offer = p.get("offer") or {}
        streams = offer.get("supportedStreams")
        if not isinstance(streams, list) or not streams:
            raise ValueError("OFFER without supportedStreams")
        specs: list[StreamSpec] = []
        for s in streams:
            kind = {"video_source": "video", "audio_source": "audio"}.get(s.get("type"))
            codec = {"video": "h264", "audio": "opus"}.get(kind or "")
            if kind is None or s.get("codecName") != codec:
                continue
            try:
                key, iv = bytes.fromhex(s["aesKey"]), bytes.fromhex(s["aesIvMask"])
                ssrc = int(s["ssrc"])
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"stream {s.get('index')}: bad key/ssrc ({e})") from e
            if len(key) != 16 or len(iv) != 16:
                raise ValueError("AES key and IV mask must be 16 bytes")
            if kind == "video" and any(x.kind == "video" for x in specs):
                continue
            if kind == "audio" and any(x.kind == "audio" for x in specs):
                continue
            tb = s.get("timeBase", "1/90000" if kind == "video" else "1/48000")
            specs.append(StreamSpec(index=int(s["index"]), kind=kind, ssrc=ssrc, rssrc=(ssrc + 1) & 0xFFFFFFFF,
                                    key=key, iv_mask=iv, pt=int(s.get("rtpPayloadType", VIDEO_PT if kind == "video"
                                                                       else AUDIO_PT)),
                                    timebase=int(str(tb).split("/")[1]) if "/" in str(tb) else 90000,
                                    target_delay_ms=int(s.get("targetDelay", 100))))
        if not any(s.kind == "video" for s in specs):
            raise ValueError("no supported video stream (h264 required)")
        if self.plane:                      # renegotiation on the same app session: new OFFER, new UDP plane
            self.plane.close()
        plane = MediaPlane(specs, self.recorder, host=self.host, drop=self.drop)
        port = await plane.start()
        self.plane = plane
        self.planes.append(plane)
        res = (streams[0].get("resolutions") or [{}])[0]
        self._ev("answer", udp_port=port, streams=[s.kind for s in specs], resolution=res,
                 max_bitrate=streams[0].get("maxBitRate"), target_delay=streams[0].get("targetDelay"))
        return {"type": "ANSWER", "result": "ok",
                "answer": {"display": {"dimensions": {"frameRate": "60", "height": 2160, "width": 3840},
                                       "scaling": "sender"},
                           "sendIndexes": [s.index for s in specs], "ssrcs": [s.rssrc for s in specs],
                           "udpPort": port}}

    # ------------------------------------------------------------------ media namespace
    async def _on_media(self, c: Conn, m: cv.CastMessage, p: dict) -> None:
        app = self.app
        if not app or m.destination_id != app["transportId"]:
            return
        t, rid = p.get("type"), p.get("requestId", 0)
        if app["appId"] == MIRRORING_APP:
            if t == "GET_STATUS":   # constant, independent of whether anything is decoded (PROTOCOL.md section 1.6)
                self._write(c, NS_MEDIA, {"type": "MEDIA_STATUS", "requestId": rid, "status": [{
                    "mediaSessionId": 1, "playerState": "PLAYING", "currentTime": 0, "playbackRate": 1,
                    "media": {"contentType": "video/webm"}}]}, app["transportId"])
            return
        if self.dmr is None:
            self.dmr = DmrSession(self)
            self.dmr_sessions.append(self.dmr)
        await self.dmr.handle(c, t, rid, p)

    def media_link(self, c: Optional[Conn] = None) -> None:  # pragma: no cover - placeholder for symmetry
        return None

    def stats(self) -> dict:
        d: dict = {"connections": self.connections_seen, "app": self.app["appId"] if self.app else None,
                   "offers": len(self.offers), "planes": [pl.stats() for pl in self.planes]}
        return d


# ====================================================================================
# Default Media Receiver
# ====================================================================================
@dataclass
class Fetch:
    url: str
    status: int = 0
    bytes: int = 0
    headers: dict = field(default_factory=dict)
    response_headers: dict = field(default_factory=dict)
    range: str = ""
    t_start: float = 0.0
    t_end: float = 0.0
    error: str = ""


class DmrSession:
    """State of one Default Media Receiver: loads a URL like Chrome's media stack, simulates playback."""

    def __init__(self, tv: CastReceiver) -> None:
        self.tv = tv
        self.media_session_id = 0
        self.state = "IDLE"
        self.idle_reason: Optional[str] = None
        self.media: dict = {}
        self.fetches: list[Fetch] = []
        self.position = 0.0
        self._clock_from: Optional[float] = None       # monotonic instant the position was last anchored
        self.duration: Optional[float] = None
        self.total_bytes = 0
        self.content_length = 0
        self.download_done = asyncio.Event()
        self.file_path: Optional[Path] = None
        self._task: Optional[asyncio.Task] = None
        self._end_task: Optional[asyncio.Task] = None
        self._player: Optional[FfplaySink] = None
        self.states: list[tuple[float, str, Optional[str]]] = []
        self.load_requests: list[dict] = []
        self.closed = False

    # ---- helpers -----------------------------------------------------------------------
    def current_time(self) -> float:
        if self.state == "PLAYING" and self._clock_from is not None:
            t = self.position + (time.monotonic() - self._clock_from)
            return min(t, self.duration) if self.duration else t
        return self.position

    def status(self) -> dict:
        s: dict = {"mediaSessionId": self.media_session_id, "playbackRate": 1, "playerState": self.state,
                   "currentTime": round(self.current_time(), 3), "supportedMediaCommands": 274447,
                   "volume": {"level": self.tv.volume, "muted": self.tv.muted}, "activeTrackIds": [],
                   "currentItemId": 1, "repeatMode": "REPEAT_OFF"}
        if self.media:
            m = dict(self.media)
            if self.duration:
                m["duration"] = self.duration
            s["media"] = m
        if self.state == "IDLE" and self.idle_reason:
            s["idleReason"] = self.idle_reason
        return s

    def _emit(self, rid: int = 0, c: Optional[Conn] = None) -> None:
        tid = self.tv.app["transportId"] if self.tv.app else "web"
        payload = {"type": "MEDIA_STATUS", "requestId": rid, "status": [self.status()]}
        if c is not None:
            self.tv._write(c, NS_MEDIA, payload, tid)
        else:
            self.tv.broadcast(NS_MEDIA, payload, tid, dst_virtual=tid)

    def _set(self, state: str, reason: Optional[str] = None, rid: int = 0, c: Optional[Conn] = None) -> None:
        if state == self.state and reason == self.idle_reason:
            return
        self.position = self.current_time()
        self.state, self.idle_reason = state, reason
        self._clock_from = time.monotonic() if state == "PLAYING" else None
        self.states.append((time.time(), state, reason))
        self.tv._ev("media_state", state=state, reason=reason, position=round(self.position, 2))
        self._emit(rid, c)

    def close(self) -> None:
        self.closed = True
        for t in (self._task, self._end_task):
            if t:
                t.cancel()
        if self._player:
            self._player.close()
            self._player = None

    # ---- message handling ---------------------------------------------------------------
    async def handle(self, c: Conn, t: Optional[str], rid: int, p: dict) -> None:
        tid = self.tv.app["transportId"] if self.tv.app else "web"
        if t == "GET_STATUS":
            self._emit(rid, c)
        elif t == "LOAD":
            await self._load(c, rid, p)
        elif t == "PLAY":
            if self.state in ("PAUSED", "BUFFERING"):
                self._set("PLAYING", rid=rid, c=c)
            else:
                self._emit(rid, c)
        elif t == "PAUSE":
            if self.state == "PLAYING":
                self._set("PAUSED", rid=rid, c=c)
            else:
                self._emit(rid, c)
        elif t == "SEEK":
            self.spawn_seek(c, rid, float(p.get("currentTime", 0)), p.get("resumeState"))
        elif t == "STOP":
            self._cancel_fetch()
            self.position = 0.0
            self._set("IDLE", "CANCELLED", rid, c)
        elif t == "SET_VOLUME":
            vol = p.get("volume") or {}
            if "level" in vol:
                self.tv.volume = max(0.0, min(1.0, float(vol["level"])))
            if "muted" in vol:
                self.tv.muted = bool(vol["muted"])
            self._emit(rid, c)
        else:
            self.tv._write(c, NS_MEDIA, {"type": "INVALID_REQUEST", "requestId": rid, "reason": "INVALID_COMMAND"}, tid)

    def _cancel_fetch(self) -> None:
        for t in (self._task, self._end_task):
            if t and not t.done():
                t.cancel()

    async def _load(self, c: Conn, rid: int, p: dict) -> None:
        self._cancel_fetch()
        self.load_requests.append(p)
        media = p.get("media") or {}
        url = media.get("contentUrl") or media.get("contentId") or ""
        self.media_session_id += 1
        self.media = {k: media[k] for k in ("contentId", "contentUrl", "contentType", "streamType", "metadata")
                      if k in media}
        self.duration = float(media["duration"]) if media.get("duration") else None
        self.position = float(p.get("currentTime") or 0)
        self.total_bytes = 0
        self.content_length = 0
        self.download_done = asyncio.Event()
        self.idle_reason = None
        self.state = "BUFFERING"
        self._clock_from = None
        self.states.append((time.time(), "BUFFERING", None))
        self.tv._ev("load", url=url, content_type=media.get("contentType"))
        # BUFFERING is a broadcast (requestId 0); the reply to the LOAD itself comes once the outcome is known:
        # MEDIA_STATUS (playing / paused) or LOAD_FAILED, both carrying the LOAD's requestId.
        self._emit(0)
        self._task = self.tv.spawn(self._fetch_and_play(c, rid, url, bool(p.get("autoplay", True))))

    def spawn_seek(self, c: Conn, rid: int, to: float, resume: Optional[str]) -> None:
        self.position = max(0.0, min(to, self.duration or to))
        was_playing = self.state == "PLAYING" or resume == "PLAYBACK_START"
        self._clock_from = time.monotonic() if self.state == "PLAYING" else None
        self.tv._ev("seek", to=self.position)
        if self.content_length and self.duration:
            off = int(self.content_length * self.position / self.duration)
            self.tv.spawn(self._range_probe(off))
        self.state = "PLAYING" if was_playing else self.state
        self._clock_from = time.monotonic() if self.state == "PLAYING" else None
        self._emit(rid, c)
        self._reschedule_end()

    async def _range_probe(self, offset: int) -> None:
        url = self.media.get("contentUrl") or ""
        f = Fetch(url=url, headers={"Range": f"bytes={offset}-", "User-Agent": CHROME_UA}, t_start=time.time(),
                  range=f"bytes={offset}-")
        self.fetches.append(f)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.get(url, headers=f.headers, allow_redirects=False) as r:
                    f.status, f.response_headers = r.status, dict(r.headers)
                    chunk = await r.content.read(65536)
                    f.bytes = len(chunk)
        except Exception as e:  # noqa: BLE001
            f.error = repr(e)
        f.t_end = time.time()

    async def _fetch_and_play(self, c: Conn, rid: int, url: str, autoplay: bool) -> None:
        f = Fetch(url=url, headers={"Range": "bytes=0-", "User-Agent": CHROME_UA, "Accept": "*/*",
                                    "Accept-Encoding": "identity", "Connection": "keep-alive"},
                  t_start=time.time(), range="bytes=0-")
        self.fetches.append(f)
        out = None
        started = False
        try:
            if self.tv.out_dir:
                d = self.tv.out_dir / "media"
                d.mkdir(parents=True, exist_ok=True)
                name = os.path.basename(url.split("?")[0]) or "media.bin"
                self.file_path = d / f"{self.media_session_id}-{name}"
                out = open(self.file_path, "wb")
            if self.tv.show:
                self._player = FfplaySink("mov,mp4,m4a,3gp,3g2,mj2", "Wirtualny TV (AirLinx)", None)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                                           sock_read=30)) as s:
                async with s.get(url, headers=f.headers, allow_redirects=False) as r:
                    f.status, f.response_headers = r.status, dict(r.headers)
                    if r.status not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status}")
                    cr = r.headers.get("Content-Range", "")
                    self.content_length = (int(cr.rsplit("/", 1)[1]) if "/" in cr and cr.rsplit("/", 1)[1].isdigit()
                                           else int(r.headers.get("Content-Length", 0) or 0))
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        f.bytes += len(chunk)
                        self.total_bytes += len(chunk)
                        if out:
                            out.write(chunk)
                        if self._player:
                            self._player.write(chunk)
                        if not started and self.total_bytes >= min(32 * 1024, self.content_length or 1 << 30):
                            started = True
                            self._on_playable(c, rid, autoplay)
            f.t_end = time.time()
            if not started:
                self._on_playable(c, rid, autoplay)
            if out:
                out.close()
                out = None
            if self.duration is None:
                self.duration = await _probe_duration(self.file_path) if self.file_path else None
                self._emit()
            self.download_done.set()
            self.tv._ev("download_done", bytes=self.total_bytes, status=f.status)
            self._reschedule_end()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            f.error = repr(e)
            f.t_end = time.time()
            self.tv._ev("load_failed", url=url, error=repr(e))
            self.tv._write(c, NS_MEDIA, {"type": "LOAD_FAILED", "requestId": rid, "detailedErrorCode": 905,
                                         "itemId": 1}, self.tv.app["transportId"] if self.tv.app else "web")
            self._set("IDLE", "ERROR")
        finally:
            if out:
                out.close()

    def _on_playable(self, c: Conn, rid: int, autoplay: bool) -> None:
        if self.state == "BUFFERING":
            self._set("PLAYING" if autoplay else "PAUSED", rid=rid, c=c)
            self._reschedule_end()

    def _reschedule_end(self) -> None:
        if self._end_task and not self._end_task.done():
            self._end_task.cancel()
        if self.state == "PLAYING" and self.duration and self.download_done.is_set():
            self._end_task = self.tv.spawn(self._end_after(self.duration - self.current_time()))

    async def _end_after(self, secs: float) -> None:
        await asyncio.sleep(max(0.0, secs if self.tv.media_realtime else 0.2))
        if self.state == "PLAYING":
            self.position = self.duration or self.position
            self._set("IDLE", "FINISHED")


async def _probe_duration(path: Path) -> Optional[float]:
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 10)
        return float(out.decode().strip())
    except Exception:  # noqa: BLE001
        return None
