#!/usr/bin/env python3
"""AirLinx spike: test server dla przeglądarki TV (MSE / WebCodecs / WebRTC).

Pipeline wideo (wspólny dla wszystkich testów):
  ffmpeg x11grab (-copyts, znaczniki = wall-clock przechwycenia)
    -> hwupload_cuda,scale_cuda (GPU) -> metadata=print (pts klatki na stderr)
    -> h264_nvenc (p1/ull/zerolatency/delay 0/bf 0)
    -> fragmented MP4 (1 klatka = 1 fragment) na stdout
Serwer parsuje boksy MP4 i:
  * MSE:       przekazuje init + moof/mdat bez zmian,
  * WebCodecs: wycina próbkę AVCC z mdat + avcC jako `description`,
  * WebRTC:    zamienia AVCC -> Annex-B i podaje gotowe pakiety do aiortc (bez re-enkodowania).

Format binarnych wiadomości WS (MSE i WebCodecs):
  [u8 typ][f64 capture_ms][f64 send_ms][f64 media_ms][payload]
  typ: 0 = init segment, 1 = fragment/delta, 2 = fragment/keyframe
  capture_ms, send_ms: czas serwera (epoch ms), media_ms: tfdt (ms) lub -1.
"""
from __future__ import annotations

import asyncio
import base64
import fractions
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESULTS.mkdir(exist_ok=True)

PORT = int(os.environ.get("PORT", "8090"))
DISPLAY = os.environ.get("CAPTURE_DISPLAY", os.environ.get("DISPLAY", ":1"))
FFMPEG = os.environ.get("FFMPEG", shutil.which("ffmpeg") or "ffmpeg")

log = logging.getLogger("tvspike")


def now_ms() -> float:
    return time.time() * 1000.0


# --------------------------------------------------------------------------- capture config


def detect_capture_region() -> str:
    """Zwraca 'WxH+X+Y' monitora primary (xrandr) albo env CAPTURE."""
    env = os.environ.get("CAPTURE")
    if env:
        return env
    try:
        out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True,
                             env={**os.environ, "DISPLAY": DISPLAY}, timeout=5).stdout
        m = re.search(r" connected primary (\d+x\d+\+\d+\+\d+)", out)
        if not m:
            m = re.search(r" connected (\d+x\d+\+\d+\+\d+)", out)
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001
        log.warning("xrandr failed: %s", e)
    return "1920x1080+0+0"


def detect_monitor_source() -> str | None:
    env = os.environ.get("AUDIO_SOURCE")
    if env:
        return env
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
        srcs = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                              text=True, timeout=5).stdout
        names = [ln.split("\t")[1] for ln in srcs.splitlines() if "\t" in ln]
        if sink and f"{sink}.monitor" in names:
            return f"{sink}.monitor"
        mons = [n for n in names if n.endswith(".monitor")]
        return mons[0] if mons else None
    except Exception as e:  # noqa: BLE001
        log.warning("pactl failed: %s", e)
        return None


CAPTURE = detect_capture_region()


# --------------------------------------------------------------------------- MP4 helpers


def iter_boxes(buf: bytes | memoryview, start: int = 0, end: int | None = None):
    end = len(buf) if end is None else end
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, off)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, off + 8)[0]
            hdr = 16
        if size < hdr or off + size > end:
            break
        yield typ.decode("latin1"), off, size, hdr
        off += size


def find_box(buf: bytes, path: list[str], start: int = 0, end: int | None = None):
    for typ, off, size, hdr in iter_boxes(buf, start, end):
        if typ == path[0]:
            if len(path) == 1:
                return off, size, hdr
            # stsd ma 8 bajtów (version/flags + entry_count) przed dziećmi, avc1 ma 78
            skip = {"stsd": 8, "avc1": 78, "avc3": 78, "mp4a": 28}.get(typ, 0)
            r = find_box(buf, path[1:], off + hdr + skip, off + size)
            if r:
                return r
    return None


def parse_avcc(avcc: bytes):
    profile, compat, level = avcc[1], avcc[2], avcc[3]
    nal_len = (avcc[4] & 3) + 1
    pos = 5
    n_sps = avcc[pos] & 0x1F
    pos += 1
    sps, pps = [], []
    for _ in range(n_sps):
        ln = struct.unpack_from(">H", avcc, pos)[0]
        sps.append(avcc[pos + 2:pos + 2 + ln])
        pos += 2 + ln
    n_pps = avcc[pos]
    pos += 1
    for _ in range(n_pps):
        ln = struct.unpack_from(">H", avcc, pos)[0]
        pps.append(avcc[pos + 2:pos + 2 + ln])
        pos += 2 + ln
    codec = "avc1.%02x%02x%02x" % (profile, compat, level)
    return codec, nal_len, sps, pps


def parse_moof(buf: bytes, off: int, size: int):
    """Zwraca listę trafów: {track, tfdt, sizes, data_offset}."""
    out = []
    for typ, o, s, h in iter_boxes(buf, off + 8, off + size):
        if typ != "traf":
            continue
        tr = {"track": None, "tfdt": None, "sizes": [], "data_offset": None}
        for t2, o2, s2, h2 in iter_boxes(buf, o + h, o + s):
            if t2 == "tfhd":
                tr["track"] = struct.unpack_from(">I", buf, o2 + 12)[0]
            elif t2 == "tfdt":
                v = buf[o2 + 8]
                tr["tfdt"] = (struct.unpack_from(">Q", buf, o2 + 12)[0] if v == 1
                              else struct.unpack_from(">I", buf, o2 + 12)[0])
            elif t2 == "trun":
                flags = struct.unpack_from(">I", buf, o2 + 8)[0] & 0xFFFFFF
                cnt = struct.unpack_from(">I", buf, o2 + 12)[0]
                p = o2 + 16
                if flags & 0x1:
                    tr["data_offset"] = struct.unpack_from(">i", buf, p)[0]
                    p += 4
                if flags & 0x4:
                    p += 4
                for _ in range(cnt):
                    if flags & 0x100:
                        p += 4
                    if flags & 0x200:
                        tr["sizes"].append(struct.unpack_from(">I", buf, p)[0])
                        p += 4
                    if flags & 0x400:
                        p += 4
                    if flags & 0x800:
                        p += 4
        out.append(tr)
    return out


def avcc_is_key(sample: bytes, nal_len: int) -> bool:
    p = 0
    while p + nal_len <= len(sample):
        ln = int.from_bytes(sample[p:p + nal_len], "big")
        if p + nal_len < len(sample) and (sample[p + nal_len] & 0x1F) == 5:
            return True
        p += nal_len + ln
    return False


def avcc_to_annexb(sample: bytes, nal_len: int, prefix: list[bytes] = ()) -> bytes:
    sc = b"\x00\x00\x00\x01"
    parts = [sc + x for x in prefix]
    p = 0
    while p + nal_len <= len(sample):
        ln = int.from_bytes(sample[p:p + nal_len], "big")
        parts.append(sc + sample[p + nal_len:p + nal_len + ln])
        p += nal_len + ln
    return b"".join(parts)


# --------------------------------------------------------------------------- ffmpeg pipeline


class Pipeline:
    """Jeden proces ffmpeg -> zdarzenia ('init', ...) / ('frag', ...) w kolejce."""

    active: set["Pipeline"] = set()

    def __init__(self, *, audio=False, fps=60, bitrate_m=12.0, size="1920x1080",
                 profile="high", gop=30, label="?", acodec="aac"):
        self.audio = audio
        self.acodec_req = acodec
        self.fps = fps
        self.bitrate = int(bitrate_m * 1_000_000)
        self.size = size
        self.profile = profile
        self.gop = gop
        self.label = label
        self.proc: asyncio.subprocess.Process | None = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=600)
        self.meta_pts: dict[int, float] = {}  # indeks klatki -> capture ms (wall-clock)
        self.pts0: float | None = None
        self.vtrack = 1
        self.nal_len = 4
        self.sps: list[bytes] = []
        self.pps: list[bytes] = []
        self.codec = None
        self.acodec = None
        self.avcc = b""
        self.vframes = 0
        self.tasks: list[asyncio.Task] = []
        self.closed = False
        self.t_start = 0.0

    def cmd(self) -> list[str]:
        w, h = self.size.split("x")
        vsize, pos = CAPTURE.split("+", 1)
        x, y = pos.split("+")
        # loglevel info: filtr metadata=print loguje pts każdej klatki przez av_log (niebuforowany stderr)
        c = [FFMPEG, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin", "-copyts",
             "-thread_queue_size", "64",
             "-f", "x11grab", "-framerate", str(self.fps), "-draw_mouse", "1",
             "-video_size", vsize, "-i", f"{DISPLAY}+{x},{y}"]
        mon = detect_monitor_source() if self.audio else None
        if self.audio and mon:
            c += ["-thread_queue_size", "64", "-f", "pulse", "-fragment_size", "1920",
                  "-i", mon]
        c += ["-map", "0:v"]
        if self.audio and mon:
            c += ["-map", "1:a"]
        vf = (f"hwupload_cuda,scale_cuda={w}:{h},"
              "metadata=mode=add:key=airlinx:value=1,"
              "metadata=mode=print")
        c += ["-vf", vf, "-vsync", "0",
              "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1",
              "-delay", "0", "-bf", "0", "-g", str(self.gop), "-forced-idr", "1",
              "-rc", "cbr", "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
              "-bufsize", str(max(self.bitrate // self.fps * 2, 200_000)),
              "-profile:v", self.profile]
        if self.audio and mon:
            if self.acodec_req == "opus":
                c += ["-c:a", "libopus", "-application", "lowdelay", "-frame_duration", "10",
                      "-b:a", "128k", "-ar", "48000", "-ac", "2"]
            else:
                c += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
            # nie czekaj na przeplot audio/wideo (domyślnie muxer trzyma wideo aż przyjdzie audio)
            c += ["-max_interleave_delta", str(int(os.environ.get("MAX_INTERLEAVE_US", "1")))]
        c += ["-video_track_timescale", "1000", "-f", "mp4",
              "-movflags", "empty_moov+default_base_moof+frag_every_frame",
              "-flush_packets", "1", "pipe:1"]
        return c

    async def start(self):
        for p in list(Pipeline.active):
            log.info("stopping previous pipeline %s", p.label)
            await p.stop()
        Pipeline.active.add(self)
        cmd = self.cmd()
        log.info("[%s] ffmpeg: %s", self.label, " ".join(cmd))
        self.t_start = now_ms()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DISPLAY": DISPLAY})
        self.tasks = [asyncio.create_task(self._read_stdout()),
                      asyncio.create_task(self._read_stderr())]

    async def stop(self):
        if self.closed:
            return
        self.closed = True
        Pipeline.active.discard(self)
        for t in self.tasks:
            t.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        log.info("[%s] stopped after %d video frames", self.label, self.vframes)
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def _read_stderr(self):
        rx = re.compile(r"frame:(\d+)\s+pts:(\d+)")
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            s = line.decode("utf8", "replace").strip()
            m = rx.search(s)
            if m:
                idx, pts_us = int(m.group(1)), int(m.group(2))
                self.meta_pts[idx] = pts_us / 1000.0
                if self.pts0 is None:
                    self.pts0 = pts_us / 1000.0
                if len(self.meta_pts) > 2000:
                    for k in sorted(self.meta_pts)[:1000]:
                        del self.meta_pts[k]
            elif s and "airlinx=" not in s:
                lvl = logging.WARNING if re.search(r"error|warn|fail|invalid", s, re.I) else logging.DEBUG
                log.log(lvl, "[%s] ffmpeg: %s", self.label, s)

    def _put(self, ev):
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            log.warning("[%s] queue full, dropping", self.label)

    async def _read_stdout(self):
        assert self.proc and self.proc.stdout
        buf = bytearray()
        init_parts: list[bytes] = []
        pending_moof: bytes | None = None
        try:
            while True:
                chunk = await self.proc.stdout.read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                consumed = 0
                for typ, off, size, hdr in iter_boxes(buf):
                    box = bytes(buf[off:off + size])
                    consumed = off + size
                    if typ in ("ftyp", "moov"):
                        init_parts.append(box)
                        if typ == "moov":
                            self._on_moov(box)
                            self._put(("init", b"".join(init_parts)))
                    elif typ == "moof":
                        pending_moof = box
                    elif typ == "mdat" and pending_moof is not None:
                        self._on_fragment(pending_moof, box)
                        pending_moof = None
                if consumed:
                    del buf[:consumed]
        except asyncio.CancelledError:
            raise
        finally:
            if not self.closed:
                rc = await self.proc.wait()
                log.warning("[%s] ffmpeg exited rc=%s", self.label, rc)
                self._put(None)

    def _on_moov(self, moov: bytes):
        # znajdź track wideo i avcC
        for typ, off, size, hdr in iter_boxes(moov, 8):
            if typ != "trak":
                continue
            tkhd = find_box(moov, ["tkhd"], off + 8, off + size)
            hdlr = find_box(moov, ["mdia", "hdlr"], off + 8, off + size)
            track_id = None
            if tkhd:
                v = moov[tkhd[0] + 8]
                track_id = struct.unpack_from(">I", moov, tkhd[0] + (20 if v == 0 else 28))[0]
            handler = moov[hdlr[0] + 16:hdlr[0] + 20].decode("latin1") if hdlr else "?"
            if handler == "vide":
                self.vtrack = track_id or 1
                r = find_box(moov, ["mdia", "minf", "stbl", "stsd", "avc1", "avcC"],
                             off + 8, off + size)
                if r:
                    self.avcc = moov[r[0] + r[2]:r[0] + r[1]]
                    self.codec, self.nal_len, self.sps, self.pps = parse_avcc(self.avcc)
            elif handler == "soun":
                self.acodec = "opus" if find_box(moov, ["mdia", "minf", "stbl", "stsd", "Opus"],
                                                 off + 8, off + size) else "mp4a.40.2"
        log.info("[%s] init: codec=%s audio=%s vtrack=%s", self.label, self.codec,
                 self.acodec, self.vtrack)

    def _on_fragment(self, moof: bytes, mdat: bytes):
        trafs = parse_moof(moof, 0, len(moof))
        video = next((t for t in trafs if t["track"] == self.vtrack), None)
        sample = None
        capture = -1.0
        media = -1.0
        key = False
        if video is not None:
            idx = self.vframes
            self.vframes += 1
            media = float(video["tfdt"] or 0)
            cap = self.meta_pts.pop(idx, None)
            if cap is None and self.pts0 is not None:
                cap = self.pts0 + media
            capture = cap if cap is not None else -1.0
            if video["data_offset"] is not None and video["sizes"]:
                start = video["data_offset"] - len(moof)
                end = start + sum(video["sizes"])
                if 8 <= start and end <= len(mdat):
                    sample = mdat[start:end]
            if sample is None and len(trafs) == 1:
                sample = mdat[8:]
            if sample is not None:
                key = avcc_is_key(sample, self.nal_len)
        self._put(("frag", moof + mdat, sample, capture, media, key, video is not None))


def pack_header(typ: int, capture: float, media: float) -> bytes:
    return struct.pack("<Bddd", typ, capture, now_ms(), media)


def pipeline_from_query(q, label, default_profile="high", audio=False) -> Pipeline:
    return Pipeline(audio=audio, fps=int(q.get("fps", 60)), bitrate_m=float(q.get("br", 12)),
                    size=q.get("res", "1920x1080"), profile=q.get("profile", default_profile),
                    gop=int(q.get("gop", 30)), label=label, acodec=q.get("acodec", "aac"))


# --------------------------------------------------------------------------- HTTP handlers


async def index(_req):
    return web.FileResponse(HERE / "static" / "index.html",
                            headers={"Cache-Control": "no-store"})


async def clock_page(_req):
    return web.FileResponse(HERE / "static" / "clock.html",
                            headers={"Cache-Control": "no-store"})


async def ws_clock(req):
    ws = web.WebSocketResponse()
    await ws.prepare(req)
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await ws.send_str(json.dumps({"t0": float(msg.data), "s": now_ms()}))
    return ws


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


async def report(req):
    data = await req.json()
    kind = data.get("kind", "report")
    peer = req.remote
    data["_server_ms"] = now_ms()
    data["_peer"] = peer
    if kind == "caps":
        p = RESULTS / f"caps-{_ts()}.json"
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print("\n===== CAPS from", peer, "->", p.name, "=====")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("=====\n", flush=True)
    else:
        sess = re.sub(r"[^A-Za-z0-9_-]", "_", str(data.get("session", "x")))[:40]
        p = RESULTS / f"{kind}-{sess}.jsonl"
        with p.open("a") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
        s = data.get("stats", {})
        short = {k: s[k] for k in list(s)[:14]}
        print(f"[{kind}] {data.get('test')} {peer}: {json.dumps(short, ensure_ascii=False)}",
              flush=True)
    return web.json_response({"ok": True, "file": p.name})


async def ws_mse(req):
    q = req.query
    audio = q.get("audio") == "1"
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(req)
    pl = pipeline_from_query(q, "mse" + ("+a" if audio else ""), audio=audio)
    await pl.start()
    reader = asyncio.create_task(_drain_ws(ws))
    sent_info = False
    try:
        while not ws.closed:
            ev = await pl.queue.get()
            if ev is None:
                break
            if ev[0] == "init":
                await ws.send_str(json.dumps({"type": "info", "codec": pl.codec,
                                              "acodec": pl.acodec, "capture": CAPTURE,
                                              "size": pl.size, "fps": pl.fps}))
                await ws.send_bytes(pack_header(0, -1, -1) + ev[1])
                sent_info = True
            elif ev[0] == "frag" and sent_info:
                _, data, _sample, capture, media, key, _isv = ev
                await ws.send_bytes(pack_header(2 if key else 1, capture, media) + data)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        reader.cancel()
        await pl.stop()
    return ws


async def ws_wc(req):
    q = req.query
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(req)
    pl = pipeline_from_query(q, "webcodecs")
    await pl.start()
    reader = asyncio.create_task(_drain_ws(ws))
    try:
        while not ws.closed:
            ev = await pl.queue.get()
            if ev is None:
                break
            if ev[0] == "init":
                await ws.send_str(json.dumps({
                    "type": "info", "codec": pl.codec, "capture": CAPTURE, "size": pl.size,
                    "fps": pl.fps, "description": base64.b64encode(pl.avcc).decode()}))
            elif ev[0] == "frag":
                _, _data, sample, capture, media, key, isv = ev
                if isv and sample is not None:
                    await ws.send_bytes(pack_header(2 if key else 1, capture, media) + sample)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        reader.cancel()
        await pl.stop()
    return ws


async def _drain_ws(ws):
    async for _ in ws:
        pass


# --------------------------------------------------------------------------- WebRTC (aiortc)

try:
    import av
    from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription
    from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
    HAVE_AIORTC = True
except Exception as _e:  # noqa: BLE001
    HAVE_AIORTC = False
    log.warning("aiortc unavailable: %s", _e)

PCS: dict = {}  # id -> (pc, pipeline)
RTC_STATS: dict = {}

if HAVE_AIORTC:
    class EncodedH264Track(MediaStreamTrack):
        """Track oddający gotowe av.Packet (H.264 Annex-B) — aiortc tylko pakietyzuje."""
        kind = "video"

        def __init__(self, pl: Pipeline):
            super().__init__()
            self.pl = pl
            self.n = 0
            self.lat_sum = 0.0
            self.t0 = time.time()

        async def recv(self):
            while True:
                ev = await self.pl.queue.get()
                if ev is None:
                    self.stop()
                    raise MediaStreamError
                if ev[0] != "frag":
                    continue
                _, _data, sample, capture, media, key, isv = ev
                if not isv or sample is None:
                    continue
                prefix = (self.pl.sps + self.pl.pps) if key else []
                pkt = av.Packet(avcc_to_annexb(sample, self.pl.nal_len, prefix))
                pkt.pts = int(media)
                pkt.dts = int(media)
                pkt.time_base = fractions.Fraction(1, 1000)
                self.n += 1
                if capture > 0:
                    self.lat_sum += now_ms() - capture
                if self.n % 120 == 0:
                    RTC_STATS.update({
                        "frames": self.n,
                        "capture_to_send_ms": round(self.lat_sum / 120, 2),
                        "fps": round(120 / (time.time() - self.t0), 1)})
                    self.lat_sum = 0.0
                    self.t0 = time.time()
                return pkt


async def offer(req):
    if not HAVE_AIORTC:
        return web.json_response({"error": "aiortc not installed"}, status=500)
    params = await req.json()
    q = req.query
    pc = RTCPeerConnection()
    pl = pipeline_from_query(q, "webrtc", default_profile=q.get("profile", "baseline"))
    pc_id = "%x" % id(pc)
    PCS[pc_id] = (pc, pl)

    @pc.on("connectionstatechange")
    async def _on_state():
        log.info("[webrtc] connection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            await _close_pc(pc_id)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type=params["type"]))
    await pl.start()
    track = EncodedH264Track(pl)
    for t in pc.getTransceivers():
        if t.kind == "video":
            caps = RTCRtpSender.getCapabilities("video")
            prefs = [c for c in caps.codecs if c.mimeType.lower() == "video/h264"]
            prefs += [c for c in caps.codecs if c.mimeType.lower() == "video/rtx"]
            t.setCodecPreferences(prefs)
    pc.addTrack(track)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
                              "id": pc_id})


async def _close_pc(pc_id):
    item = PCS.pop(pc_id, None)
    if item:
        pc, pl = item
        await pl.stop()
        await pc.close()


async def rtc_close(req):
    await _close_pc(req.match_info["id"])
    return web.Response(status=204)


async def favicon(_req):
    return web.Response(status=204)


async def status(_req):
    return web.json_response({"server_ms": now_ms(), "capture": CAPTURE, "display": DISPLAY,
                              "active": [p.label for p in Pipeline.active], "rtc": RTC_STATS})


async def on_shutdown(_app):
    for p in list(Pipeline.active):
        await p.stop()
    await asyncio.gather(*[_close_pc(i) for i in list(PCS)], return_exceptions=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aioice").setLevel(logging.WARNING)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/clock.html", clock_page)
    app.router.add_get("/ws/clock", ws_clock)
    app.router.add_get("/ws/mse", ws_mse)
    app.router.add_get("/ws/wc", ws_wc)
    app.router.add_post("/offer", offer)
    app.router.add_post("/rtc/close/{id}", rtc_close)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_post("/report", report)
    app.router.add_get("/status", status)
    app.router.add_static("/static/", HERE / "static")
    app.on_shutdown.append(on_shutdown)
    log.info("capture region %s on %s; audio monitor: %s", CAPTURE, DISPLAY,
             detect_monitor_source())
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    sys.exit(main())
