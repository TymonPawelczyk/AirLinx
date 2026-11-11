"""Where the received media goes: files on disk and, optionally, a live ``ffplay`` window."""
from __future__ import annotations

import contextlib
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Optional


def _ogg_crc_table() -> list[int]:
    t = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else r << 1
            r &= 0xFFFFFFFF
        t.append(r)
    return t


_CRC = _ogg_crc_table()


def ogg_crc(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[((c >> 24) & 0xFF) ^ b]
    return c


def opus_packet_samples(pkt: bytes) -> int:
    """Samples at 48 kHz in one Opus packet (RFC 6716 section 3.1 TOC byte)."""
    if not pkt:
        return 0
    toc = pkt[0]
    cfg = toc >> 3
    if cfg < 12:
        dur = (480, 960, 1920, 2880)[cfg & 3]
    elif cfg < 16:
        dur = (480, 960)[cfg & 1]
    else:
        dur = (120, 240, 480, 960)[cfg & 3]
    code = toc & 3
    n = 1 if code == 0 else 2 if code in (1, 2) else (pkt[1] & 0x3F if len(pkt) > 1 else 1)
    return dur * n


class OggOpusWriter:
    """Minimal Ogg Opus muxer: one packet per page, granule position = 48 kHz sample count."""

    def __init__(self, path: Path, channels: int = 2) -> None:
        self.f = open(path, "wb")
        self.serial = 0x56545631
        self.seq = 0
        self.granule = 0
        self._page(b"OpusHead" + struct.pack("<BBHIhB", 1, channels, 312, 48000, 0, 0), 0, 0x02)
        self._page(b"OpusTags" + struct.pack("<I", 8) + b"AirLinxV" + struct.pack("<I", 0), 0, 0)

    def _page(self, packet: bytes, granule: int, flags: int) -> None:
        segs, n = [], len(packet)
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)
        hdr = (b"OggS" + struct.pack("<BBqIIIB", 0, flags, granule, self.serial, self.seq, 0, len(segs))
               + bytes(segs))
        page = bytearray(hdr + packet)
        struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
        self.f.write(page)
        self.seq += 1

    def write(self, packet: bytes) -> None:
        self.granule += opus_packet_samples(packet)
        self._page(packet, self.granule, 0)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.f.flush()
            self.f.close()


class FfplaySink:
    """Pipes an elementary stream into ``ffplay`` (low-latency flags). Failures never break the receiver."""

    def __init__(self, fmt: str, title: str, extra: Optional[list[str]] = None) -> None:
        self.proc: Optional[subprocess.Popen] = None
        exe = shutil.which("ffplay")
        if exe is None:
            return
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-window_title", title, "-fflags", "nobuffer",
               "-flags", "low_delay", "-framedrop", "-probesize", "32768", "-analyzeduration", "0",
               "-f", fmt, *(extra or []), "-i", "pipe:0"]
        with contextlib.suppress(OSError):
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=os.environ.copy())

    def write(self, data: bytes) -> None:
        p = self.proc
        if p is None or p.stdin is None:
            return
        if p.poll() is not None:
            self.proc = None
            return
        try:
            p.stdin.write(data)
            p.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.proc = None

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        with contextlib.suppress(Exception):
            if p.stdin:
                p.stdin.close()
        with contextlib.suppress(Exception):
            p.terminate()
            p.wait(timeout=2)
        if p.poll() is None:
            with contextlib.suppress(Exception):
                p.kill()


class StreamRecorder:
    """Mirroring output: ``video.h264`` (Annex-B), ``audio.opus`` (Ogg) and an optional live window."""

    def __init__(self, out_dir: Optional[Path], show: bool = False) -> None:
        self.out_dir = out_dir
        self.out_dir_ = out_dir
        self.video_codec = "h264"
        self.video_path = out_dir / "video.h264" if out_dir else None
        self.audio_path = out_dir / "audio.opus" if out_dir else None
        self._vf = None
        self._audio: Optional[OggOpusWriter] = None
        self._show = show
        self._player: Optional[FfplaySink] = None
        self.video_bytes = 0
        self.audio_packets = 0
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    def set_video_codec(self, codec: str) -> None:
        """"h264" -> video.h264, "hevc" -> video.h265 (Annex-B elementary stream, demuxer ``-f h264`` / ``-f hevc``)."""
        self.video_codec = codec
        if self.out_dir_:
            self.video_path = self.out_dir_ / ("video.h265" if codec == "hevc" else "video.h264")

    def video(self, annexb: bytes) -> None:
        self.video_bytes += len(annexb)
        if self.video_path:
            if self._vf is None:
                self._vf = open(self.video_path, "ab")
            self._vf.write(annexb)
        if self._show:
            if self._player is None:
                self._player = FfplaySink("hevc" if self.video_codec == "hevc" else "h264", "Wirtualny TV (AirLinx)",
                                          ["-framerate", "60"])
            self._player.write(annexb)

    def audio(self, packet: bytes) -> None:
        self.audio_packets += 1
        if self.audio_path:
            if self._audio is None:
                self._audio = OggOpusWriter(self.audio_path)
            self._audio.write(packet)

    def flush(self) -> None:
        for f in (self._vf,):
            if f:
                with contextlib.suppress(Exception):
                    f.flush()
        if self._audio:
            with contextlib.suppress(Exception):
                self._audio.f.flush()

    def close(self) -> None:
        if self._vf:
            with contextlib.suppress(Exception):
                self._vf.close()
            self._vf = None
        if self._audio:
            self._audio.close()
            self._audio = None
        if self._player:
            self._player.close()
            self._player = None


__all__ = ["StreamRecorder", "OggOpusWriter", "FfplaySink", "opus_packet_samples", "ogg_crc"]
