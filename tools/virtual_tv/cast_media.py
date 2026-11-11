"""Receiver side of Cast Streaming media: RTP/RTCP, AES-CTR, frame assembly, feedback.

Written from spikes/cast_streaming/PROTOCOL.md and openscreen's receiver, **independently of
``airlinx.core.cast.rtp``** (no shared code), so a mistake in the sender cannot be mirrored here.
Only ``cryptography`` is used for AES.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .sinks import StreamRecorder

log = logging.getLogger("vtv.cast.media")

NTP_EPOCH = 2208988800
CAST, CST2 = 0x43415354, 0x43535432
VIDEO_PT, AUDIO_PT = 96, 127


def aes_ctr(key: bytes, iv_mask: bytes, frame_id: int, data: bytes) -> bytes:
    """nonce = (0^8 || BE32(frame_id) || 0^4) XOR iv_mask, counter incremented as 128-bit big endian."""
    nonce = ((frame_id & 0xFFFFFFFF) << 32) ^ int.from_bytes(iv_mask, "big")
    c = Cipher(algorithms.AES(key), modes.CTR(nonce.to_bytes(16, "big"))).decryptor()
    return c.update(data) + c.finalize()


def ntp_to_unix(ntp: int) -> float:
    return (ntp >> 32) - NTP_EPOCH + (ntp & 0xFFFFFFFF) / (1 << 32)


def unix_to_ntp(t: float) -> int:
    sec = int(t)
    return ((sec + NTP_EPOCH) << 32) | (int((t - sec) * (1 << 32)) & 0xFFFFFFFF)


def expand8(lower: int, ref: int) -> int:
    """Full frame id closest to ``ref`` whose low byte is ``lower`` (ids only grow, so prefer >= ref - 128)."""
    cand = (ref & ~0xFF) | lower
    while cand < ref - 128:
        cand += 256
    while cand > ref + 127:
        cand -= 256
    return max(cand, 0)


@dataclass
class RtpPacket:
    pt: int
    marker: bool
    seq: int
    ts: int
    ssrc: int
    key: bool
    fid8: int
    pid: int
    maxpid: int
    ref8: int
    playout_delay_ms: Optional[int]
    payload: bytes


def parse_rtp(pkt: bytes) -> RtpPacket:
    """Cast RTP header (openscreen rtp_packet_parser.cc). Raises ValueError on anything malformed."""
    if len(pkt) < 19 or pkt[0] != 0x80:
        raise ValueError("not a Cast RTP packet")
    m_pt, seq, ts, ssrc = struct.unpack_from(">BHII", pkt, 1)
    b12, fid8, pid, maxpid, ref8 = struct.unpack_from(">BBHHB", pkt, 12)
    p = 19
    delay = None
    for _ in range(b12 & 0x3F):
        if p + 2 > len(pkt):
            raise ValueError("truncated extension")
        tl, = struct.unpack_from(">H", pkt, p)
        typ, ln = tl >> 10, tl & 0x3FF
        if typ == 1 and ln >= 2:
            delay, = struct.unpack_from(">H", pkt, p + 2)
        p += 2 + ln
    if p > len(pkt) or pid > maxpid:
        raise ValueError("bad packet id")
    return RtpPacket(pt=m_pt & 0x7F, marker=bool(m_pt & 0x80), seq=seq, ts=ts, ssrc=ssrc, key=bool(b12 & 0x80),
                     fid8=fid8, pid=pid, maxpid=maxpid, ref8=ref8, playout_delay_ms=delay, payload=pkt[p:])


@dataclass
class StreamSpec:
    """One negotiated stream (from the OFFER)."""
    index: int
    kind: str                 # "video" | "audio"
    ssrc: int                 # sender
    rssrc: int                # ours (reported in ANSWER)
    key: bytes
    iv_mask: bytes
    pt: int
    timebase: int
    target_delay_ms: int
    codec: str = "h264"        # video: "h264" | "hevc"


@dataclass
class FrameRecord:
    """One fully received frame, for tests and latency analysis."""
    kind: str
    fid: int
    rtp_ts: int
    size: int
    key: bool
    packets: int
    t_first: float            # wall clock, first packet of the frame
    t_done: float             # wall clock, last missing packet arrived
    capture_wall: Optional[float] = None   # sender capture instant (SR mapping), wall clock


@dataclass
class _Partial:
    fid: int
    rtp_ts: int
    key: bool
    maxpid: int
    t_first: float
    parts: dict[int, bytes] = field(default_factory=dict)


class StreamRx:
    """Reassembly + feedback state of one stream."""

    def __init__(self, spec: StreamSpec) -> None:
        self.spec = spec
        self.partials: dict[int, _Partial] = {}
        self.done: dict[int, tuple[bytes, FrameRecord]] = {}   # complete, waiting for delivery order
        self.complete_ids: set[int] = set()
        self.checkpoint = -1            # highest id such that all ids <= it are complete (or given up on)
        self.next_deliver = 0
        self.highest = -1
        self.sr: Optional[tuple[int, int, float]] = None   # (ntp, rtp, local arrival wall)
        self.sr_count = 0
        self.feedback_count = 0
        self.packets = 0
        self.octets = 0
        self.dropped_before_sr = 0
        self.lost_given_up = 0
        self.dup_packets = 0
        self.max_seq = -1
        self.records: list[FrameRecord] = []
        self.last_rtp_arrival = 0.0
        self.jitter = 0.0

    def capture_wall(self, rtp_ts: int) -> Optional[float]:
        """Wall-clock instant the sender stamped on ``rtp_ts`` (from the last Sender Report)."""
        if self.sr is None:
            return None
        ntp, sr_rtp, _ = self.sr
        delta = (rtp_ts - sr_rtp + (1 << 31)) % (1 << 32) - (1 << 31)    # signed 32-bit distance
        return ntp_to_unix(ntp) + delta / self.spec.timebase

    def missing(self) -> list[tuple[int, int]]:
        """(frame id, packet id) pairs still missing for frames older than the newest packet seen."""
        out = []
        for fid, p in sorted(self.partials.items()):
            for pid in range(p.maxpid + 1):
                if pid not in p.parts:
                    out.append((fid, pid))
        return out


class MediaPlane:
    """UDP endpoint that receives Cast RTP for the negotiated streams and answers with RTCP feedback."""

    def __init__(self, streams: list[StreamSpec], recorder: StreamRecorder, *, host: str = "127.0.0.1",
                 drop: Optional[Callable[[RtpPacket, int], bool]] = None, on_frame: Optional[Callable] = None,
                 give_up_s: float = 0.6, require_sr: bool = True) -> None:
        self.host = host
        self.rx = {s.ssrc: StreamRx(s) for s in streams}
        self.recorder = recorder
        self.drop = drop                       # fault injection: drop(packet, n_seen) -> True = lose it
        self.on_frame = on_frame
        self.give_up_s = give_up_s
        self.require_sr = require_sr
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.port = 0
        self.peer: Optional[tuple[str, int]] = None
        self.seen = 0
        self.dropped = 0
        self.rtcp_sent = 0
        self.malformed = 0
        self.unknown_ssrc = 0
        self.first_packet_wall: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self.closed = False

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        plane = self

        class P(asyncio.DatagramProtocol):
            def connection_made(self, t):  # noqa: ANN001
                plane.transport = t

            def datagram_received(self, data, addr):  # noqa: ANN001
                plane._on_datagram(data, addr)

        await loop.create_datagram_endpoint(P, local_addr=(self.host, 0))
        assert self.transport is not None
        self.port = self.transport.get_extra_info("sockname")[1]
        sock = self.transport.get_extra_info("socket")
        if sock is not None:
            import socket as _s
            sock.setsockopt(_s.SOL_SOCKET, _s.SO_RCVBUF, 4 << 20)
        self._task = asyncio.create_task(self._housekeeping(), name="vtv-media-housekeeping")
        return self.port

    def close(self) -> None:
        self.closed = True
        if self._task:
            self._task.cancel()
        if self.transport:
            self.transport.close()

    # ---- datagrams ---------------------------------------------------------------------
    def _on_datagram(self, data: bytes, addr) -> None:  # noqa: ANN001
        if self.closed or len(data) < 8:
            return
        self.peer = addr
        if data[0] >> 6 == 2 and 200 <= data[1] <= 207:
            self._on_rtcp(data)
            return
        now = time.time()
        try:
            pkt = parse_rtp(data)
        except ValueError:
            self.malformed += 1
            return
        rx = self.rx.get(pkt.ssrc)
        if rx is None:
            self.unknown_ssrc += 1
            return
        self.seen += 1
        if self.drop and self.drop(pkt, self.seen):
            self.dropped += 1
            return
        if self.first_packet_wall is None:
            self.first_packet_wall = now
        if self.require_sr and rx.sr is None:
            rx.dropped_before_sr += 1          # like the real receiver: no Sender Report yet, packet is useless
            return
        self._on_rtp(rx, pkt, now, len(data))

    def _on_rtcp(self, data: bytes) -> None:
        off = 0
        while off + 8 <= len(data):
            b0, pt, ln = struct.unpack_from(">BBH", data, off)
            body = data[off + 4: off + 4 + ln * 4]
            off += 4 + ln * 4
            if pt == 200 and len(body) >= 20:
                ssrc, ntp, rtp, _pc, _oc = struct.unpack_from(">IQIII", body, 0)
                rx = self.rx.get(ssrc)
                if rx is not None:
                    rx.sr = (ntp, rtp, time.time())
                    rx.sr_count += 1

    def _on_rtp(self, rx: StreamRx, pkt: RtpPacket, now: float, size: int) -> None:
        rx.packets += 1
        rx.octets += size
        rx.max_seq = max(rx.max_seq, pkt.seq)
        if rx.last_rtp_arrival:
            rx.jitter += (abs((now - rx.last_rtp_arrival) - 0.0) - rx.jitter) / 16
        rx.last_rtp_arrival = now
        ref = max(rx.highest, rx.checkpoint, 0)
        fid = expand8(pkt.fid8, ref)
        if fid < rx.next_deliver or fid in rx.complete_ids:
            rx.dup_packets += 1                  # retransmission of something we already have
            self._feedback(rx)
            return
        rx.highest = max(rx.highest, fid)
        p = rx.partials.get(fid)
        if p is None:
            p = rx.partials[fid] = _Partial(fid, pkt.ts, pkt.key, pkt.maxpid, now)
        p.parts[pkt.pid] = pkt.payload
        if len(p.parts) == p.maxpid + 1:
            self._complete(rx, p, now)
            self._feedback(rx)
        elif pkt.marker or pkt.pid == p.maxpid:
            self._feedback(rx)                   # tail seen with gaps: tell the sender what is missing

    def _complete(self, rx: StreamRx, p: _Partial, now: float) -> None:
        enc = b"".join(p.parts[i] for i in range(p.maxpid + 1))
        data = aes_ctr(rx.spec.key, rx.spec.iv_mask, p.fid, enc)
        rec = FrameRecord(rx.spec.kind, p.fid, p.rtp_ts, len(data), p.key, p.maxpid + 1, p.t_first, now,
                          rx.capture_wall(p.rtp_ts))
        del rx.partials[p.fid]
        rx.complete_ids.add(p.fid)
        rx.done[p.fid] = (data, rec)
        self._deliver(rx)

    def _deliver(self, rx: StreamRx) -> None:
        while rx.next_deliver in rx.done:
            data, rec = rx.done.pop(rx.next_deliver)
            rx.next_deliver += 1
            rx.records.append(rec)
            if rx.spec.kind == "video":
                self.recorder.video(data)
            else:
                self.recorder.audio(data)
            if self.on_frame:
                try:
                    self.on_frame(rec, data)
                except Exception:  # noqa: BLE001
                    log.exception("on_frame failed")
        rx.checkpoint = rx.next_deliver - 1
        for fid in [f for f in rx.complete_ids if f <= rx.checkpoint - 300]:
            rx.complete_ids.discard(fid)

    async def _housekeeping(self) -> None:
        """Give up on frames that never complete (the sender gave up too); re-send feedback for NACKs."""
        while True:
            await asyncio.sleep(0.05)
            now = time.time()
            for rx in self.rx.values():
                stale = [f for f, p in rx.partials.items() if now - p.t_first > self.give_up_s]
                for f in stale:
                    del rx.partials[f]
                    rx.lost_given_up += 1
                    rx.complete_ids.add(f)        # counts as "handled": do not stall the checkpoint
                    rx.done[f] = (b"", FrameRecord(rx.spec.kind, f, 0, 0, False, 0, now, now))
                if stale:
                    self._skip_empty(rx)
                if rx.partials and now - min(p.t_first for p in rx.partials.values()) > 0.02:
                    self._feedback(rx)            # still missing packets: NACK again

    def _skip_empty(self, rx: StreamRx) -> None:
        while rx.next_deliver in rx.done:
            data, rec = rx.done.pop(rx.next_deliver)
            rx.next_deliver += 1
            if data:
                rx.records.append(rec)
                (self.recorder.video if rx.spec.kind == "video" else self.recorder.audio)(data)
        rx.checkpoint = rx.next_deliver - 1

    # ---- RTCP out ------------------------------------------------------------------------
    def _feedback(self, rx: StreamRx) -> None:
        if self.transport is None or self.peer is None or self.closed:
            return
        spec = rx.spec
        now = time.time()
        ck = rx.next_deliver - 1
        # ACK vector: frames complete beyond ck+1; bit0 = ck + 2
        ack_bits = bytearray()
        beyond = sorted(f for f in rx.done if f > ck + 1)
        if beyond:
            span = beyond[-1] - (ck + 2) + 1
            ack_bits = bytearray((span + 7) // 8)
            for f in beyond:
                i = f - (ck + 2)
                ack_bits[i // 8] |= 1 << (i % 8)
        loss = []
        for fid, p in sorted(rx.partials.items()):
            miss = [i for i in range(p.maxpid + 1) if i not in p.parts]
            if len(p.parts) == 0 or not miss:
                continue
            if miss == list(range(p.maxpid + 1)):
                loss.append((fid & 0xFF, 0xFFFF, 0))
                continue
            while miss:
                first = miss.pop(0)
                bits = 0
                for m in list(miss):
                    if 0 < m - first <= 8:
                        bits |= 1 << (m - first - 1)
                        miss.remove(m)
                loss.append((fid & 0xFF, first, bits))
        loss = loss[:20]
        sr = rx.sr
        lsr = dlsr = 0
        if sr is not None:
            lsr = (sr[0] >> 16) & 0xFFFFFFFF
            dlsr = int((now - sr[2]) * 65536) & 0xFFFFFFFF
        # --- RR with one report block
        block = struct.pack(">IIIIII", spec.ssrc, 0, max(rx.max_seq, 0) & 0xFFFFFFFF, int(rx.jitter * spec.timebase),
                            lsr, dlsr)
        rr_body = struct.pack(">I", spec.rssrc) + block
        rr = struct.pack(">BBH", 0x81, 201, len(rr_body) // 4) + rr_body
        # --- XR RRTR
        xr_body = struct.pack(">I", spec.rssrc) + struct.pack(">BBHQ", 4, 0, 2, unix_to_ntp(now))
        xr = struct.pack(">BBH", 0x80, 207, len(xr_body) // 4) + xr_body
        # --- PSFB FMT=15 Cast feedback
        fb = struct.pack(">III", spec.rssrc, spec.ssrc, CAST)
        fb += struct.pack(">BBH", ck & 0xFF, len(loss), spec.target_delay_ms & 0xFFFF)
        for f8, pid, bits in loss:
            fb += struct.pack(">BHB", f8, pid, bits)
        rx.feedback_count = (rx.feedback_count + 1) & 0xFF
        fb += struct.pack(">IBB", CST2, rx.feedback_count, len(ack_bits)) + bytes(ack_bits)
        while len(fb) % 4:
            fb += b"\x00"
        psfb = struct.pack(">BBH", 0x80 | 15, 206, len(fb) // 4) + fb
        self.transport.sendto(rr + xr + psfb, self.peer)
        self.rtcp_sent += 1

    def send_pli(self, kind: str = "video") -> None:
        """Ask the sender for a keyframe (what a real TV does after unrecoverable loss)."""
        for rx in self.rx.values():
            if rx.spec.kind == kind and self.transport and self.peer:
                self.transport.sendto(struct.pack(">BBHII", 0x81, 206, 2, rx.spec.rssrc, rx.spec.ssrc), self.peer)

    # ---- reporting ---------------------------------------------------------------------
    def stats(self) -> dict:
        out: dict = {"packets_seen": self.seen, "dropped_by_fault": self.dropped, "rtcp_sent": self.rtcp_sent,
                     "malformed": self.malformed, "unknown_ssrc": self.unknown_ssrc}
        for rx in self.rx.values():
            k = rx.spec.kind
            out[k] = dict(frames=len(rx.records), keyframes=sum(1 for r in rx.records if r.key),
                          bytes=sum(r.size for r in rx.records), sr=rx.sr_count, packets=rx.packets,
                          checkpoint=rx.checkpoint, lost_given_up=rx.lost_given_up, retx_dups=rx.dup_packets,
                          dropped_before_sr=rx.dropped_before_sr)
        return out

    def records(self, kind: str) -> list[FrameRecord]:
        return [r for rx in self.rx.values() if rx.spec.kind == kind for r in rx.records]
