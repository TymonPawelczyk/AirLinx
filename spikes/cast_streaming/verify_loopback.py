#!/usr/bin/env python3
"""Test samospójności (bez TV): ffmpeg -> szyfrowanie -> pakiety Cast RTP -> (przetasowanie)
-> parser wg openscreen rtp_packet_parser -> deszyfrowanie -> plik .h264 -> ffmpeg dekoduje.

Nie dowodzi interoperacyjności z TV, ale wyklucza błędy w nagłówkach/fragmentacji/CTR.
"""
import os
import random
import struct
import subprocess
import sys

import cast_rtp as cr
import flv_h264

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/loopback.h264"


def parse(pkt: bytes):
    # rtp_packet_parser.cc: bajt0 == 0x80, PT, seq, ts, ssrc, potem nagłówek Cast
    assert pkt[0] == 0x80
    m_pt, seq, ts, ssrc = struct.unpack_from(">BHII", pkt, 1)
    b12, fid8, pid, maxpid, rfid8 = struct.unpack_from(">BBHHB", pkt, 12)
    p = 19
    delay = None
    for _ in range(b12 & 0x3F):
        tl, = struct.unpack_from(">H", pkt, p)
        typ, ln = tl >> 10, tl & 0x3FF
        if typ == 1:
            delay, = struct.unpack_from(">H", pkt, p + 2)
        p += 2 + ln
    return dict(key=bool(b12 & 0x80), fid8=fid8, pid=pid, maxpid=maxpid, rfid8=rfid8, ts=ts,
                pt=m_pt & 0x7F, marker=bool(m_pt & 0x80), payload=pkt[p:], delay=delay)


def main():
    key, iv = os.urandom(16), os.urandom(16)
    crypto = cr.FrameCrypto(key, iv)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "x11grab",
                           "-framerate", "30", "-video_size", "1920x1080", "-i", ":1+0,420", "-frames:v", "60",
                           "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-bf", "0", "-g", "30",
                           "-profile:v", "main", "-f", "flv", "pipe:1"], stdout=subprocess.PIPE,
                          env=dict(os.environ, DISPLAY=":1"))
    wire = []
    seq = 1234
    fid = 0
    try:
        for fr in flv_h264.iter_frames(ff.stdout):
            enc = crypto.crypt(fid, fr.data)
            pk = cr.packetize(payload=enc, frame_id=fid, ref_frame_id=fid if fr.key else fid - 1,
                              key_frame=fr.key, rtp_ts=fid * 3000, ssrc=1, pt=96, seq_start=seq,
                              new_playout_delay_ms=100 if fid == 0 else 0)
            seq += len(pk)
            fid += 1
            wire.append(pk)
    except EOFError:
        pass
    # "odbiornik": tasujemy pakiety w obrębie ramki, składamy wg pid, rozwijamy fid8
    out = open(OUT, "wb")
    last_full = -1
    for pk in wire:
        pk = pk[:]
        random.shuffle(pk)
        parts = [parse(p) for p in pk]
        full = cr.expand8(parts[0]["fid8"], last_full, greater_than=True)
        n = parts[0]["maxpid"] + 1
        assert len(parts) == n and all(p["marker"] == (p["pid"] == n - 1) for p in parts)
        data = b"".join(p["payload"] for p in sorted(parts, key=lambda x: x["pid"]))
        out.write(crypto.crypt(full, data))
        last_full = full
    out.close()
    print(f"frames={len(wire)} packets={sum(map(len, wire))} -> {OUT}")
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                        "stream=codec_name,profile,width,height,nb_read_frames", "-of", "compact", OUT],
                       capture_output=True, text=True)
    print(r.stdout.strip(), r.stderr.strip()[:500])


if __name__ == "__main__":
    main()
