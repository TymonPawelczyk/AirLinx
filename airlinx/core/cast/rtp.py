# Portions derived from Chromium openscreen (BSD-3-Clause, Copyright 2018 The Chromium Authors); see THIRD_PARTY_NOTICES.md.
"""Cast Streaming media plane (port of openscreen cast/streaming/impl).

- :class:`FrameCrypto` – AES-128-CTR per frame, nonce = (0^8 || BE32(frame_id) || 0^4) XOR aesIvMask
- :func:`packetize` – 12 B RTP + 7 B Cast header (+4 B adaptive-latency extension)
- :func:`build_sender_report`, :func:`parse_receiver_rtcp` – RTCP SR / RR, Cast feedback (ACK/NACK),
  PLI, XR RRTR, receiver frame log
- :class:`StreamSender` – one outgoing stream: frame ids, retransmit buffer, ACK tracking, stats
- :class:`Pacer` – token-bucket pacing; without it the TV's Wi-Fi drops keyframe bursts
- :class:`RtcpRouter` – one UDP socket for all streams, demux by receiver SSRC

Everything here is pure (no sockets, injectable clocks) so it can be tested offline.
"""
from __future__ import annotations

import collections
import statistics
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_PACKET_SIZE = 1500 - 20 - 8          # kMaxRtpPacketSizeForIpv4UdpOnEthernet
BASE_HDR = 12 + 7                        # RtpPacketizer::kBaseRtpHeaderSize
ADAPTIVE_LATENCY_HDR = 4
MAX_PAYLOAD = MAX_PACKET_SIZE - BASE_HDR - ADAPTIVE_LATENCY_HDR

RTCP_SR, RTCP_RR, RTCP_SDES, RTCP_APP, RTCP_PSFB, RTCP_XR = 200, 201, 202, 204, 206, 207
SUB_PLI, SUB_RECEIVER_LOG, SUB_FEEDBACK = 1, 2, 15
CAST = 0x43415354  # 'CAST'
CST2 = 0x43535432  # 'CST2'
ALL_PACKETS_LOST = 0xFFFF

MAX_UNACKED_FRAMES = 120                 # kMaxUnackedFrames
UNACKED_SOFT_LIMIT = 100                 # stop sending delta frames, wait for keyframe
FRAME_HISTORY = 300
NTP_EPOCH_OFFSET = 2208988800            # 1900 -> 1970

VIDEO_PT, AUDIO_PT = 96, 127

WIRE_EVENTS = {1: "AudioAckSent", 2: "AudioPlayoutDelay", 3: "AudioFrameDecoded", 4: "AudioPacketReceived",
               5: "VideoAckSent", 6: "VideoRenderDelay", 7: "VideoFrameDecoded", 8: "VideoPacketReceived",
               11: "AckSent", 12: "RenderDelay", 13: "FrameDecoded", 14: "PacketReceived"}


# --------------------------------------------------------------------------------------
# Crypto / packetization
# --------------------------------------------------------------------------------------
class FrameCrypto:
    def __init__(self, key: bytes, iv_mask: bytes):
        if len(key) != 16 or len(iv_mask) != 16:
            raise ValueError("AES key and IV mask must be 16 bytes")
        self.key = key
        self.iv_mask = iv_mask
        self._alg = algorithms.AES(key)
        self._mask_int = int.from_bytes(iv_mask, "big")

    def nonce(self, frame_id: int) -> bytes:
        base = (frame_id & 0xFFFFFFFF) << 32
        return (base ^ self._mask_int).to_bytes(16, "big")

    def crypt(self, frame_id: int, data: bytes) -> bytes:
        # AES_ctr128_encrypt increments the whole 128-bit counter big-endian == modes.CTR
        enc = Cipher(self._alg, modes.CTR(self.nonce(frame_id))).encryptor()
        return enc.update(data) + enc.finalize()


def packetize(*, payload: bytes, frame_id: int, ref_frame_id: int, key_frame: bool, rtp_ts: int,
              ssrc: int, pt: int, seq_start: int, new_playout_delay_ms: int = 0) -> list[bytes]:
    """RTP packets of one (already encrypted) frame; sequence numbers start at ``seq_start``."""
    n = max(1, -(-len(payload) // MAX_PAYLOAD))
    if n > 0xFFFE:
        raise ValueError("frame too large")
    out = []
    for pid in range(n):
        last = pid == n - 1
        chunk = payload[pid * MAX_PAYLOAD:(pid + 1) * MAX_PAYLOAD]
        ext = pid == 0 and new_playout_delay_ms > 0
        hdr = struct.pack(">BBHII", 0x80, (0x80 if last else 0) | (pt & 0x7F), (seq_start + pid) & 0xFFFF,
                          rtp_ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
        b12 = (0x80 if key_frame else 0) | 0x40 | (1 if ext else 0)
        hdr += struct.pack(">BBHHB", b12, frame_id & 0xFF, pid, n - 1, ref_frame_id & 0xFF)
        if ext:
            hdr += struct.pack(">HH", (1 << 10) | 2, new_playout_delay_ms & 0xFFFF)
        out.append(hdr + chunk)
    return out


def ntp_from_unix(t: float) -> int:
    sec = int(t) + NTP_EPOCH_OFFSET
    frac = int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF
    return (sec << 32) | frac


def ntp_now() -> int:
    return ntp_from_unix(time.time())


def build_sender_report(ssrc: int, ntp: int, rtp_ts: int, pkt_count: int, octet_count: int) -> bytes:
    payload = struct.pack(">IQIII", ssrc & 0xFFFFFFFF, ntp, rtp_ts & 0xFFFFFFFF, pkt_count & 0xFFFFFFFF,
                          octet_count & 0xFFFFFFFF)
    return struct.pack(">BBH", 0x80, RTCP_SR, len(payload) // 4) + payload


# --------------------------------------------------------------------------------------
# RTCP parsing
# --------------------------------------------------------------------------------------
@dataclass
class RtcpResult:
    report_block: Optional[dict] = None
    checkpoint8: Optional[int] = None           # 8-bit checkpoint frame id
    playout_delay_ms: Optional[int] = None
    nacks: list = field(default_factory=list)       # (frame8, packet_id, bitvec)
    acks8_rel: list = field(default_factory=list)   # offsets relative to checkpoint (+2..)
    pli: bool = False
    rrtr_ntp: Optional[int] = None
    logs: list = field(default_factory=list)        # (rtp_ts32, base_ms, [(event, value, dt_ms)])
    feedback_count: Optional[int] = None
    unknown: list = field(default_factory=list)


def parse_receiver_rtcp(buf: bytes) -> RtcpResult:
    r = RtcpResult()
    off = 0
    while off + 4 <= len(buf):
        b0, pt, ln = struct.unpack_from(">BBH", buf, off)
        if b0 >> 6 != 2:
            r.unknown.append(("badver", b0))
            break
        sub = b0 & 0x1F
        body = buf[off + 4: off + 4 + ln * 4]
        off += 4 + ln * 4
        try:
            _parse_one(r, pt, sub, body)
        except struct.error:
            r.unknown.append(("truncated", pt, sub, len(body)))
    return r


def _parse_one(r: RtcpResult, pt: int, sub: int, body: bytes) -> None:
    if pt == RTCP_RR:
        if sub >= 1 and len(body) >= 4 + 24:
            (rssrc, to_ssrc, lost, ehsn, jitter, lsr, dlsr) = struct.unpack_from(">IIIIIII", body, 0)
            r.report_block = dict(receiver_ssrc=rssrc, to_ssrc=to_ssrc, fraction_lost=lost >> 24,
                                  cum_lost=lost & 0xFFFFFF, ehsn=ehsn, jitter=jitter, lsr=lsr, dlsr=dlsr)
    elif pt == RTCP_PSFB and sub == SUB_FEEDBACK:
        if len(body) < 16:
            return
        _rssrc, _sssrc, ident = struct.unpack_from(">III", body, 0)
        if ident != CAST:
            return
        ckpt, nloss, delay = struct.unpack_from(">BBH", body, 12)
        r.checkpoint8, r.playout_delay_ms = ckpt, delay
        p = 16
        for _ in range(nloss):
            f8, pid, bits = struct.unpack_from(">BHB", body, p)
            r.nacks.append((f8, pid, bits))
            p += 4
        if len(body) >= p + 6 and struct.unpack_from(">I", body, p)[0] == CST2:
            r.feedback_count, nocts = struct.unpack_from(">BB", body, p + 4)
            vec = body[p + 6: p + 6 + nocts]
            for i, byte in enumerate(vec):
                for bit in range(8):
                    if byte & (1 << bit):
                        r.acks8_rel.append(2 + i * 8 + bit)
    elif pt == RTCP_PSFB and sub == SUB_PLI:
        r.pli = True
    elif pt == RTCP_XR:
        p = 4
        while p + 4 <= len(body):
            bt, _, bl = struct.unpack_from(">BBH", body, p)
            if bt == 4 and bl == 2:
                r.rrtr_ntp = struct.unpack_from(">Q", body, p + 4)[0]
            p += 4 + bl * 4
    elif pt == RTCP_APP and sub == SUB_RECEIVER_LOG:
        if len(body) < 8:
            return
        p = 8  # ssrc + 'CAST'
        while p + 8 <= len(body):
            rtp32, data = struct.unpack_from(">II", body, p)
            p += 8
            nev = 1 + (data >> 24)
            base = data & 0xFFFFFF
            evs = []
            for _ in range(nev):
                if p + 4 > len(body):
                    break
                dd, tt = struct.unpack_from(">HH", body, p)
                p += 4
                et = tt >> 12
                name = WIRE_EVENTS.get(et, f"ev{et}")
                val = dd if "PacketReceived" in name else struct.unpack(">h", struct.pack(">H", dd))[0]
                evs.append((name, val, tt & 0xFFF))
            r.logs.append((rtp32, base, evs))
    else:
        r.unknown.append((pt, sub, len(body)))


def expand8(lower8: int, ref: int, greater_than: bool = False) -> int:
    """Expand an 8-bit frame id to the full id closest to ``ref`` (<= ref, or > ref)."""
    cand = (ref & ~0xFF) | lower8
    if greater_than:
        while cand <= ref:
            cand += 256
        while cand - 256 > ref:
            cand -= 256
    else:
        while cand > ref:
            cand -= 256
        while cand + 256 <= ref:
            cand += 256
    return cand


def receiver_ssrc_of(datagram: bytes) -> Optional[int]:
    """SSRC of the sender of an RTCP compound packet (first word after the first header)."""
    if len(datagram) < 8 or datagram[0] >> 6 != 2 or not (RTCP_SR <= datagram[1] <= RTCP_XR):
        return None
    return struct.unpack_from(">I", datagram, 4)[0]


# --------------------------------------------------------------------------------------
# Outgoing stream
# --------------------------------------------------------------------------------------
@dataclass
class _Frame:
    fid: int
    pkts: list[bytes]
    cap: float
    rtp: int
    key: bool
    size: int
    created: float
    last_send: list[float] = field(default_factory=list)
    first_send: Optional[float] = None
    acked: bool = False


@dataclass
class OutPacket:
    """Unit of work for the pacer."""
    data: bytes
    sender: "StreamSender"
    fid: int = -1            # -1: not an RTP media packet (e.g. SR)
    idx: int = 0
    retx: bool = False


class _Window:
    """Rolling window of (t, value) samples."""

    def __init__(self, seconds: float = 2.0, maxlen: int = 2000):
        self.seconds = seconds
        self.items: Deque[tuple[float, float]] = collections.deque(maxlen=maxlen)

    def add(self, t: float, v: float) -> None:
        self.items.append((t, v))

    def values(self, now: float) -> list[float]:
        lo = now - self.seconds
        while self.items and self.items[0][0] < lo:
            self.items.popleft()
        return [v for _, v in self.items]

    def median(self, now: float) -> Optional[float]:
        v = self.values(now)
        return statistics.median(v) if v else None

    def sum(self, now: float) -> float:
        return sum(self.values(now))


class StreamSender:
    """State of one Cast RTP stream (audio or video).

    ``origin`` is the shared session zero (monotonic seconds). RTP timestamps of all
    streams MUST be computed from the same origin without a random base; the TV renderer
    treats RTP time as media time (PROTOCOL.md, run7 vs run8).
    """

    def __init__(self, *, kind: str, ssrc: int, receiver_ssrc: int, pt: int, key: bytes, iv_mask: bytes,
                 timebase: int, origin: float, target_delay_ms: int,
                 clock: Callable[[], float] = time.monotonic, seq_start: int = 0):
        self.kind = kind
        self.ssrc = ssrc
        self.receiver_ssrc = receiver_ssrc
        self.pt = pt
        self.crypto = FrameCrypto(key, iv_mask)
        self.timebase = timebase
        self.origin = origin
        self.target_delay_ms = target_delay_ms
        self.clock = clock
        self.seq = seq_start & 0xFFFF
        self.next_fid = 0
        self.checkpoint = -1
        self.frames: dict[int, _Frame] = {}
        self.waiting_key = False
        self.keyframe_wanted = False   # consumer should force an IDR (PLI / stalled window)
        self._delay_ext_frames = 10    # announce playout delay on the first frames
        self.pkt_count = 0
        self.octets = 0
        self.sr_started = False
        self._sr_sent: "collections.OrderedDict[int, float]" = collections.OrderedDict()
        self.playout_delay_ms: Optional[int] = None
        self.fraction_lost: Optional[float] = None
        self.last_rtcp: Optional[float] = None
        # counters
        self.st: dict[str, int] = dict(frames=0, keyframes=0, bytes=0, packets=0, retx=0, nack_msgs=0,
                                       pli=0, rtcp=0, dropped=0, acked=0)
        # rolling windows (seconds, values in ms / bytes)
        self.w_rtt = _Window(5.0)
        self.w_send2ack = _Window(2.0)
        self.w_cap2ack = _Window(2.0)
        self.w_cap2send = _Window(2.0)
        self.w_bytes = _Window(2.0)
        self.w_frames = _Window(2.0)
        self.w_packets = _Window(2.0)
        self.w_retx = _Window(2.0)

    # ---- timing ----------------------------------------------------------------------
    def rtp_ts_for(self, t: float) -> int:
        return int(round((t - self.origin) * self.timebase)) & 0xFFFFFFFF

    def set_target_delay(self, ms: int) -> None:
        if ms != self.target_delay_ms:
            self.target_delay_ms = ms
            self._delay_ext_frames = 10

    # ---- frames ----------------------------------------------------------------------
    def build_frame(self, data: bytes, key: bool, cap_t: float, *, rtp_ts: Optional[int] = None,
                    now: Optional[float] = None) -> Optional[list[OutPacket]]:
        """Encrypt + packetize one encoded frame. Returns None when dropped (waiting for keyframe)."""
        now = self.clock() if now is None else now
        fid = self.next_fid
        if fid - self.checkpoint > UNACKED_SOFT_LIMIT:
            if not self.waiting_key:
                self.keyframe_wanted = True
            self.waiting_key = True
        if self.waiting_key and not key:
            self.st["dropped"] += 1
            return None
        self.waiting_key = False
        self.next_fid += 1
        enc = self.crypto.crypt(fid, data)
        if rtp_ts is None:
            rtp_ts = self.rtp_ts_for(cap_t)
        delay_ext = 0
        if self._delay_ext_frames > 0:
            delay_ext = self.target_delay_ms
            self._delay_ext_frames -= 1
        pkts = packetize(payload=enc, frame_id=fid, ref_frame_id=fid if key else fid - 1, key_frame=key,
                         rtp_ts=rtp_ts, ssrc=self.ssrc, pt=self.pt, seq_start=self.seq,
                         new_playout_delay_ms=delay_ext)
        self.seq = (self.seq + len(pkts)) & 0xFFFF
        fr = _Frame(fid=fid, pkts=pkts, cap=cap_t, rtp=rtp_ts, key=key, size=len(data), created=now,
                    last_send=[0.0] * len(pkts))
        self.frames[fid] = fr
        if len(self.frames) > FRAME_HISTORY:
            for old in [k for k in self.frames if k < fid - FRAME_HISTORY]:
                del self.frames[old]
        self.st["frames"] += 1
        self.st["keyframes"] += int(key)
        self.st["bytes"] += len(data)
        self.w_frames.add(now, 1)
        self.w_bytes.add(now, len(data))
        out = []
        if not self.sr_started:  # receiver drops packet 0 of each frame until it has an SR
            out.append(OutPacket(self.sender_report(now), self))
        out.extend(OutPacket(p, self, fid, i) for i, p in enumerate(pkts))
        return out

    def on_sent(self, pkt: OutPacket, now: float) -> None:
        if pkt.fid < 0:
            return
        fr = self.frames.get(pkt.fid)
        if fr is not None and pkt.idx < len(fr.last_send):
            fr.last_send[pkt.idx] = now
            if fr.first_send is None:
                fr.first_send = now
                self.w_cap2send.add(now, (now - fr.cap) * 1000)
        self.pkt_count += 1
        self.octets += max(0, len(pkt.data) - BASE_HDR)
        self.st["packets"] += 1
        self.w_packets.add(now, 1)
        if pkt.retx:
            self.st["retx"] += 1
            self.w_retx.add(now, 1)

    # ---- RTCP ------------------------------------------------------------------------
    def sender_report(self, now: Optional[float] = None, wall: Optional[float] = None) -> bytes:
        now = self.clock() if now is None else now
        wall = time.time() if wall is None else wall
        ntp = ntp_from_unix(wall)
        self.sr_started = True
        self._sr_sent[(ntp >> 16) & 0xFFFFFFFF] = now
        while len(self._sr_sent) > 64:
            self._sr_sent.popitem(last=False)
        return build_sender_report(self.ssrc, ntp, self.rtp_ts_for(now), self.pkt_count, self.octets)

    def handle_rtcp(self, r: RtcpResult, rx: float) -> list[OutPacket]:
        """Apply receiver feedback; returns packets to retransmit."""
        self.st["rtcp"] += 1
        self.last_rtcp = rx
        rb = r.report_block
        if rb:
            self.fraction_lost = rb["fraction_lost"] / 256.0
            if rb["lsr"]:
                t_sr = self._sr_sent.get(rb["lsr"])
                if t_sr is not None:
                    rtt = (rx - t_sr) * 1000 - rb["dlsr"] / 65.536
                    if 0 <= rtt < 5000:
                        self.w_rtt.add(rx, rtt)
        if r.pli:
            self.st["pli"] += 1
            self.keyframe_wanted = True
        if r.playout_delay_ms is not None:
            self.playout_delay_ms = r.playout_delay_ms
        last = self.next_fid - 1
        if r.checkpoint8 is None or last < 0:
            return []
        ck = expand8(r.checkpoint8, last)
        newly = []
        if ck > self.checkpoint:
            newly.extend(range(self.checkpoint + 1, ck + 1))
            self.checkpoint = ck
        newly.extend(self.checkpoint + rel for rel in r.acks8_rel)
        for f in newly:
            fr = self.frames.get(f)
            if fr is not None and not fr.acked:
                fr.acked = True
                self.st["acked"] += 1
                if fr.first_send is not None:
                    self.w_send2ack.add(rx, (rx - fr.first_send) * 1000)
                self.w_cap2ack.add(rx, (rx - fr.cap) * 1000)
        resend: list[OutPacket] = []
        if r.nacks:
            self.st["nack_msgs"] += 1
        rtt = self.w_rtt.median(rx)
        min_gap = max(0.010, (rtt or 0) / 1000.0)
        for f8, pid, bits in r.nacks:
            fid = expand8(f8, self.checkpoint, greater_than=True)
            fr = self.frames.get(fid)
            if fr is None or fr.acked:
                continue
            if pid == ALL_PACKETS_LOST:
                ids: Any = range(len(fr.pkts))
            else:
                ids = [pid] + [pid + 1 + b for b in range(8) if bits & (1 << b)]
            for i in ids:
                if i < len(fr.pkts) and rx - fr.last_send[i] > min_gap:
                    fr.last_send[i] = rx  # avoid duplicate scheduling before the pacer sends it
                    resend.append(OutPacket(fr.pkts[i], self, fid, i, retx=True))
        return resend

    # ---- stats -----------------------------------------------------------------------
    @property
    def lag(self) -> int:
        return self.next_fid - 1 - self.checkpoint

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        span = 2.0
        pk = self.w_packets.sum(now)
        return dict(
            fps=self.w_frames.sum(now) / span,
            bitrate=self.w_bytes.sum(now) * 8 / span,
            rtt_ms=self.w_rtt.median(now),
            send2ack_ms=self.w_send2ack.median(now),
            cap2ack_ms=self.w_cap2ack.median(now),
            cap2send_ms=self.w_cap2send.median(now),
            retx_ratio=(self.w_retx.sum(now) / pk) if pk else 0.0,
            fraction_lost=self.fraction_lost,
            playout_delay_ms=self.playout_delay_ms,
            lag=self.lag,
            **self.st,
        )


# --------------------------------------------------------------------------------------
# Pacing / routing
# --------------------------------------------------------------------------------------
class Pacer:
    """Token bucket. ``priority`` packets (retransmits, audio, SR) jump the queue."""

    def __init__(self, rate_bps: float = 80e6, burst_bytes: Optional[int] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.rate = rate_bps / 8.0
        self.burst = burst_bytes if burst_bytes is not None else 16 * MAX_PACKET_SIZE
        self.clock = clock
        self.tokens = float(self.burst)
        self._t = clock()
        self.hi: Deque[OutPacket] = collections.deque()
        self.lo: Deque[OutPacket] = collections.deque()

    def set_rate(self, rate_bps: float) -> None:
        self.rate = rate_bps / 8.0

    def push(self, pkts: list[OutPacket], priority: bool = False) -> None:
        (self.hi if priority else self.lo).extend(pkts)

    def clear(self) -> None:
        self.hi.clear()
        self.lo.clear()

    def __len__(self) -> int:
        return len(self.hi) + len(self.lo)

    def _refill(self, now: float) -> None:
        self.tokens = min(float(self.burst), self.tokens + (now - self._t) * self.rate)
        self._t = now

    def pop_ready(self, now: Optional[float] = None) -> list[OutPacket]:
        now = self.clock() if now is None else now
        self._refill(now)
        out = []
        while self.tokens > 0 and (self.hi or self.lo):
            p = (self.hi or self.lo).popleft()
            self.tokens -= len(p.data)
            out.append(p)
        return out

    def delay_until_next(self) -> Optional[float]:
        if not len(self):
            return None
        if self.tokens > 0:
            return 0.0
        return -self.tokens / self.rate


class RtcpRouter:
    def __init__(self) -> None:
        self.by_receiver_ssrc: dict[int, StreamSender] = {}

    def add(self, s: StreamSender) -> None:
        self.by_receiver_ssrc[s.receiver_ssrc] = s

    def route(self, datagram: bytes) -> Optional[tuple[StreamSender, RtcpResult]]:
        ssrc = receiver_ssrc_of(datagram)
        s = self.by_receiver_ssrc.get(ssrc) if ssrc is not None else None
        if s is None:
            return None
        return s, parse_receiver_rtcp(datagram)
