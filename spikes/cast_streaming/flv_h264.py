"""Czytnik strumienia FLV (z ffmpeg -f flv pipe:1) -> ramki H.264 Annex-B.

FLV daje dokładne granice access-unitów (1 tag = 1 ramka) bez czekania na
następny AUD, flagę keyframe i znacznik czasu w ms. Z `-copyts` i x11grab
timestamp = CLOCK_MONOTONIC chwili przechwycenia (mod 2^32 ms), czyli to samo
co time.monotonic() w Pythonie -> mierzymy opóźnienie capture->...
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

START = b"\x00\x00\x00\x01"


@dataclass
class H264Frame:
    data: bytes          # Annex-B (SPS/PPS doklejone przed IDR)
    key: bool
    ts_ms: int           # FLV timestamp (32 bit)


def _read_exact(f: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def iter_frames(f: BinaryIO) -> Iterator[H264Frame]:
    hdr = _read_exact(f, 9)
    if hdr[:3] != b"FLV":
        raise ValueError("not FLV")
    _read_exact(f, hdr[8] - 9 + 4)  # reszta nagłówka + PreviousTagSize0
    sps_pps = b""
    nal_len_size = 4
    while True:
        th = _read_exact(f, 11)
        ttype = th[0] & 0x1F
        size = int.from_bytes(th[1:4], "big")
        ts = int.from_bytes(th[4:7], "big") | (th[7] << 24)
        body = _read_exact(f, size)
        _read_exact(f, 4)  # PreviousTagSize
        if ttype != 9 or size < 5:
            continue
        frame_type = body[0] >> 4
        codec = body[0] & 0x0F
        if codec != 7:
            continue
        pkt_type = body[1]
        payload = body[5:]
        if pkt_type == 0:  # AVCDecoderConfigurationRecord
            nal_len_size = (payload[4] & 0x03) + 1
            p = 5
            nsps = payload[p] & 0x1F
            p += 1
            out = b""
            for _ in range(nsps):
                ln = struct.unpack_from(">H", payload, p)[0]
                out += START + payload[p + 2:p + 2 + ln]
                p += 2 + ln
            npps = payload[p]
            p += 1
            for _ in range(npps):
                ln = struct.unpack_from(">H", payload, p)[0]
                out += START + payload[p + 2:p + 2 + ln]
                p += 2 + ln
            sps_pps = out
            continue
        if pkt_type != 1:
            continue
        nals = []
        p = 0
        has_sps = False
        while p + nal_len_size <= len(payload):
            ln = int.from_bytes(payload[p:p + nal_len_size], "big")
            nal = payload[p + nal_len_size:p + nal_len_size + ln]
            p += nal_len_size + ln
            if nal and (nal[0] & 0x1F) == 7:
                has_sps = True
            nals.append(START + nal)
        key = frame_type == 1
        data = b"".join(nals)
        if key and not has_sps:
            data = sps_pps + data
        yield H264Frame(data=data, key=key, ts_ms=ts)
