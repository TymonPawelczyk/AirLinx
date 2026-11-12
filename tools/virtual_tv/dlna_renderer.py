"""Fake DLNA/UPnP MediaRenderer (the TV side of AVTransport), modelled on the Samsung S85F.

HTTP only: device description at ``/dmr`` (also ``/device.xml``), SCPD documents and SOAP control URLs
for AVTransport, RenderingControl and ConnectionManager. ``Play`` downloads the URL like a Samsung TV
does (HEAD + ``Range: bytes=0-``, ``getcontentFeatures.dlna.org``, ``transferMode.dlna.org``, and
``TimeSeekRange.dlna.org`` for live streams) and simulates playback in real time.

``samsung_quirks=True`` (default): GetVolume/GetMute always answer 0, SetVolume is accepted and ignored,
exactly like the real TV, which has a fixed output volume.

Failure injection (``failure=``): ``reject_714`` / ``reject_716`` (UPnP fault on SetAVTransportURI), ``drop_soap``
(close the connection) and ``no_response`` (never answer) on the ``failure_action`` call (default Play), and
``drop_fetch`` (abort the media download after ``drop_after_bytes``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web

from .sinks import FfplaySink

log = logging.getLogger("vtv.dlna")

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
SAMSUNG_UA = "SEC_HHP_[TV] Virtual Samsung S85F/1.0 DLNADOC/1.50 UPnP/1.0"

# service -> (urn, path prefix, actions{name: (in args, out args)}, state variables{name: (type, allowed|None)})
_ARG = "A_ARG_TYPE_"
_AVT_ACTIONS: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {
    "SetAVTransportURI": ([("InstanceID", _ARG + "InstanceID"), ("CurrentURI", "AVTransportURI"),
                           ("CurrentURIMetaData", "AVTransportURIMetaData")], []),
    "GetMediaInfo": ([("InstanceID", _ARG + "InstanceID")],
                     [("NrTracks", "NumberOfTracks"), ("MediaDuration", "CurrentMediaDuration"),
                      ("CurrentURI", "AVTransportURI"), ("CurrentURIMetaData", "AVTransportURIMetaData"),
                      ("NextURI", "NextAVTransportURI"), ("NextURIMetaData", "NextAVTransportURIMetaData"),
                      ("PlayMedium", "PlaybackStorageMedium"), ("RecordMedium", "PossibleRecordStorageMedia"),
                      ("WriteStatus", "RecordMediumWriteStatus")]),
    "GetTransportInfo": ([("InstanceID", _ARG + "InstanceID")],
                         [("CurrentTransportState", "TransportState"), ("CurrentTransportStatus", "TransportStatus"),
                          ("CurrentSpeed", "TransportPlaySpeed")]),
    "GetPositionInfo": ([("InstanceID", _ARG + "InstanceID")],
                        [("Track", "CurrentTrack"), ("TrackDuration", "CurrentTrackDuration"),
                         ("TrackMetaData", "CurrentTrackMetaData"), ("TrackURI", "CurrentTrackURI"),
                         ("RelTime", "RelativeTimePosition"), ("AbsTime", "AbsoluteTimePosition"),
                         ("RelCount", "RelativeCounterPosition"), ("AbsCount", "AbsoluteCounterPosition")]),
    "GetDeviceCapabilities": ([("InstanceID", _ARG + "InstanceID")],
                              [("PlayMedia", "PossiblePlaybackStorageMedia"),
                               ("RecMedia", "PossibleRecordStorageMedia"),
                               ("RecQualityModes", "PossibleRecordQualityModes")]),
    "GetTransportSettings": ([("InstanceID", _ARG + "InstanceID")],
                             [("PlayMode", "CurrentPlayMode"), ("RecQualityMode", "CurrentRecordQualityMode")]),
    "Stop": ([("InstanceID", _ARG + "InstanceID")], []),
    "Play": ([("InstanceID", _ARG + "InstanceID"), ("Speed", "TransportPlaySpeed")], []),
    "Pause": ([("InstanceID", _ARG + "InstanceID")], []),
    "Seek": ([("InstanceID", _ARG + "InstanceID"), ("Unit", _ARG + "SeekMode"), ("Target", _ARG + "SeekTarget")], []),
    "Next": ([("InstanceID", _ARG + "InstanceID")], []),
    "Previous": ([("InstanceID", _ARG + "InstanceID")], []),
    "GetCurrentTransportActions": ([("InstanceID", _ARG + "InstanceID")], [("Actions", "CurrentTransportActions")]),
}
_AVT_VARS: dict[str, tuple[str, Optional[list[str]]]] = {
    "TransportState": ("string", ["STOPPED", "PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING", "NO_MEDIA_PRESENT"]),
    "TransportStatus": ("string", ["OK", "ERROR_OCCURRED"]), "PlaybackStorageMedium": ("string", None),
    "PossiblePlaybackStorageMedia": ("string", None), "PossibleRecordStorageMedia": ("string", None),
    "CurrentPlayMode": ("string", ["NORMAL"]), "TransportPlaySpeed": ("string", ["1"]),
    "RecordMediumWriteStatus": ("string", None), "CurrentRecordQualityMode": ("string", None),
    "PossibleRecordQualityModes": ("string", None), "NumberOfTracks": ("ui4", None), "CurrentTrack": ("ui4", None),
    "CurrentTrackDuration": ("string", None), "CurrentMediaDuration": ("string", None),
    "CurrentTrackMetaData": ("string", None), "CurrentTrackURI": ("string", None), "AVTransportURI": ("string", None),
    "AVTransportURIMetaData": ("string", None), "NextAVTransportURI": ("string", None),
    "NextAVTransportURIMetaData": ("string", None), "RelativeTimePosition": ("string", None),
    "AbsoluteTimePosition": ("string", None), "RelativeCounterPosition": ("i4", None),
    "AbsoluteCounterPosition": ("ui4", None), "CurrentTransportActions": ("string", None),
    _ARG + "SeekMode": ("string", ["REL_TIME", "TRACK_NR"]), _ARG + "SeekTarget": ("string", None),
    _ARG + "InstanceID": ("ui4", None),
}
_RCS_ACTIONS = {
    "ListPresets": ([("InstanceID", _ARG + "InstanceID")], [("CurrentPresetNameList", "PresetNameList")]),
    "SelectPreset": ([("InstanceID", _ARG + "InstanceID"), ("PresetName", _ARG + "PresetName")], []),
    "GetMute": ([("InstanceID", _ARG + "InstanceID"), ("Channel", _ARG + "Channel")], [("CurrentMute", "Mute")]),
    "SetMute": ([("InstanceID", _ARG + "InstanceID"), ("Channel", _ARG + "Channel"), ("DesiredMute", "Mute")], []),
    "GetVolume": ([("InstanceID", _ARG + "InstanceID"), ("Channel", _ARG + "Channel")], [("CurrentVolume", "Volume")]),
    "SetVolume": ([("InstanceID", _ARG + "InstanceID"), ("Channel", _ARG + "Channel"),
                   ("DesiredVolume", "Volume")], []),
}
_RCS_VARS = {"PresetNameList": ("string", None), "Mute": ("boolean", None), "Volume": ("ui2", None),
             _ARG + "Channel": ("string", ["Master"]), _ARG + "InstanceID": ("ui4", None),
             _ARG + "PresetName": ("string", ["FactoryDefaults"])}
_CMS_ACTIONS = {
    "GetProtocolInfo": ([], [("Source", "SourceProtocolInfo"), ("Sink", "SinkProtocolInfo")]),
    "GetCurrentConnectionIDs": ([], [("ConnectionIDs", "CurrentConnectionIDs")]),
    "GetCurrentConnectionInfo": ([("ConnectionID", _ARG + "ConnectionID")],
                                 [("RcsID", _ARG + "RcsID"), ("AVTransportID", _ARG + "AVTransportID"),
                                  ("ProtocolInfo", _ARG + "ProtocolInfo"),
                                  ("PeerConnectionManager", _ARG + "ConnectionManager"),
                                  ("PeerConnectionID", _ARG + "ConnectionID"),
                                  ("Direction", _ARG + "Direction"), ("Status", _ARG + "ConnectionStatus")]),
}
_CMS_VARS = {"SourceProtocolInfo": ("string", None), "SinkProtocolInfo": ("string", None),
             "CurrentConnectionIDs": ("string", None), _ARG + "ConnectionStatus": ("string", ["OK"]),
             _ARG + "ConnectionManager": ("string", None), _ARG + "Direction": ("string", ["Input", "Output"]),
             _ARG + "ProtocolInfo": ("string", None), _ARG + "ConnectionID": ("i4", None),
             _ARG + "AVTransportID": ("i4", None), _ARG + "RcsID": ("i4", None)}

SERVICES = {
    "AVTransport": (AVT, _AVT_ACTIONS, _AVT_VARS),
    "RenderingControl": (RCS, _RCS_ACTIONS, _RCS_VARS),
    "ConnectionManager": (CMS, _CMS_ACTIONS, _CMS_VARS),
}
SINK_PROTOCOLS = ",".join([
    "http-get:*:video/mp4:*", "http-get:*:video/mpeg:*", "http-get:*:video/x-matroska:*", "http-get:*:video/mp2t:*",
    "http-get:*:audio/mpeg:*", "http-get:*:audio/mp4:*", "http-get:*:audio/flac:*", "http-get:*:image/jpeg:*",
    "http-get:*:image/png:*"])


def scpd(actions: dict, variables: dict) -> str:
    out = ['<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major>'
           '<minor>0</minor></specVersion><actionList>']
    for name, (ins, outs) in actions.items():
        out.append(f"<action><name>{name}</name><argumentList>")
        for a, v in ins:
            out.append(f"<argument><name>{a}</name><direction>in</direction><relatedStateVariable>{v}"
                       "</relatedStateVariable></argument>")
        for a, v in outs:
            out.append(f"<argument><name>{a}</name><direction>out</direction><relatedStateVariable>{v}"
                       "</relatedStateVariable></argument>")
        out.append("</argumentList></action>")
    out.append("</actionList><serviceStateTable>")
    for name, (typ, allowed) in variables.items():
        send = "yes" if name.startswith(_ARG) else "no"
        out.append(f'<stateVariable sendEvents="{send}"><name>{name}</name><dataType>{typ}</dataType>')
        if allowed:
            out.append("<allowedValueList>" + "".join(f"<allowedValue>{a}</allowedValue>" for a in allowed)
                       + "</allowedValueList>")
        if name == "Volume":
            out.append("<allowedValueRange><minimum>0</minimum><maximum>100</maximum><step>1</step></allowedValueRange>")
        out.append("</stateVariable>")
    out.append("</serviceStateTable></scpd>")
    return "".join(out)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def parse_time(s: str) -> float:
    m = re.match(r"^(\d+):(\d+):(\d+)(?:\.(\d+))?$", (s or "").strip())
    if not m:
        raise ValueError(s)
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) + (float("0." + m[4]) if m[4] else 0.0)


class UpnpFault(Exception):
    def __init__(self, code: int, desc: str) -> None:
        self.code, self.desc = code, desc


@dataclass
class DlnaFetch:
    method: str
    url: str
    headers: dict
    status: int = 0
    response_headers: dict = field(default_factory=dict)
    bytes: int = 0
    t_start: float = 0.0
    t_end: float = 0.0
    error: str = ""


FAILURES = ("", "reject_714", "reject_716", "drop_soap", "no_response", "drop_fetch")


class DlnaRenderer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 9197, name: str = "Wirtualny TV (DLNA)",
                 udn: Optional[str] = None, samsung_quirks: bool = True, out_dir: Optional[Path] = None,
                 show: bool = False, realtime: bool = True, failure: str = "", failure_action: str = "Play",
                 drop_after_bytes: int = 20_000) -> None:
        if failure not in FAILURES:
            raise ValueError(f"unknown failure mode {failure!r}")
        self.failure, self.failure_action, self.drop_after_bytes = failure, failure_action, drop_after_bytes
        self.host, self.port, self.name = host, port, name
        self.udn = "uuid:" + (udn or str(uuid.uuid4()))
        self.samsung_quirks = samsung_quirks
        self.out_dir = out_dir
        self.show = show
        self.realtime = realtime
        self.runner: Optional[web.AppRunner] = None
        self.volume = 20
        self.muted = False
        self.uri = ""
        self.metadata = ""
        self.state = "NO_MEDIA_PRESENT"
        self.duration: Optional[float] = None
        self.position = 0.0
        self._clock_from: Optional[float] = None
        self.fetches: list[DlnaFetch] = []
        self.actions: list[tuple[float, str, dict]] = []
        self.states: list[tuple[float, str]] = []
        self.bytes_received = 0
        self.live = False
        self._task: Optional[asyncio.Task] = None
        self._player: Optional[FfplaySink] = None
        self.file_path: Optional[Path] = None
        self.download_done = asyncio.Event()
        self.fail_next_fetch = False

    # ---------------------------------------------------------------- lifecycle
    @property
    def location(self) -> str:
        return f"http://{self.host}:{self.port}/dmr"

    async def start(self) -> "DlnaRenderer":
        app = web.Application()
        app.router.add_get("/dmr", self._desc)
        app.router.add_get("/device.xml", self._desc)
        for svc in SERVICES:
            app.router.add_get(f"/{svc}/scpd.xml", self._scpd)
            app.router.add_post(f"/{svc}/control", self._control)
            app.router.add_route("SUBSCRIBE", f"/{svc}/event", self._event)
            app.router.add_route("UNSUBSCRIBE", f"/{svc}/event", self._event)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        self._cancel()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self._player:
            self._player.close()

    # ---------------------------------------------------------------- HTTP: description
    async def _desc(self, request: web.Request) -> web.Response:
        base = f"http://{self.host}:{self.port}"
        services = "".join(
            f"<service><serviceType>{urn}</serviceType><serviceId>urn:upnp-org:serviceId:{svc}</serviceId>"
            f"<controlURL>/{svc}/control</controlURL><eventSubURL>/{svc}/event</eventSubURL>"
            f"<SCPDURL>/{svc}/scpd.xml</SCPDURL></service>" for svc, (urn, _, _) in SERVICES.items())
        xml = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0" '
               'xmlns:dlna="urn:schemas-dlna-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>'
               f"<URLBase>{base}/</URLBase><device><dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>"
               "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
               f"<friendlyName>{escape(self.name)}</friendlyName><manufacturer>AirLinx Virtual TV</manufacturer>"
               "<modelName>Virtual MediaRenderer</modelName><modelNumber>1</modelNumber>"
               f"<UDN>{self.udn}</UDN><serviceList>{services}</serviceList></device></root>")
        return web.Response(text=xml, content_type="text/xml", charset="utf-8")

    async def _scpd(self, request: web.Request) -> web.Response:
        svc = request.path.split("/")[1]
        _, actions, variables = SERVICES[svc]
        return web.Response(text=scpd(actions, variables), content_type="text/xml", charset="utf-8")

    async def _event(self, request: web.Request) -> web.Response:
        return web.Response(status=200, headers={"SID": "uuid:" + str(uuid.uuid4()), "TIMEOUT": "Second-1800"})

    # ---------------------------------------------------------------- SOAP
    async def _control(self, request: web.Request) -> web.Response:
        svc = request.path.split("/")[1]
        urn = SERVICES[svc][0]
        action_hdr = request.headers.get("SOAPACTION", "").strip('"')
        try:
            body = ET.fromstring(await request.read())
            call = next(iter(body.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")))
        except (ET.ParseError, TypeError, StopIteration):
            return self._fault(401, "Invalid Action")
        name = call.tag.split("}")[-1]
        if action_hdr and not action_hdr.endswith("#" + name):
            return self._fault(401, "Invalid Action")
        args = {c.tag.split("}")[-1]: (c.text or "") for c in call}
        self.actions.append((time.time(), name, args))
        if self.failure in ("drop_soap", "no_response") and name == self.failure_action:
            if self.failure == "drop_soap":
                request.transport.abort()  # type: ignore[union-attr]
            await asyncio.sleep(3600)       # cancelled when the client goes away or the renderer stops
        if self.failure in ("reject_714", "reject_716") and name == "SetAVTransportURI":
            code = int(self.failure[-3:])
            return self._fault(code, "Illegal MIME-type" if code == 714 else "Resource not found")
        handler = getattr(self, f"_a_{svc}_{name}", None)
        if handler is None or name not in SERVICES[svc][1]:
            return self._fault(401, "Invalid Action")
        try:
            out = await handler(args) or {}
        except UpnpFault as f:
            return self._fault(f.code, f.desc)
        inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in out.items())
        xml = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{name}Response xmlns:u="{urn}">{inner}</u:{name}Response></s:Body></s:Envelope>')
        return web.Response(text=xml, content_type="text/xml", charset="utf-8")

    @staticmethod
    def _fault(code: int, desc: str) -> web.Response:
        xml = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault><faultcode>s:Client'
               '</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError '
               f'xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
               f"<errorDescription>{escape(desc)}</errorDescription></UPnPError></detail></s:Fault></s:Body>"
               "</s:Envelope>")
        return web.Response(status=500, text=xml, content_type="text/xml", charset="utf-8")

    # ---------------------------------------------------------------- state machine
    def current_time(self) -> float:
        if self.state == "PLAYING" and self._clock_from is not None:
            t = self.position + (time.monotonic() - self._clock_from)
            return min(t, self.duration) if self.duration else t
        return self.position

    def _set_state(self, st: str) -> None:
        if st == self.state:
            return
        self.position = self.current_time()
        self.state = st
        self._clock_from = time.monotonic() if st == "PLAYING" else None
        self.states.append((time.time(), st))
        log.info("transport state %s", st)

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    # ---- AVTransport
    async def _a_AVTransport_SetAVTransportURI(self, a: dict) -> dict:
        self._cancel()
        self.uri, self.metadata = a.get("CurrentURI", ""), a.get("CurrentURIMetaData", "")
        self.position, self.duration, self.live = 0.0, None, False
        m = re.search(r'duration="([0-9:.]+)"', self.metadata)
        if m:
            with contextlib.suppress(ValueError):
                self.duration = parse_time(m[1])
        self._set_state("STOPPED" if self.uri else "NO_MEDIA_PRESENT")
        return {}

    async def _a_AVTransport_Play(self, a: dict) -> dict:
        if not self.uri:
            raise UpnpFault(701, "Transition not available")
        if self.state == "PAUSED_PLAYBACK":
            self._set_state("PLAYING")
            return {}
        if self.state in ("PLAYING", "TRANSITIONING"):
            return {}
        self._set_state("TRANSITIONING")
        self.download_done = asyncio.Event()
        self._task = asyncio.ensure_future(self._play_task(self.uri))
        return {}

    async def _a_AVTransport_Pause(self, a: dict) -> dict:
        if self.state != "PLAYING":
            raise UpnpFault(701, "Transition not available")
        self._set_state("PAUSED_PLAYBACK")
        return {}

    async def _a_AVTransport_Stop(self, a: dict) -> dict:
        self._cancel()
        self.position = 0.0
        self._set_state("STOPPED" if self.uri else "NO_MEDIA_PRESENT")
        return {}

    async def _a_AVTransport_Seek(self, a: dict) -> dict:
        if a.get("Unit") != "REL_TIME":
            raise UpnpFault(710, "Seek mode not supported")
        try:
            t = parse_time(a.get("Target", ""))
        except ValueError:
            raise UpnpFault(711, "Illegal seek target") from None
        if self.state not in ("PLAYING", "PAUSED_PLAYBACK"):
            raise UpnpFault(701, "Transition not available")
        self.position = min(t, self.duration) if self.duration else t
        if self._clock_from is not None:
            self._clock_from = time.monotonic()
        return {}

    async def _a_AVTransport_Next(self, a: dict) -> dict:
        raise UpnpFault(701, "Transition not available")

    _a_AVTransport_Previous = _a_AVTransport_Next

    async def _a_AVTransport_GetTransportInfo(self, a: dict) -> dict:
        return {"CurrentTransportState": self.state, "CurrentTransportStatus": "OK", "CurrentSpeed": "1"}

    async def _a_AVTransport_GetPositionInfo(self, a: dict) -> dict:
        t = fmt_time(self.current_time())
        d = fmt_time(self.duration) if self.duration else "00:00:00"
        return {"Track": 1 if self.uri else 0, "TrackDuration": d, "TrackMetaData": self.metadata,
                "TrackURI": self.uri, "RelTime": t, "AbsTime": t, "RelCount": 2147483647, "AbsCount": 2147483647}

    async def _a_AVTransport_GetMediaInfo(self, a: dict) -> dict:
        return {"NrTracks": 1 if self.uri else 0, "MediaDuration": fmt_time(self.duration or 0),
                "CurrentURI": self.uri, "CurrentURIMetaData": self.metadata, "NextURI": "NOT_IMPLEMENTED",
                "NextURIMetaData": "NOT_IMPLEMENTED", "PlayMedium": "NETWORK", "RecordMedium": "NOT_IMPLEMENTED",
                "WriteStatus": "NOT_IMPLEMENTED"}

    async def _a_AVTransport_GetDeviceCapabilities(self, a: dict) -> dict:
        return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED", "RecQualityModes": "NOT_IMPLEMENTED"}

    async def _a_AVTransport_GetTransportSettings(self, a: dict) -> dict:
        return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}

    async def _a_AVTransport_GetCurrentTransportActions(self, a: dict) -> dict:
        acts = {"PLAYING": "Pause,Stop,Seek", "PAUSED_PLAYBACK": "Play,Stop,Seek", "STOPPED": "Play",
                "TRANSITIONING": "Stop"}.get(self.state, "")
        return {"Actions": acts}

    # ---- RenderingControl
    async def _a_RenderingControl_GetVolume(self, a: dict) -> dict:
        return {"CurrentVolume": 0 if self.samsung_quirks else self.volume}

    async def _a_RenderingControl_SetVolume(self, a: dict) -> dict:
        try:
            v = int(a.get("DesiredVolume", "0"))
        except ValueError:
            raise UpnpFault(402, "Invalid Args") from None
        if not 0 <= v <= 100:
            raise UpnpFault(601, "Argument value out of range")
        if not self.samsung_quirks:
            self.volume = v
        return {}

    async def _a_RenderingControl_GetMute(self, a: dict) -> dict:
        return {"CurrentMute": 0 if self.samsung_quirks else int(self.muted)}

    async def _a_RenderingControl_SetMute(self, a: dict) -> dict:
        if not self.samsung_quirks:
            self.muted = a.get("DesiredMute", "0") in ("1", "true", "True")
        return {}

    async def _a_RenderingControl_ListPresets(self, a: dict) -> dict:
        return {"CurrentPresetNameList": "FactoryDefaults"}

    async def _a_RenderingControl_SelectPreset(self, a: dict) -> dict:
        return {}

    # ---- ConnectionManager
    async def _a_ConnectionManager_GetProtocolInfo(self, a: dict) -> dict:
        return {"Source": "", "Sink": SINK_PROTOCOLS}

    async def _a_ConnectionManager_GetCurrentConnectionIDs(self, a: dict) -> dict:
        return {"ConnectionIDs": "0"}

    async def _a_ConnectionManager_GetCurrentConnectionInfo(self, a: dict) -> dict:
        return {"RcsID": 0, "AVTransportID": 0, "ProtocolInfo": "", "PeerConnectionManager": "",
                "PeerConnectionID": -1, "Direction": "Input", "Status": "OK"}

    # ---------------------------------------------------------------- playback = download like a Samsung
    def _headers(self, live_hint: bool) -> dict:
        h = {"User-Agent": SAMSUNG_UA, "Accept": "*/*", "Connection": "close", "transferMode.dlna.org": "Streaming",
             "getcontentFeatures.dlna.org": "1", "Range": "bytes=0-"}
        if live_hint:
            h["TimeSeekRange.dlna.org"] = "npt=0.000-"
        return h

    async def _play_task(self, url: str) -> None:
        live_hint = "/live/" in url
        head = DlnaFetch("HEAD", url, self._headers(live_hint), t_start=time.time())
        get = DlnaFetch("GET", url, self._headers(live_hint), t_start=time.time())
        out = None
        started = False
        try:
            if self.fail_next_fetch:
                self.fail_next_fetch = False
                raise RuntimeError("fetch failure injected")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                                           sock_read=30)) as s:
                self.fetches.append(head)
                async with s.head(url, headers=head.headers, allow_redirects=False) as r:
                    head.status, head.response_headers = r.status, dict(r.headers)
                head.t_end = time.time()
                self.fetches.append(get)
                if self.out_dir:
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    self.file_path = self.out_dir / ("dlna-" + (os.path.basename(url.split("?")[0]) or "media.bin"))
                    out = open(self.file_path, "wb")
                if self.show:
                    self._player = FfplaySink("mpegts" if live_hint else "mp4", "Wirtualny TV DLNA (AirLinx)", None)
                async with s.get(url, headers=get.headers, allow_redirects=False) as r:
                    get.status, get.response_headers = r.status, dict(r.headers)
                    if r.status not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status}")
                    self.live = r.headers.get("Content-Length") is None and r.status == 200
                    async for chunk in r.content.iter_chunked(64 * 1024):
                        get.bytes += len(chunk)
                        if self.failure == "drop_fetch" and get.bytes >= self.drop_after_bytes:
                            raise ConnectionResetError("connection dropped by the TV")
                        self.bytes_received += len(chunk)
                        if out:
                            out.write(chunk)
                        if self._player:
                            self._player.write(chunk)
                        if not started and get.bytes >= 16 * 1024:
                            started = True
                            self._set_state("PLAYING")
            get.t_end = time.time()
            if self.out_dir:
                import json
                with contextlib.suppress(OSError), open(self.out_dir / "events.jsonl", "a") as f:
                    f.write(json.dumps({"t": time.time(), "kind": "fetch", "url": url, "status": get.status,
                                        "bytes": get.bytes, "ua": get.headers["User-Agent"]}) + "\n")
            if out:
                out.close()
                out = None
            if not started:
                self._set_state("PLAYING")
            if self.duration is None and self.file_path and not self.live:
                from .cast_receiver import _probe_duration
                self.duration = await _probe_duration(self.file_path)
            self.download_done.set()
            if self.live or not self.duration:
                self._set_state("STOPPED")          # the stream ended
                return
            while self.state in ("PLAYING", "PAUSED_PLAYBACK") and self.current_time() < self.duration:
                await asyncio.sleep(0.05 if self.realtime else 0)
                if not self.realtime:
                    self.position = self.duration
                    self._clock_from = None if self.state != "PLAYING" else time.monotonic()
            if self.state == "PLAYING":
                self.position = 0.0
                self._set_state("STOPPED")            # Samsung: STOPPED at the end of the track
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            get.error = repr(e)
            get.t_end = time.time()
            log.warning("fetch of %s failed: %s", url.split("?")[0], e)
            self._set_state("STOPPED")
        finally:
            if out:
                out.close()


__all__ = ["DlnaRenderer", "SAMSUNG_UA"]
