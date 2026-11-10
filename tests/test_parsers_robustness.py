"""Everything a TV or any LAN host can send us goes through a parser. None of them may crash, hang or
over-allocate on hostile or damaged input, and the happy-path edge cases (wraparound, units) must be right."""
import random
import struct
import time

import pytest

from airlinx.core import discovery as dc
from airlinx.core import probe
from airlinx.core.cast import castv2 as cv
from airlinx.core.cast import rtp as cr

from test_cast_rtp import TV_RTCP, feedback, rr

RNG = random.Random(1234)


def mutations(blob: bytes, n=300):
    yield b""
    yield blob[:1]
    for i in range(len(blob) + 1):             # every truncation
        yield blob[:i]
    for _ in range(n):                         # bit flips
        b = bytearray(blob)
        for _ in range(RNG.randint(1, 4)):
            b[RNG.randrange(len(b))] ^= 1 << RNG.randrange(8)
        yield bytes(b)
    for _ in range(n):                         # random garbage
        yield bytes(RNG.getrandbits(8) for _ in range(RNG.randint(0, 200)))


# ----------------------------------------------------------------------------- RTCP (UDP from the TV)

def test_rtcp_parser_never_raises_on_damaged_packets():
    seeds = [TV_RTCP, feedback(2, 1, 10, nacks=[(12, 3, 5)], acks_bits=b"\x03"), rr(5, 6, 7, 8, 9)]
    for seed in seeds:
        for m in mutations(seed):
            cr.parse_receiver_rtcp(m)
            cr.receiver_ssrc_of(m)


def test_rtcp_huge_length_field_does_not_loop_or_overread():
    pkt = struct.pack(">BBH", 0x80 | 15, 206, 0xFFFF) + b"\x00" * 8
    t = time.monotonic()
    r = cr.parse_receiver_rtcp(pkt * 50)
    assert time.monotonic() - t < 1
    assert r.nacks == []


def test_rtcp_wrong_version_stops_parsing_but_keeps_earlier_blocks():
    good = rr(5, 6, 7, 8)
    r = cr.parse_receiver_rtcp(good + bytes([0x00, 201, 0, 0]) + rr(9, 9, 9, 9))
    assert r.report_block["receiver_ssrc"] == 5
    assert ("badver", 0) in r.unknown


def test_rtcp_cast_receiver_log_is_decoded():
    # APP packet, subtype 2 (receiver log), "CAST": one frame with two events
    events = struct.pack(">HH", 33, (14 << 12) | 5) + struct.pack(">HH", 0xFFFE, (12 << 12) | 7)   # PacketReceived, RenderDelay(-2)
    body = struct.pack(">II", 0x11223344, 0x43415354) + struct.pack(">II", 777, (1 << 24) | 0xABCDEF) + events
    pkt = struct.pack(">BBH", 0x80 | 2, 204, len(body) // 4) + body
    r = cr.parse_receiver_rtcp(pkt)
    assert r.logs == [(777, 0xABCDEF, [("PacketReceived", 33, 5), ("RenderDelay", -2, 7)])]


def test_rtcp_unknown_packet_types_are_recorded_not_fatal():
    pkt = struct.pack(">BBH", 0x80, 250, 1) + b"\x00" * 4
    assert cr.parse_receiver_rtcp(pkt).unknown == [(250, 0, 4)]


def test_rtcp_feedback_with_foreign_ident_is_ignored():
    body = struct.pack(">III", 1, 2, 0xDEADBEEF) + struct.pack(">BBH", 5, 0, 100)
    pkt = struct.pack(">BBH", 0x80 | 15, 206, len(body) // 4) + body
    r = cr.parse_receiver_rtcp(pkt)
    assert r.checkpoint8 is None and r.playout_delay_ms is None


@pytest.mark.parametrize("lower8,ref,gt,want", [
    (0, 0, False, 0), (255, 0, False, -1), (0, 255, False, 0), (255, 256, False, 255),
    (1, 255, True, 257), (0, 0, True, 256), (5, 1000, False, 1000 - (1000 - 5) % 256),
    (250, 10, False, -6), (3, 10, True, 259),
])
def test_expand8_wraparound(lower8, ref, gt, want):
    got = cr.expand8(lower8, ref, gt)
    assert got & 0xFF == lower8
    assert (got > ref) if gt else (got <= ref)
    assert abs(got - ref) <= 256
    assert got == want


def test_expand8_property_closest_id():
    for _ in range(2000):
        ref = RNG.randint(-300, 100_000)
        lo = RNG.randrange(256)
        lt, gt = cr.expand8(lo, ref), cr.expand8(lo, ref, True)
        assert lt <= ref < gt and gt - lt == 256 and lt & 0xFF == lo


def test_receiver_ssrc_of_rejects_non_rtcp():
    assert cr.receiver_ssrc_of(b"") is None
    assert cr.receiver_ssrc_of(b"\x80\x60\x00\x01" + b"\x00" * 8) is None      # RTP data, not RTCP
    assert cr.receiver_ssrc_of(bytes([0x40, 201, 0, 1]) + b"\x00" * 4) is None  # wrong version


# ----------------------------------------------------------------------------- CASTV2 framing

def test_castv2_decode_only_raises_valueerror_on_garbage():
    good = cv.encode_message(cv.CastMessage("sender-0", "receiver-0", cv.NS_RECEIVER, '{"type":"GET_STATUS"}'))
    for m in mutations(good):
        try:
            cv.decode_message(m)
        except ValueError:
            pass


def test_castv2_roundtrip_with_unicode_and_binary():
    m = cv.CastMessage("sender-0", "receiver-0", cv.NS_RECEIVER, '{"name":"Telewizor w salonie żółć"}')
    assert cv.decode_message(cv.encode_message(m)).payload_utf8 == m.payload_utf8
    b = cv.CastMessage("a", "b", cv.NS_DEVICEAUTH, None, bytes(range(256)))
    out = cv.decode_message(cv.encode_message(b))
    assert out.payload_binary == bytes(range(256)) and out.payload_utf8 is None


def test_castv2_frame_has_big_endian_length_prefix():
    m = cv.CastMessage("a", "b", "urn:x", "{}")
    framed = cv.frame_message(m)
    assert struct.unpack(">I", framed[:4])[0] == len(framed) - 4


# ----------------------------------------------------------------------------- SSDP / UPnP / mDNS

def test_ssdp_parser_never_raises():
    seed = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: http://192.0.2.10:9197/dmr\r\n"
            b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n")
    for m in mutations(seed):
        dc.parse_ssdp_response(m)


@pytest.mark.parametrize("raw,ok", [
    (b"HTTP/1.1 200 OK\r\nLocation: http://1.2.3.4/d.xml\r\n\r\n", True),
    (b"HTTP/1.1 200 OK\nLOCATION:http://1.2.3.4/d.xml\n\n", True),           # bare LF, no space after colon
    (b"NOTIFY * HTTP/1.1\r\nLOCATION: http://1.2.3.4/d.xml\r\nNTS: ssdp:alive\r\n\r\n", True),
    (b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n", False),                             # no location
    (b"HTTP/1.1 500 Server Error\r\nLOCATION: http://1.2.3.4/\r\n\r\n", False),
    (b"M-SEARCH * HTTP/1.1\r\nLOCATION: http://1.2.3.4/\r\n\r\n", False),    # our own query echoed back
    (b"\xff\xfe\x00garbage", False),
])
def test_ssdp_parser_cases(raw, ok):
    r = dc.parse_ssdp_response(raw)
    assert (r is not None) == ok
    if ok:
        assert r["location"] == "http://1.2.3.4/d.xml"


def test_msearch_is_a_valid_ssdp_request():
    m = dc.build_msearch().decode()
    assert m.startswith("M-SEARCH * HTTP/1.1\r\n") and m.endswith("\r\n\r\n")
    assert 'MAN: "ssdp:discover"' in m and "ST: urn:schemas-upnp-org:device:MediaRenderer:1" in m


BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
<!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
<!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
<!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
<!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">]>
<root><device><friendlyName>&lol7;</friendlyName></device></root>"""

XXE = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<root><device><friendlyName>&x;</friendlyName></device></root>"""


@pytest.mark.parametrize("xml", [BILLION_LAUGHS, XXE], ids=["billion-laughs", "external-entity"])
def test_device_description_rejects_entity_attacks_quickly(xml):
    t = time.monotonic()
    with pytest.raises(Exception):
        dc.parse_description(xml)
    assert time.monotonic() - t < 2


@pytest.mark.parametrize("xml", [b"", b"<root>", b"not xml", b"<a><b></a>", b"\x00\x01"])
def test_device_description_malformed_xml_raises_cleanly(xml):
    with pytest.raises(Exception):
        dc.parse_description(xml)


def test_device_description_without_renderer_picks_first_device_and_handles_empty():
    assert dc.parse_description("<root></root>") == {"services": {}}
    xml = ('<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceType>urn:x:device:Basic:1</deviceType>'
           "<friendlyName> Soundbar </friendlyName>"
           "<deviceList><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
           "<friendlyName>Salon</friendlyName><serviceList><service>"
           "<serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/avt</controlURL>"
           "</service></serviceList></device></deviceList></device></root>")
    d = dc.parse_description(xml)
    assert d["friendly_name"] == "Salon", "the embedded MediaRenderer wins over the root device"
    assert d["services"] == {"urn:schemas-upnp-org:service:AVTransport:1": "/avt"}


def test_cast_txt_records_tolerate_missing_and_binary_values():
    assert dc.parse_cast_txt({}) == {"id": "", "name": "", "model": "", "ca": "", "st": ""}
    p = dc.parse_cast_txt({b"fn": b"Salon \xff\xfe", b"md": b"Chromecast", b"id": b"abc"})
    assert p["name"].startswith("Salon") and p["model"] == "Chromecast" and p["id"] == "abc"
    assert dc.parse_cast_txt({"fn": "Salon", "id": "1"})["name"] == "Salon"
    assert dc.parse_cast_txt({b"fn": None})["name"] == ""


# ----------------------------------------------------------------------------- ffprobe JSON

WEIRD = [    # shapes real ffprobe emits for odd files: missing sections, "N/A" values, bare streams
    {}, {"format": {}, "streams": []},
    {"format": {"format_name": "mp3", "duration": "N/A"}, "streams": [{"codec_type": "audio"}]},
    {"format": {"format_name": "matroska,webm", "duration": "-5"}, "streams": [{"codec_type": "audio", "codec_name": "opus"}]},
    {"format": {"format_name": "x", "duration": "1e400"}, "streams": [{"codec_type": "video", "codec_name": "h264"}]},
    {"format": {"format_name": "x"}, "streams": [{"codec_type": "subtitle", "index": 3}]},
    {"format": {"format_name": "matroska,webm", "duration": "10"},
     "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]},
    {"format": {"format_name": "image2"}, "streams": [{"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}]},
]


@pytest.mark.parametrize("data", WEIRD)
@pytest.mark.parametrize("target", ["cast", "dlna"])
def test_probe_and_decide_survive_odd_ffprobe_output(data, target):
    info = probe.parse_probe(data)
    plan = probe.decide(info, target, nvenc=False)
    assert plan.mode in ("direct", "remux", "transcode", "unsupported") or plan.mode
    assert plan.mime


def test_probe_duration_garbage_is_none():
    for v in ("N/A", "", None, "-1", "0", "abc", [], {}):
        assert probe._f(v) is None
    assert probe._f("12.5") == 12.5


def test_url_guess_never_raises_and_uses_extension():
    for url in ("", "x", "http://h/a", "http://h/a.MKV?x=1", "file:///a/b.mp3", "http://h/%00.mp4", "." * 500):
        assert probe.plan_for_url_guess(url, "cast") is not None
        assert probe.plan_for_url_guess(url, "dlna") is not None
