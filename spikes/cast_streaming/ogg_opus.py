"""Czytnik Ogg (ffmpeg -f ogg pipe:1) -> pojedyncze pakiety Opus (1 pakiet = 1 ramka Cast).

Granice pakietów z tablicy segmentów (lacing: 255 = pakiet trwa dalej). Pomija OpusHead/OpusTags.
ffmpeg trzeba odpalić z `-page_duration 20000 -flush_packets 1`, inaczej muxer trzyma strony ~1 s.
"""
from __future__ import annotations

import struct
from typing import BinaryIO, Iterator


def _read_exact(f: BinaryIO, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def iter_packets(f: BinaryIO) -> Iterator[bytes]:
    pending = b""
    while True:
        hdr = _read_exact(f, 27)
        if hdr[:4] != b"OggS":
            raise ValueError("lost Ogg sync")
        nseg = hdr[26]
        segs = _read_exact(f, nseg)
        body = _read_exact(f, sum(segs))
        p = 0
        for ln in segs:
            pending += body[p:p + ln]
            p += ln
            if ln < 255:
                pkt, pending = pending, b""
                if pkt.startswith(b"OpusHead") or pkt.startswith(b"OpusTags"):
                    continue
                yield pkt


def opus_samples(pkt: bytes, rate: int = 48000) -> int:
    """Liczba próbek w pakiecie Opus (RFC 6716 §3.1, TOC)."""
    toc = pkt[0]
    cfg = toc >> 3
    if cfg < 12:
        dur = [10, 20, 40, 60][cfg & 3] * rate // 1000
    elif cfg < 16:
        dur = [10, 20][cfg & 1] * rate // 1000
    else:
        dur = [25, 50, 100, 200][cfg & 3] * rate // 10000
    c = toc & 3
    n = 1 if c == 0 else 2 if c in (1, 2) else (pkt[1] & 0x3F)
    return dur * n
