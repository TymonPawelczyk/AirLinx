"""Offline tests for the Cast Streaming media plane (airlinx.core.cast.rtp)."""
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from airlinx.core.cast import rtp as cr

# Real compound RTCP from the TV (spike logs/run2.log): RR + report block, XR RRTR, Cast feedback
TV_RTCP = bytes.fromhex(
    "81c900072ffb24842ffb2483000000000000000000000000be1f4d870000000080cf00042ffb248404000002"
    "ee9cbe1e5f62e9038fce00062ffb24842ffb248343415354ff0000644353543200020000")


def feedback(rssrc: int, sssrc: int, ckpt8: int, nacks=(), acks_bits=b"", delay=100) -> bytes:
    body = struct.pack(">III", rssrc, sssrc, cr.CAST) + struct.pack(">BBH", ckpt8, len(nacks), delay)
    for f8, pid, bits in nacks:
        body += struct.pack(">BHB", f8, pid, bits)
    body += struct.pack(">IBB", cr.CST2, 1, len(acks_bits)) + acks_bits
    body += b"\x00" * (-len(body) % 4)
    return struct.pack(">BBH", 0x80 | 15, 206, len(body) // 4) + body


def rr(rssrc: int, to_ssrc: int, lsr: int, dlsr: int, fraction_lost: int = 0) -> bytes:
    body = struct.pack(">IIIIIII", rssrc, to_ssrc, fraction_lost << 24, 0, 0, lsr, dlsr)
    return struct.pack(">BBH", 0x81, 201, len(body) // 4) + body


def test_crypto_matches_manual_ctr():
    key, iv = os.urandom(16), os.urandom(16)
    data = os.urandom(100)
    fid = 0x01020304
    nonce = int.from_bytes(bytes(8) + fid.to_bytes(4, "big") + bytes(4), "big") ^ int.from_bytes(iv, "big")
    ecb = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ks = b"".join(ecb.update(((nonce + i) % (1 << 128)).to_bytes(16, "big")) for i in range(7))
    expect = bytes(a ^ b for a, b in zip(data, ks))
    c = cr.FrameCrypto(key, iv)
    assert c.crypt(fid, data) == expect
    assert c.crypt(fid, c.crypt(fid, data)) == data


def test_packetizer_headers():
    pk = cr.packetize(payload=b"x" * 3000, frame_id=300, ref_frame_id=299, key_frame=False, rtp_ts=0xABCDEF01,
                      ssrc=7, pt=96, seq_start=0xFFFF, new_playout_delay_ms=100)
    assert len(pk) == 3
    b0, b1, seq, ts, ssrc = struct.unpack_from(">BBHII", pk[0])
    assert (b0, b1, seq, ts, ssrc) == (0x80, 96, 0xFFFF, 0xABCDEF01, 7)
    assert struct.unpack_from(">BBHHB", pk[0], 12) == (0x41, 300 & 0xFF, 0, 2, 299 & 0xFF)
    assert struct.unpack_from(">HH", pk[0], 19) == ((1 << 10) | 2, 100)
    assert pk[1][12] == 0x40  # extension only in packet 0
    assert pk[-1][1] == 0x80 | 96 and struct.unpack_from(">H", pk[1], 2)[0] == 0  # marker, seq wrap
    assert all(len(p) <= cr.MAX_PACKET_SIZE for p in pk)
    assert b"".join(p[cr.BASE_HDR + (4 if i == 0 else 0):] for i, p in enumerate(pk)) == b"x" * 3000
    a = cr.packetize(payload=b"o" * 50, frame_id=5, ref_frame_id=5, key_frame=True, rtp_ts=480, ssrc=9, pt=127,
                     seq_start=1)
    assert len(a) == 1 and a[0][1] == 0x80 | 127 and a[0][12] == 0xC0


def test_parse_real_tv_rtcp():
    r = cr.parse_receiver_rtcp(TV_RTCP)
    assert r.report_block["receiver_ssrc"] == 0x2FFB2484 and r.report_block["to_ssrc"] == 0x2FFB2483
    assert r.checkpoint8 == 0xFF and r.playout_delay_ms == 100 and r.nacks == [] and r.rrtr_ntp
    assert cr.expand8(r.checkpoint8, 0) == -1
    assert cr.receiver_ssrc_of(TV_RTCP) == 0x2FFB2484


def test_parse_nack_and_acks():
    pkt = feedback(2, 1, 10, nacks=[(12, 3, 0b101), (13, 0xFFFF, 0)], acks_bits=bytes([0b11, 0]), delay=50)
    r = cr.parse_receiver_rtcp(pkt)
    assert r.checkpoint8 == 10 and r.nacks == [(12, 3, 5), (13, 0xFFFF, 0)] and r.acks8_rel == [2, 3]
    assert r.playout_delay_ms == 50
    assert cr.expand8(12, 10, greater_than=True) == 12 and cr.expand8(2, 250, greater_than=True) == 258


def test_parse_pli_and_truncated():
    pli = struct.pack(">BBHII", 0x81, 206, 2, 2, 1)
    assert cr.parse_receiver_rtcp(pli).pli
    bad = feedback(2, 1, 10)[:10]
    cr.parse_receiver_rtcp(bad)  # must not raise


def test_sender_report():
    sr = cr.build_sender_report(5, 0x1122334455667788, 99, 3, 4)
    assert sr[:4] == bytes([0x80, 200, 0, 6]) and len(sr) == 28
    assert struct.unpack_from(">IQI", sr, 4) == (5, 0x1122334455667788, 99)


def make_sender(clock=lambda: 100.0, **kw):
    args = dict(kind="video", ssrc=1000, receiver_ssrc=1001, pt=96, key=bytes(16), iv_mask=bytes(16),
                timebase=90000, origin=99.0, target_delay_ms=100, clock=clock)
    args.update(kw)
    return cr.StreamSender(**args)


def test_shared_origin_rtp_timestamps():
    v = make_sender()
    a = make_sender(kind="audio", ssrc=2000, receiver_ssrc=2001, pt=127, timebase=48000)
    assert v.rtp_ts_for(100.0) == 90000 and a.rtp_ts_for(100.0) == 48000  # common zero, no random base


def test_stream_sender_first_frame_has_sr_and_delay_ext():
    s = make_sender()
    out = s.build_frame(b"\x00" * 5000, True, 99.5, now=100.0)
    assert out[0].fid == -1 and out[0].data[1] == cr.RTCP_SR  # SR precedes packet 0
    media = [p for p in out if p.fid >= 0]
    assert len(media) == 4 and media[0].data[12] & 0x80 and media[0].data[12] & 0x01
    rtp_ts = struct.unpack_from(">I", media[0].data, 4)[0]
    assert rtp_ts == 45000
    out2 = s.build_frame(b"\x00" * 10, False, 99.6, now=100.1)
    assert out2[0].fid == 1 and out2[0].data[16] == 0  # ref frame id = 0


def test_stream_sender_ack_nack_retransmit():
    t = [100.0]
    s = make_sender(clock=lambda: t[0])
    for i in range(5):
        for p in s.build_frame(bytes(4000), i == 0, 99.9 + i / 30, now=t[0]):
            s.on_sent(p, t[0])
    t[0] += 0.05
    # checkpoint frame 1, frame 3 packet 1 lost (+ next bit -> packet 2), frame 4 ACKed out of order
    r = cr.parse_receiver_rtcp(feedback(1001, 1000, 1, nacks=[(3, 1, 0b1)], acks_bits=bytes([0b10])))
    resend = s.handle_rtcp(r, t[0])
    assert s.checkpoint == 1 and s.st["acked"] == 3
    assert s.frames[4].acked and not s.frames[3].acked
    assert [(p.fid, p.idx, p.retx) for p in resend] == [(3, 1, True), (3, 2, True)]
    # immediate duplicate NACK is throttled
    assert s.handle_rtcp(r, t[0] + 0.001) == []
    # whole-frame loss
    r2 = cr.parse_receiver_rtcp(feedback(1001, 1000, 1, nacks=[(2, cr.ALL_PACKETS_LOST, 0)]))
    assert {p.idx for p in s.handle_rtcp(r2, t[0] + 0.1)} == {0, 1, 2}


def test_stream_sender_rtt_from_rr():
    t = [100.0]
    s = make_sender(clock=lambda: t[0])
    sr = s.sender_report(now=100.0, wall=1_700_000_000.0)
    ntp = struct.unpack_from(">Q", sr, 8)[0]
    lsr = (ntp >> 16) & 0xFFFFFFFF
    r = cr.parse_receiver_rtcp(rr(1001, 1000, lsr, dlsr=int(0.002 * 65536), fraction_lost=64))
    s.handle_rtcp(r, 100.010)
    rtt = s.w_rtt.median(100.010)
    assert abs(rtt - 8.0) < 0.1 and s.fraction_lost == 0.25


def test_stream_sender_waits_for_keyframe_when_window_full():
    s = make_sender()
    for i in range(cr.UNACKED_SOFT_LIMIT):
        assert s.build_frame(b"x", i == 0, 99.9, now=100.0)
    assert s.build_frame(b"x", False, 99.9, now=100.0) is None
    assert s.keyframe_wanted and s.st["dropped"] == 1
    assert s.build_frame(b"x", True, 99.9, now=100.0) is not None


def test_pli_sets_keyframe_wanted():
    s = make_sender()
    s.build_frame(b"x", True, 99.9, now=100.0)
    s.handle_rtcp(cr.parse_receiver_rtcp(struct.pack(">BBHII", 0x81, 206, 2, 1001, 1000)), 100.1)
    assert s.keyframe_wanted and s.st["pli"] == 1


def test_pacer_token_bucket():
    s = make_sender()
    pkts = [cr.OutPacket(bytes(1000), s) for _ in range(100)]
    p = cr.Pacer(rate_bps=8e6, burst_bytes=10_000, clock=lambda: 0.0)  # 1 MB/s
    p.push(pkts)
    first = p.pop_ready(0.0)
    assert len(first) == 10  # burst
    assert p.pop_ready(0.0) == []
    assert abs(p.delay_until_next() - 0.0) < 1e-9 or p.delay_until_next() > 0
    assert len(p.pop_ready(0.005)) == 5  # 5 ms × 1 MB/s = 5 kB
    hi = cr.OutPacket(b"retx", s, retx=True)
    p.push([hi], priority=True)
    assert p.pop_ready(0.006)[0] is hi


def test_router_by_receiver_ssrc():
    v = make_sender()
    a = make_sender(kind="audio", ssrc=2000, receiver_ssrc=2001)
    r = cr.RtcpRouter()
    r.add(v)
    r.add(a)
    s, res = r.route(feedback(2001, 2000, 0))
    assert s is a and res.checkpoint8 == 0
    assert r.route(feedback(9999, 1, 0)) is None
    assert r.route(b"\x80\x60" + bytes(20)) is None  # RTP, not RTCP
