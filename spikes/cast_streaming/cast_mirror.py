#!/usr/bin/env python3
"""AirLinx spike: natywny Cast Streaming (Chrome Mirroring, app 0F5096E8) z Linuksa.

Przepływ: TLS CASTv2 -> CONNECT -> LAUNCH 0F5096E8 -> CONNECT(transportId) ->
OFFER (urn:x-cast:com.google.cast.webrtc) -> ANSWER (udpPort, ssrc) ->
ffmpeg x11grab|h264_nvenc|flv -> AES-CTR -> Cast RTP -> UDP; RTCP SR co 250 ms;
odbiór RTCP odbiornika (Cast feedback ACK/NACK, PLI, RR, receiver log) ->
retransmisje. Na końcu zawsze STOP sesji.

Użycie: python cast_mirror.py --seconds 20 [--delay 100] [--fps 30] [--bitrate 6000000]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import select
import signal
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time

import cast_rtp as cr
import castv2
import flv_h264
import ogg_opus

log = logging.getLogger("mirror")
MASK24 = (1 << 24) - 1


def mono_ms() -> int:
    return int(time.monotonic() * 1000)


def sdiff24(a: int, b: int) -> int:
    """(a - b) mod 2^24 jako liczba ze znakiem."""
    d = (a - b) & MASK24
    return d - (1 << 24) if d >= (1 << 23) else d


class Sender:
    def __init__(self, args, tv_ip: str, udp_port: int, sender_ssrc: int, receiver_ssrc: int,
                 key: bytes, iv: bytes, pt: int, target_delay_ms: int, *, timebase: int = 90000,
                 kind: str = "video", sock: socket.socket | None = None):
        self.args = args
        self.kind = kind
        self.timebase = timebase
        self.dest = (tv_ip, udp_port)
        self.ssrc = sender_ssrc
        self.rssrc = receiver_ssrc
        self.pt = pt
        self.crypto = cr.FrameCrypto(key, iv)
        # kontrola negatywna: szyfrujemy innym kluczem niż w OFFER -> odbiornik nie zdekoduje
        self.bad_crypto = cr.FrameCrypto(os.urandom(16), iv) if args.bad_key else None
        self.target_delay_ms = target_delay_ms
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            sock.bind(("0.0.0.0", 0))
            sock.connect(self.dest)
        self.sock = sock  # wszystkie strumienie: jeden socket -> ten sam udpPort TV
        self.lock = threading.Lock()
        self.seq = random.randint(0, 0xFFFF)
        self.next_fid = 0
        self.checkpoint = -1
        self.frames: dict[int, dict] = {}
        # Jak openscreen/Chrome: RTP ts = (capture - wspólny początek sesji) * timebase, BEZ losowej bazy.
        # Odbiornik (Chromium cast_streaming) używa RTP ts jako timestampów mediów, więc audio i wideo
        # muszą mieć wspólne zero, inaczej renderer czeka na wideo "z przyszłości" (zaobserwowane: run7).
        self.rtp_base = 0
        self.origin = None  # monotonic początku sesji, wspólny dla strumieni
        self.t0_mono = None
        self.pkt_count = 0
        self.octets = 0
        self.alive = True
        self.waiting_key = False
        self.sr_sent = {}  # lsr(mid32) -> mono time
        # statystyki
        self.st = dict(frames=0, keyframes=0, bytes=0, packets=0, retx=0, nack_msgs=0, pli=0,
                       rtcp=0, dropped=0, acked=0)
        self.rtt_ms: list[float] = []
        self.ack_lat: list[float] = []      # send(first pkt) -> RTCP z ACK (nasz zegar)
        self.cap2ack: list[float] = []      # capture (x11grab) -> RTCP z ACK
        self.cap2send: list[float] = []     # capture -> wysłanie 1. pakietu (encode+pipe)
        self.raw_cap2out: list[float] = []  # surowe (ffmpeg pts) -> wyjście z ffmpeg
        self.playout_delays = set()
        self.recv_events: dict[str, int] = {}
        self.fwd_samples: list[int] = []    # recv_ts(PacketReceived) - our_send (mod 2^24)
        self.bwd_samples: list[int] = []    # our_rx - recv_ts(AckSent)
        self.e2e_est: list[float] = []      # capture -> RenderDelay/PlayedOut (szac.)
        self.decoded_rtp = set()
        self.render_delay_vals: list[int] = []
        self.acked_ids: list[int] = []
        self.first_rtcp_dump = 0
        self.log_pending: list = []  # (rx_ms, rtcp logs)
        self.audio_lag: list[float] = []

    # ---------------- RTP ----------------
    def rtp_ts_for(self, mono_s: float) -> int:
        return (self.rtp_base + int(round((mono_s - self.origin) * self.timebase))) & 0xFFFFFFFF

    def send_sr(self) -> None:
        if self.t0_mono is None:
            return
        now_wall, now_mono = time.time(), time.monotonic()
        ntp = cr.ntp_now(now_wall)
        pkt = cr.build_sender_report(self.ssrc, ntp, self.rtp_ts_for(now_mono), self.pkt_count, self.octets)
        self.sr_sent[(ntp >> 16) & 0xFFFFFFFF] = now_mono
        if len(self.sr_sent) > 64:
            for k in list(self.sr_sent)[:-32]:
                del self.sr_sent[k]
        self.sock.send(pkt)

    def on_frame(self, fr: flv_h264.H264Frame) -> None:
        now_mono, now_wall = time.monotonic(), time.time()
        # x11grab (ffmpeg 4.4) stempluje ramki av_gettime() = CLOCK_REALTIME; z -copyts
        # FLV muxer zapisuje ts w ms, ale obcięty do 31 bitów (bajt extended & 0x7F).
        now_ms = int(now_wall * 1000)
        cap_ms = (now_ms & ~0x7FFFFFFF) | (fr.ts_ms & 0x7FFFFFFF)
        if cap_ms > now_ms + 1000:
            cap_ms -= 1 << 31
        cap_s = now_mono - (now_wall - cap_ms / 1000.0)  # chwila przechwycenia w zegarze monotonic
        # pts z x11grab bywa ~1 ramkę "w przyszłości" względem wyjścia z enkodera -> nie
        # pozwalamy, by czas referencyjny był późniejszy niż teraz (inaczej TV dokłada opóźnienie)
        self.raw_cap2out.append((now_mono - cap_s) * 1000)
        cap_s = min(cap_s, now_mono)
        self.enqueue(fr.data, fr.key, cap_s)

    def enqueue(self, data: bytes, key: bool, cap_s: float, rtp_ts: int | None = None) -> None:
        """Wspólne dla audio i wideo: szyfrowanie, pakietowanie, wysyłka. cap_s = monotonic."""
        class fr:  # noqa: N801  (zachowuje nazwy pól używane niżej)
            pass
        fr.data, fr.key = data, key
        with self.lock:
            if self.t0_mono is None:
                self.t0_mono = cap_s
                self.send_sr()  # SR musi dojść przed pakietem 0 pierwszej ramki
            fid = self.next_fid
            if fid - self.checkpoint > 100:  # kMaxUnackedFrames = 120
                self.waiting_key = True
            if self.waiting_key and not fr.key:
                self.st["dropped"] += 1
                return
            self.waiting_key = False
            self.next_fid += 1
            enc = (self.bad_crypto or self.crypto).crypt(fid, fr.data)
            if rtp_ts is None:
                rtp_ts = self.rtp_ts_for(cap_s)
            pkts = cr.packetize(payload=enc, frame_id=fid, ref_frame_id=fid if fr.key else fid - 1,
                                key_frame=fr.key, rtp_ts=rtp_ts, ssrc=self.ssrc, pt=self.pt,
                                seq_start=self.seq,
                                new_playout_delay_ms=self.target_delay_ms if fid == 0 else 0)
            self.seq = (self.seq + len(pkts)) & 0xFFFF
            ent = dict(pkts=pkts, last_send=[0.0] * len(pkts), cap=cap_s, rtp=rtp_ts, key=fr.key,
                       first_send=None, acked=False, size=len(fr.data))
            self.frames[fid] = ent
            for old in [k for k in self.frames if k < fid - 300]:
                del self.frames[old]
        # wysyłka poza lockiem, z prostym pacingiem dla dużych ramek
        t_first = time.monotonic()
        ent["first_send"] = t_first
        self.cap2send.append((t_first - cap_s) * 1000)
        rate = self.args.pace_mbps * 1e6 / 8  # B/s
        sent = 0
        for i, p in enumerate(pkts):
            self._send_pkt(ent, i)
            sent += len(p)
            if rate > 0 and len(pkts) > 8:
                ahead = sent / rate - (time.monotonic() - t_first)
                if ahead > 0.0007:
                    time.sleep(ahead)
        self.st["frames"] += 1
        self.st["keyframes"] += int(fr.key)
        self.st["bytes"] += len(fr.data)

    def _send_pkt(self, ent, i, retx=False) -> None:
        p = ent["pkts"][i]
        try:
            self.sock.send(p)
        except BlockingIOError:
            time.sleep(0.001)
            self.sock.send(p)
        ent["last_send"][i] = time.monotonic()
        self.pkt_count += 1
        self.octets += len(p) - cr.BASE_HDR
        self.st["packets"] += 1
        if retx:
            self.st["retx"] += 1

    # ---------------- RTCP ----------------
    def rtcp_loop(self) -> None:
        self.sock.settimeout(0.2)
        while self.alive:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                if self.alive:
                    log.warning("udp recv: %s", e)
                continue
            rx = time.monotonic()
            if not data or (data[1] < 200 or data[1] > 207):
                continue
            self.st["rtcp"] += 1
            r = cr.parse_receiver_rtcp(data)
            if self.first_rtcp_dump < 3:
                self.first_rtcp_dump += 1
                log.info("RTCP#%d raw=%s parsed=%s", self.first_rtcp_dump, data.hex(), r)
            self.handle_rtcp(r, rx)

    def handle_rtcp(self, r: cr.RtcpResult, rx: float) -> None:
        if r.report_block and r.report_block["lsr"]:
            t_sr = self.sr_sent.get(r.report_block["lsr"])
            if t_sr is not None:
                rtt = (rx - t_sr) * 1000 - r.report_block["dlsr"] / 65.536
                self.rtt_ms.append(rtt)
        if r.pli:
            self.st["pli"] += 1
        if r.playout_delay_ms is not None:
            self.playout_delays.add(r.playout_delay_ms)
        resend = []
        with self.lock:
            last = self.next_fid - 1
            if r.checkpoint8 is not None and last >= 0:
                ck = cr.expand8(r.checkpoint8, last)
                newly = []
                if ck > self.checkpoint:
                    for f in range(self.checkpoint + 1, ck + 1):
                        newly.append(f)
                    self.checkpoint = ck
                for rel in r.acks8_rel:
                    newly.append(self.checkpoint + rel)
                for f in newly:
                    e = self.frames.get(f)
                    if e and not e["acked"]:
                        e["acked"] = True
                        self.st["acked"] += 1
                        self.acked_ids.append(f)
                        if e["first_send"]:
                            self.ack_lat.append((rx - e["first_send"]) * 1000)
                        self.cap2ack.append((rx - e["cap"]) * 1000)
                if r.nacks:
                    self.st["nack_msgs"] += 1
                for f8, pid, bits in r.nacks:
                    fid = cr.expand8(f8, self.checkpoint, greater_than=True)
                    e = self.frames.get(fid)
                    if not e:
                        continue
                    if pid == cr.ALL_PACKETS_LOST:
                        ids = range(len(e["pkts"]))
                    else:
                        ids = [pid] + [pid + 1 + b for b in range(8) if bits & (1 << b)]
                    for i in ids:
                        if i < len(e["pkts"]) and rx - e["last_send"][i] > 0.010:
                            resend.append((e, i))
        for e, i in resend:
            self._send_pkt(e, i, retx=True)
        if r.logs:
            self.handle_logs(r.logs, rx)

    def handle_logs(self, logs, rx: float) -> None:
        rx_ms = int(rx * 1000) & MASK24
        rtp2frame = {e["rtp"]: e for e in list(self.frames.values())}
        for rtp32, base, evs in logs:
            e = rtp2frame.get(rtp32)
            for name, val, dt in evs:
                self.recv_events[name] = self.recv_events.get(name, 0) + 1
                ts = (base + dt) & MASK24
                if name.endswith("PacketReceived") and e and val < len(e["last_send"]):
                    sent_ms = int(e["last_send"][val] * 1000) & MASK24
                    self.fwd_samples.append(sdiff24(ts, sent_ms))
                elif name.endswith("AckSent"):
                    self.bwd_samples.append(sdiff24(rx_ms, ts))
                elif name.endswith("FrameDecoded"):
                    self.decoded_rtp.add(rtp32)
                elif name.endswith("RenderDelay") or name.endswith("PlayoutDelay"):
                    self.render_delay_vals.append(val)
                    if e is not None:
                        self.log_pending.append((ts, e["cap"]))

    def clock_offset(self):
        """offset = recv_clock - our_clock (ms, mod 2^24); jak NTP: (fwd - bwd)/2."""
        if not self.fwd_samples or not self.bwd_samples:
            return None, None
        fwd, bwd = min(self.fwd_samples), min(self.bwd_samples)
        return (fwd - bwd) / 2.0, (fwd + bwd) / 2.0

    def stats_line(self) -> str:
        def med(v):
            return f"{statistics.median(v[-90:]):.0f}" if v else "-"
        lag = self.next_fid - 1 - self.checkpoint
        return (f"fid={self.next_fid - 1} ckpt={self.checkpoint} lag={lag} frames={self.st['frames']} "
                f"key={self.st['keyframes']} acked={self.st['acked']} rtcp={self.st['rtcp']} "
                f"nack={self.st['nack_msgs']} retx={self.st['retx']} pli={self.st['pli']} drop={self.st['dropped']} "
                f"rtt={med(self.rtt_ms)}ms cap2send={med(self.cap2send)}ms send2ack={med(self.ack_lat)}ms "
                f"cap2ack={med(self.cap2ack)}ms playoutDelay={sorted(self.playout_delays)} "
                f"rxEvents={self.recv_events}")


def rtcp_router(sock: socket.socket, streams: dict) -> None:
    """Jeden socket UDP dla wszystkich strumieni; RTCP rozdzielamy po SSRC odbiornika
    (pierwsze słowo po nagłówku pierwszego pakietu compound – jak SenderPacketRouter)."""
    sock.settimeout(0.2)
    any_s = next(iter(streams.values()))
    while any_s.alive:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            continue
        rx = time.monotonic()
        if len(data) < 8 or not (200 <= data[1] <= 207):
            continue
        s_ = streams.get(struct.unpack_from(">I", data, 4)[0])
        if s_ is None:
            continue
        s_.st["rtcp"] += 1
        r = cr.parse_receiver_rtcp(data)
        if s_.first_rtcp_dump < 2:
            s_.first_rtcp_dump += 1
            log.info("RTCP[%s]#%d raw=%s parsed=%s", s_.kind, s_.first_rtcp_dump, data.hex(), r)
        s_.handle_rtcp(r, rx)


def start_ffmpeg(args) -> subprocess.Popen:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-copyts",
           "-f", "x11grab", "-draw_mouse", "1", "-framerate", str(args.fps),
           "-video_size", args.capture_size, "-i", f"{args.display}+{args.capture_offset}",
           "-vf", f"scale={args.width}:{args.height}:flags=fast_bilinear,format=nv12",
           "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-delay", "0",
           "-bf", "0", "-g", str(args.gop), "-forced-idr", "1", "-profile:v", "main",
           "-rc", "cbr", "-b:v", str(args.bitrate), "-maxrate", str(args.bitrate),
           "-bufsize", str(args.bitrate // 2),
           "-flush_packets", "1", "-f", "flv", "pipe:1"]
    log.info("ffmpeg: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0,
                            env=dict(os.environ, DISPLAY=args.display))


def default_monitor() -> str:
    out = subprocess.run(["pactl", "info"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip() + ".monitor"
    return "default"


def start_ffmpeg_audio(args) -> subprocess.Popen:
    # Opus 10 ms, lowdelay; strony Ogg co 10 ms, żeby muxer nie buforował
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
           "-f", "pulse", "-fragment_size", "1920", "-i", args.audio_source,
           "-ac", "2", "-ar", "48000", "-c:a", "libopus", "-b:a", str(args.audio_bitrate),
           "-application", "lowdelay", "-frame_duration", "10", "-page_duration", "10000",
           "-flush_packets", "1", "-f", "ogg", "pipe:1"]
    log.info("ffmpeg audio: %s", " ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)


def build_offer(args, ssrc: int, key: bytes, iv: bytes, seq: int, audio: dict | None = None) -> dict:
    video = {
        "index": 0, "type": "video_source", "codecName": "h264", "rtpProfile": "cast",
        "rtpPayloadType": args.pt, "ssrc": ssrc, "maxFrameRate": f"{args.fps * 1000}/1000",
        "timeBase": "1/90000", "maxBitRate": args.bitrate, "profile": "main", "level": "4",
        "targetDelay": args.delay, "aesKey": key.hex(), "aesIvMask": iv.hex(),
        "receiverRtcpEventLog": True, "channels": 1,
        "resolutions": [{"width": args.width, "height": args.height}],
    }
    streams = [video]
    if audio:
        streams.append({
            "index": 1, "type": "audio_source", "codecName": "opus", "rtpProfile": "cast",
            "rtpPayloadType": 127, "ssrc": audio["ssrc"], "targetDelay": args.delay,
            "aesKey": audio["key"].hex(), "aesIvMask": audio["iv"].hex(), "bitRate": args.audio_bitrate,
            "sampleRate": 48000, "timeBase": "1/48000", "channels": 2, "receiverRtcpEventLog": True})
    return {"type": "OFFER", "seqNum": seq,
            "offer": {"castMode": "mirroring", "receiverGetStatus": True, "supportedStreams": streams}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv", default=os.environ.get("AIRLINX_TV_IP"), required=not os.environ.get("AIRLINX_TV_IP"),
                    help="adres TV (albo zmienna AIRLINX_TV_IP)")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--delay", type=int, default=100, help="targetDelay (ms) w OFFER")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--gop", type=int, default=120)
    ap.add_argument("--bitrate", type=int, default=6_000_000)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--display", default=":1")
    ap.add_argument("--capture-size", default="4096x2304")
    ap.add_argument("--capture-offset", default="0,420")
    ap.add_argument("--pt", type=int, default=96, help="RTP PT (Chrome: 96 = 'Android TV hack'; spec: 101)")
    ap.add_argument("--pace-mbps", type=float, default=80, help="pacing dużych ramek (0 = burst)")
    ap.add_argument("--bad-key", action="store_true", help="kontrola negatywna: zły klucz AES")
    ap.add_argument("--force", action="store_true", help="uruchom mimo działającej aplikacji na TV")
    ap.add_argument("--offer-only", action="store_true", help="tylko OFFER/ANSWER, bez streamu")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--audio-source", default=None, help="źródło Pulse (domyślnie monitor domyślnego wyjścia)")
    ap.add_argument("--audio-bitrate", type=int, default=128000)
    ap.add_argument("--audio-latency-ms", type=float, default=20,
                    help="szac. opóźnienie przechwycenia+kodowania audio (do synchronizacji z wideo)")
    ap.add_argument("--log", default=None)
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log:
        handlers.append(logging.FileHandler(args.log, mode="w"))
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, handlers=handlers,
                        format="%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    ch = castv2.CastChannel(args.tv)
    ch.connect()
    st = ch.get_status()
    log.info("receiver apps before: %s", [a.get("displayName") for a in st["status"].get("applications", [])])
    app = None
    ff = None
    extra_procs: list = []
    sender = None
    stop_flag = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop_flag.set())
    busy = [a for a in st["status"].get("applications", [])
            if not a.get("isIdleScreen") and a.get("appId") not in ("E8C28D3C", castv2.MIRRORING_APP_ID)]
    if busy and not args.force:
        log.error("TV zajęty (%s) – nie przerywam. Użyj --force, jeśli to zamierzone.",
                  [a.get("displayName") for a in busy])
        ch.close()
        return 3
    try:
        app = ch.launch(castv2.MIRRORING_APP_ID)
        log.info("LAUNCHED: %s", json.dumps(app))
        tid = app["transportId"]
        ch.connect(tid)
        ssrc = random.randint(1, 0x7FFFFFFF)
        key, iv = os.urandom(16), os.urandom(16)
        audio = None
        if not args.no_audio:
            args.audio_source = args.audio_source or default_monitor()
            audio = dict(ssrc=random.randint(1, 0x7FFFFFFF), key=os.urandom(16), iv=os.urandom(16))
        offer = build_offer(args, ssrc, key, iv, ch.next_request_id(), audio)
        log.info("OFFER: %s", json.dumps(offer))
        ch.send(castv2.NS_WEBRTC, offer, tid)
        _, _, ans, raw = ch.wait_for(lambda ns, s, p: ns == castv2.NS_WEBRTC and p and p.get("type") == "ANSWER",
                                     timeout=15)
        log.info("ANSWER (raw): %s", raw)
        if ans.get("result") != "ok":
            log.error("ANSWER not ok")
            return 2
        a = ans["answer"]
        port = a["udpPort"]
        idx2ssrc = dict(zip(a["sendIndexes"], a["ssrcs"]))
        rssrc = idx2ssrc[0]
        if audio and 1 not in idx2ssrc:
            log.warning("TV nie przyjął strumienia audio")
            audio = None
        if args.offer_only:
            time.sleep(2)
            return 0
        ch.add_listener(lambda ns, s, d, p: log.info("CAST msg %s from %s: %s", ns, s, json.dumps(p)[:1500])
                        if ns not in (castv2.NS_RECEIVER,) else None)
        sender = Sender(args, args.tv, port, ssrc, rssrc, key, iv, args.pt, args.delay)
        streams = {rssrc: sender}
        sender.origin = time.monotonic() - 1.0  # zapas, by żadna ramka nie miała ujemnego ts
        asender = None
        if audio:
            asender = Sender(args, args.tv, port, audio["ssrc"], idx2ssrc[1], audio["key"], audio["iv"], 127,
                             args.delay, timebase=48000, kind="audio", sock=sender.sock)
            asender.bad_crypto = None
            asender.origin = sender.origin
            streams[idx2ssrc[1]] = asender
        threading.Thread(target=rtcp_router, args=(sender.sock, streams), daemon=True).start()

        def sr_loop():
            while sender.alive:
                for s_ in streams.values():
                    with s_.lock:
                        s_.send_sr()
                time.sleep(0.25)
        threading.Thread(target=sr_loop, daemon=True).start()

        ff = start_ffmpeg(args)

        def reader():
            try:
                for fr in flv_h264.iter_frames(ff.stdout):
                    if not sender.alive:
                        break
                    sender.on_frame(fr)
            except EOFError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("reader")
        threading.Thread(target=reader, daemon=True).start()

        if asender:
            ffa = start_ffmpeg_audio(args)
            extra_procs.append(ffa)

            def areader():
                t_anchor = None
                last_cap = None
                samples = 0
                try:
                    for pk in ogg_opus.iter_packets(ffa.stdout):
                        if not asender.alive:
                            break
                        now = time.monotonic()
                        cap = None if t_anchor is None else t_anchor + samples / 48000
                        # (re)kotwiczenie: start, albo dryf zegara karty vs monotonic > 40 ms
                        if cap is None or cap > now or now - cap > args.audio_latency_ms / 1000 + 0.04:
                            if t_anchor is not None:
                                asender.st["reanchor"] = asender.st.get("reanchor", 0) + 1
                            t_anchor = now - args.audio_latency_ms / 1000 - samples / 48000
                            cap = t_anchor + samples / 48000
                            if last_cap is not None and cap <= last_cap:  # RTP musi rosnąć
                                t_anchor += last_cap + 0.001 - cap
                                cap = last_cap + 0.001
                        last_cap = cap
                        if asender.t0_mono is None:
                            asender.t0_mono = cap
                            with asender.lock:
                                asender.send_sr()
                        asender.audio_lag.append((now - cap) * 1000)
                        # RTP ts = licznik próbek (dokładny); czas referencyjny = cap
                        asender.enqueue(pk, True, cap, rtp_ts=asender.rtp_ts_for(cap))
                        samples += ogg_opus.opus_samples(pk)
                except EOFError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("audio reader")
            threading.Thread(target=areader, daemon=True).start()

        t_end = time.time() + args.seconds
        probes_sent = False
        while time.time() < t_end and not stop_flag.is_set():
            time.sleep(1)
            if int(t_end - time.time()) % 3 == 0:  # MEDIA_STATUS co ~3 s (playerState/currentTime)
                ch.send("urn:x-cast:com.google.cast.media", {"type": "GET_STATUS", "requestId": ch.next_request_id()}, tid)
            if not probes_sent and time.time() > t_end - args.seconds + 3:
                probes_sent = True
                # zapytania diagnostyczne (odpowiedzi logowane przez listener "CAST msg")
                ch.send(castv2.NS_REMOTING, {"type": "GET_CAPABILITIES", "seqNum": ch.next_request_id()}, tid)
                ch.send("urn:x-cast:com.google.cast.media", {"type": "GET_STATUS", "requestId": ch.next_request_id()}, tid)
                ch.send(castv2.NS_WEBRTC, {"type": "GET_STATUS", "seqNum": ch.next_request_id(),
                                           "get_status": ["wifiSnr", "wifiSpeed"]}, tid)
            log.info("STAT %s", sender.stats_line())
            if asender:
                log.info("STAT-AUDIO %s", asender.stats_line())
            if not ch._alive:
                log.error("cast channel died")
                break
        # podsumowanie
        off, owd = sender.clock_offset()
        log.info("SUMMARY %s", sender.stats_line())
        if sender.rtt_ms:
            log.info("RTT ms: min=%.1f med=%.1f max=%.1f n=%d", min(sender.rtt_ms), statistics.median(sender.rtt_ms),
                     max(sender.rtt_ms), len(sender.rtt_ms))
        for name, v in (("raw_pts2out", sender.raw_cap2out), ("cap2send", sender.cap2send), ("send2ack", sender.ack_lat), ("cap2ack", sender.cap2ack)):
            if v:
                q = statistics.quantiles(v, n=10) if len(v) >= 10 else [min(v)] * 9
                log.info("%s ms: min=%.1f p50=%.1f p90=%.1f max=%.1f n=%d", name, min(v), statistics.median(v),
                         q[8], max(v), len(v))
        log.info("receiver log events: %s; decoded frames (by rtp ts): %d", sender.recv_events,
                 len(sender.decoded_rtp))
        if sender.render_delay_vals:
            log.info("RenderDelay/PlayoutDelay deltas (ms, recv-reported): med=%s min=%s max=%s",
                     statistics.median(sender.render_delay_vals), min(sender.render_delay_vals),
                     max(sender.render_delay_vals))
        if off is not None:
            e2e = []
            for ts, cap in sender.log_pending:
                cap_ms24 = int(cap * 1000) & MASK24
                e2e.append(sdiff24(ts, cap_ms24) - off)
            log.info("clock offset est (recv-ours)=%.1f ms, one-way est=%.1f ms (fwd=%d bwd=%d samples)",
                     off, owd, len(sender.fwd_samples), len(sender.bwd_samples))
            if e2e:
                log.info("ESTIMATED capture->render-event ms: min=%.1f med=%.1f max=%.1f n=%d",
                         min(e2e), statistics.median(e2e), max(e2e), len(e2e))
        log.info("first acked frame ids: %s ... last: %s", sender.acked_ids[:10], sender.acked_ids[-5:])
        if asender:
            log.info("SUMMARY-AUDIO %s reanchor=%s", asender.stats_line(), asender.st.get("reanchor", 0))
            for name, v in (("audio send2ack", asender.ack_lat), ("audio cap2ack", asender.cap2ack)):
                if v:
                    log.info("%s ms: min=%.1f p50=%.1f max=%.1f n=%d", name, min(v), statistics.median(v), max(v), len(v))
        return 0
    finally:
        for s_ in (sender, locals().get("asender")):
            if s_:
                s_.alive = False
        for pr in extra_procs:
            pr.terminate()
        if ff:
            ff.terminate()
            try:
                ff.wait(2)
            except subprocess.TimeoutExpired:
                ff.kill()
        if app:
            log.info("STOP session %s", app.get("sessionId"))
            try:
                ch.stop(app["sessionId"])
                time.sleep(1.0)
                st = ch.get_status()
                log.info("receiver apps after STOP: %s",
                         [x.get("displayName") for x in st["status"].get("applications", [])])
            except Exception as e:  # noqa: BLE001
                log.warning("STOP failed: %s", e)
        ch.close()


if __name__ == "__main__":
    sys.exit(main())
