"""Testy offline (bez TV). Uruchom: .venv/bin/python -m pytest -q test_offline.py  (albo python test_offline.py)"""
import os
import struct
import subprocess
import tempfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import cast_rtp as cr
import ogg_opus

# Prawdziwy compound RTCP z TV (logs/run2.log, RTCP#1): RR+report block, XR RRTR, Cast feedback
TV_RTCP = bytes.fromhex(
    "81c900072ffb24842ffb2483000000000000000000000000be1f4d870000000080cf00042ffb248404000002"
    "ee9cbe1e5f62e9038fce00062ffb24842ffb248343415354ff0000644353543200020000")


def test_crypto_matches_manual_ctr():
    key, iv = os.urandom(16), os.urandom(16)
    data = os.urandom(100)
    fid = 0x01020304
    # niezależna implementacja: keystream = AES-ECB(counter), counter = nonce + i (128-bit BE)
    nonce = int.from_bytes(bytes(8) + fid.to_bytes(4, "big") + bytes(4), "big") ^ int.from_bytes(iv, "big")
    ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ks = b"".join(ecb.update(((nonce + i) % (1 << 128)).to_bytes(16, "big")) for i in range(7))
    expect = bytes(a ^ b for a, b in zip(data, ks))
    assert cr.FrameCrypto(key, iv).crypt(fid, data) == expect


def test_packetizer_headers():
    pk = cr.packetize(payload=b"x" * 3000, frame_id=300, ref_frame_id=299, key_frame=False, rtp_ts=0xABCDEF01,
                      ssrc=7, pt=96, seq_start=0xFFFF, new_playout_delay_ms=100)
    assert len(pk) == 3
    b0, b1, seq, ts, ssrc = struct.unpack_from(">BBHII", pk[0])
    assert (b0, b1, seq, ts, ssrc) == (0x80, 96, 0xFFFF, 0xABCDEF01, 7)
    assert struct.unpack_from(">BBHHB", pk[0], 12) == (0x41, 300 & 0xFF, 0, 2, 299 & 0xFF)
    assert struct.unpack_from(">HH", pk[0], 19) == ((1 << 10) | 2, 100)
    assert pk[-1][1] == 0x80 | 96 and struct.unpack_from(">H", pk[1], 2)[0] == 0  # marker, seq wrap
    assert all(len(p) <= cr.MAX_PACKET_SIZE for p in pk)
    # audio: keyframe, ref = self
    a = cr.packetize(payload=b"o" * 50, frame_id=5, ref_frame_id=5, key_frame=True, rtp_ts=480, ssrc=9, pt=127,
                     seq_start=1)
    assert len(a) == 1 and a[0][1] == 0x80 | 127 and a[0][12] == 0xC0


def test_parse_real_tv_rtcp():
    r = cr.parse_receiver_rtcp(TV_RTCP)
    assert r.report_block["receiver_ssrc"] == 0x2FFB2484 and r.report_block["to_ssrc"] == 0x2FFB2483
    assert r.checkpoint8 == 0xFF and r.playout_delay_ms == 100 and r.nacks == [] and r.rrtr_ntp
    assert cr.expand8(r.checkpoint8, 0) == -1  # "przed pierwszą ramką"


def test_parse_nack_and_acks():
    body = struct.pack(">III", 2, 1, cr.CAST) + struct.pack(">BBH", 10, 2, 50)
    body += struct.pack(">BHB", 12, 3, 0b101) + struct.pack(">BHB", 13, 0xFFFF, 0)
    body += struct.pack(">IBB", cr.CST2, 1, 2) + bytes([0b11, 0])
    pkt = struct.pack(">BBH", 0x80 | 15, 206, len(body) // 4) + body
    r = cr.parse_receiver_rtcp(pkt)
    assert r.checkpoint8 == 10 and r.nacks == [(12, 3, 5), (13, 0xFFFF, 0)] and r.acks8_rel == [2, 3]
    assert cr.expand8(12, 10, greater_than=True) == 12 and cr.expand8(2, 250, greater_than=True) == 258


def test_sender_report():
    sr = cr.build_sender_report(5, 0x1122334455667788, 99, 3, 4)
    assert sr[:4] == bytes([0x80, 200, 0, 6]) and len(sr) == 28


def test_opus_ogg_roundtrip(tmp=None):
    tmp = tmp or os.path.join(tempfile.mkdtemp(prefix="airlinx-spike-"), "opus_test.ogg")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", "-ac", "2",
                    "-c:a", "libopus", "-frame_duration", "10", "-page_duration", "10000", tmp], check=True)
    pkts = []
    try:
        for p in ogg_opus.iter_packets(open(tmp, "rb")):
            pkts.append(p)
    except EOFError:
        pass
    assert 95 <= len(pkts) <= 105 and all(ogg_opus.opus_samples(p) == 480 for p in pkts)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
