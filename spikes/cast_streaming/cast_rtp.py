"""Cast Streaming wire format (port z openscreen cast/streaming/impl).

- FrameCrypto: AES-128-CTR, nonce = (0^8 || BE32(frame_id) || 0^4) XOR aesIvMask  (frame_crypto.cc)
- RTP packetizer: 12B RTP + 7B Cast header (+4B adaptive latency ext)       (rtp_packetizer.cc)
- RTCP: Sender Report (sender_report_builder.cc), parser odpowiedzi odbiornika:
  RR + report block, PSFB(206)/15 Cast feedback (CAST + CST2), PSFB/1 PLI,
  XR(207) RRTR, APP(204)/2 receiver frame log                                (compound_rtcp_parser.cc)
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

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

NTP_EPOCH_OFFSET = 2208988800  # 1900 -> 1970

WIRE_EVENTS = {1: "AudioAckSent", 2: "AudioPlayoutDelay", 3: "AudioFrameDecoded", 4: "AudioPacketReceived",
               5: "VideoAckSent", 6: "VideoRenderDelay", 7: "VideoFrameDecoded", 8: "VideoPacketReceived",
               11: "AckSent", 12: "RenderDelay", 13: "FrameDecoded", 14: "PacketReceived"}


class FrameCrypto:
    def __init__(self, key: bytes, iv_mask: bytes):
        assert len(key) == 16 and len(iv_mask) == 16
        self.key = key
        self.iv_mask = iv_mask
        self._alg = algorithms.AES(key)

    def crypt(self, frame_id: int, data: bytes) -> bytes:
        nonce = bytearray(16)
        struct.pack_into(">I", nonce, 8, frame_id & 0xFFFFFFFF)
        nonce = bytes(a ^ b for a, b in zip(nonce, self.iv_mask))
        # AES_ctr128_encrypt inkrementuje cały 128-bitowy licznik big-endian == modes.CTR
        enc = Cipher(self._alg, modes.CTR(nonce)).encryptor()
        return enc.update(data) + enc.finalize()


def packetize(*, payload: bytes, frame_id: int, ref_frame_id: int, key_frame: bool, rtp_ts: int,
              ssrc: int, pt: int, seq_start: int, new_playout_delay_ms: int = 0) -> list[bytes]:
    """Zwraca listę pakietów RTP jednej (już zaszyfrowanej) ramki. seq numery: seq_start.."""
    n = max(1, -(-len(payload) // MAX_PAYLOAD))
    if n > 0xFFFE:
        raise ValueError("frame too large")
    out = []
    for pid in range(n):
        last = pid == n - 1
        chunk = payload[pid * MAX_PAYLOAD:(pid + 1) * MAX_PAYLOAD]
        ext = pid == 0 and new_playout_delay_ms > 0
        hdr = struct.pack(">BBHII", 0x80, (0x80 if last else 0) | (pt & 0x7F), (seq_start + pid) & 0xFFFF,
                          rtp_ts & 0xFFFFFFFF, ssrc)
        b12 = (0x80 if key_frame else 0) | 0x40 | (1 if ext else 0)
        hdr += struct.pack(">BBHHB", b12, frame_id & 0xFF, pid, n - 1, ref_frame_id & 0xFF)
        if ext:
            hdr += struct.pack(">HH", (1 << 10) | 2, new_playout_delay_ms)
        out.append(hdr + chunk)
    return out


def ntp_now(t: float | None = None) -> int:
    t = time.time() if t is None else t
    sec = int(t) + NTP_EPOCH_OFFSET
    frac = int((t - int(t)) * (1 << 32)) & 0xFFFFFFFF
    return (sec << 32) | frac


def build_sender_report(ssrc: int, ntp: int, rtp_ts: int, pkt_count: int, octet_count: int) -> bytes:
    payload = struct.pack(">IQIII", ssrc, ntp, rtp_ts & 0xFFFFFFFF, pkt_count & 0xFFFFFFFF,
                          octet_count & 0xFFFFFFFF)
    return struct.pack(">BBH", 0x80, RTCP_SR, len(payload) // 4) + payload


@dataclass
class RtcpResult:
    report_block: dict | None = None
    checkpoint8: int | None = None       # 8-bitowy checkpoint frame id
    playout_delay_ms: int | None = None
    nacks: list = field(default_factory=list)     # (frame8, packet_id, bitvec)
    acks8_rel: list = field(default_factory=list)  # offsety względem checkpoint (+2..)
    pli: bool = False
    rrtr_ntp: int | None = None
    logs: list = field(default_factory=list)       # (rtp_ts32, base_ms, [(type, delay_or_pid, dt_ms)])
    feedback_count: int | None = None
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
        if pt == RTCP_RR:
            if sub >= 1 and len(body) >= 4 + 24:
                (rssrc, to_ssrc, lost, ehsn, jitter, lsr, dlsr) = struct.unpack_from(">IIIIIII", body, 0)
                r.report_block = dict(receiver_ssrc=rssrc, to_ssrc=to_ssrc, fraction_lost=lost >> 24,
                                      cum_lost=lost & 0xFFFFFF, ehsn=ehsn, jitter=jitter, lsr=lsr, dlsr=dlsr)
        elif pt == RTCP_PSFB and sub == SUB_FEEDBACK:
            if len(body) < 16:
                continue
            rssrc, sssrc, ident = struct.unpack_from(">III", body, 0)
            if ident != CAST:
                continue
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
                continue
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
    return r


def expand8(lower8: int, ref: int, greater_than: bool = False) -> int:
    """Rozwija 8-bitowy frame id do pełnego, najbliżej `ref` (<= ref albo > ref)."""
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
