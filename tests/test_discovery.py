import asyncio
import contextlib

import pytest

from airlinx.core import discovery as disc

SSDP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"CACHE-CONTROL: max-age=1800\r\n"
    b"DATE: Tue, 10 Nov 2026 10:00:00 GMT\r\n"
    b"EXT:\r\n"
    b"LOCATION: http://192.0.2.10:9197/dmr\r\n"
    b"SERVER: SHP, UPnP/1.0, Samsung UPnP SDK/1.0\r\n"
    b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    b"USN: uuid:a1b2c3d4-0000-1111-2222-333344445555::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    b"Content-Length: 0\r\n\r\n"
)

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns='urn:schemas-upnp-org:device-1-0' xmlns:sec='http://www.sec.co.kr/dlna' xmlns:dlna='urn:schemas-dlna-org:device-1-0'>
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
  <friendlyName>[TV] Salon</friendlyName>
  <manufacturer>Samsung Electronics</manufacturer>
  <modelName>S85FAE</modelName>
  <UDN>uuid:a1b2c3d4-0000-1111-2222-333344445555</UDN>
  <serviceList>
   <service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
    <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId><controlURL>/upnp/control/RenderingControl1</controlURL></service>
   <service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
    <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId><controlURL>/upnp/control/AVTransport1</controlURL></service>
  </serviceList>
 </device>
</root>"""

CAST_TXT = {b"id": b"8f2a1b3c4d5e6f708192a3b4c5d6e7f8", b"fn": "Salon".encode(), b"md": b"Samsung S85F",
            b"ca": b"264709", b"st": b"0"}


def test_parse_ssdp_response():
    h = disc.parse_ssdp_response(SSDP_RESPONSE)
    assert h["location"] == "http://192.0.2.10:9197/dmr"
    assert h["st"].endswith("MediaRenderer:1")
    assert disc.parse_ssdp_response(b"M-SEARCH * HTTP/1.1\r\n\r\n") is None
    assert disc.parse_ssdp_response(b"garbage") is None


def test_build_msearch():
    m = disc.build_msearch().decode()
    assert m.startswith("M-SEARCH * HTTP/1.1\r\n") and "ST: urn:schemas-upnp-org:device:MediaRenderer:1" in m
    assert 'MAN: "ssdp:discover"' in m and m.endswith("\r\n\r\n")


def test_parse_description():
    d = disc.parse_description(DESCRIPTION)
    assert d["friendly_name"] == "[TV] Salon"
    assert d["model_name"] == "S85FAE"
    assert d["udn"].startswith("uuid:a1b2")
    assert d["services"]["urn:schemas-upnp-org:service:AVTransport:1"] == "/upnp/control/AVTransport1"


def test_parse_cast_txt_bytes_and_str():
    t = disc.parse_cast_txt(CAST_TXT)
    assert t["name"] == "Salon" and t["model"] == "Samsung S85F" and t["id"].startswith("8f2a")
    assert disc.parse_cast_txt({"fn": "X"})["name"] == "X"


def test_merge_same_ip_into_one_device():
    seen = []
    d = disc.Discovery(seen.append, interface_ip="192.0.2.16")
    d.upsert_dlna("192.0.2.10", "http://192.0.2.10:9197/dmr", disc.parse_description(DESCRIPTION))
    assert d.devices[0].name == "[TV] Salon" and d.devices[0].caps == {"dlna"}
    d.upsert_cast("192.0.2.10", 8009, disc.parse_cast_txt(CAST_TXT))
    assert len(d.devices) == 1
    dev = d.devices[0]
    assert dev.caps == {"cast_mirror", "cast_media", "dlna"}
    assert dev.name == "Salon"  # Cast name wins
    assert dev.id == "8f2a1b3c4d5e6f708192a3b4c5d6e7f8"
    assert dev.cast_port == 8009 and dev.dlna_location.endswith("/dmr")
    assert dev.model == "Samsung S85F"
    # later SSDP refresh must not override Cast identity
    d.upsert_dlna("192.0.2.10", "http://192.0.2.10:9197/dmr", disc.parse_description(DESCRIPTION))
    assert d.devices[0].name == "Salon" and d.devices[0].id.startswith("8f2a")
    assert len(seen) == 2  # identical refresh doesn't re-notify


def test_offline_tracking():
    seen = []
    d = disc.Discovery(seen.append)
    d.upsert_cast("192.0.2.20", 8009, {"id": "abc", "name": "Mini", "model": "Chromecast"}, zc_name="X._googlecast")
    d.upsert_dlna("192.0.2.30", "http://192.0.2.30:1/d", {"friendly_name": "Old TV", "udn": "uuid:1-2"})
    assert all(x.online for x in d.devices)
    d._cast_removed("X._googlecast")
    mini = next(x for x in d.devices if x.ip == "192.0.2.20")
    assert not mini.online and seen[-1][-1].ip == "192.0.2.20"  # offline sorted last
    e = d._entries["192.0.2.30"]
    for _ in range(disc.SSDP_MISSES_OFFLINE):
        e.dlna_misses += 1
    # simulate the bookkeeping that _ssdp_scan does when no reply arrives
    e.dlna_alive = False
    d._update_online(e)
    assert not e.device.online
    assert e.device.id == "dlna:12"


@pytest.mark.asyncio
async def test_ssdp_scan_with_fake_transport(monkeypatch):
    """Drive _ssdp_scan with a fake datagram endpoint + fake description fetch."""
    import asyncio

    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")

    class FakeTransport:
        def __init__(self, proto):
            self.proto = proto

        def sendto(self, data, addr):
            assert b"M-SEARCH" in data
            asyncio.get_running_loop().call_soon(self.proto.datagram_received, SSDP_RESPONSE, ("192.0.2.10", 1900))

        def close(self):
            pass

    loop = asyncio.get_running_loop()

    async def fake_endpoint(factory, sock=None, **kw):
        sock.close()
        p = factory()
        return FakeTransport(p), p

    monkeypatch.setattr(loop, "create_datagram_endpoint", fake_endpoint)

    async def fake_fetch(loc):
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = fake_fetch
    await d._ssdp_scan(timeout=0.7)
    assert [x.name for x in d.devices] == ["[TV] Salon"]
    # a scan with no answers marks it offline after N misses
    async def silent_endpoint(factory, sock=None, **kw):
        sock.close()
        p = factory()

        class T(FakeTransport):
            def sendto(self, data, addr):
                pass
        return T(p), p

    monkeypatch.setattr(loop, "create_datagram_endpoint", silent_endpoint)
    for _ in range(disc.SSDP_MISSES_OFFLINE):
        await d._ssdp_scan(timeout=0.7)
    assert not d.devices[0].online


def test_valid_location_rules():
    v = disc.valid_location
    assert v("http://192.0.2.10:9197/dmr", "192.0.2.10")
    assert v("https://192.0.2.10/d.xml", "192.0.2.10")
    assert not v("http://192.0.2.11:9197/dmr", "192.0.2.10")       # host != sender
    assert not v("http://evil.example/dmr", "192.0.2.10")            # hostname, not sender IP
    assert not v("ftp://192.0.2.10/dmr", "192.0.2.10")             # scheme
    assert not v("file:///etc/passwd", "192.0.2.10")
    assert not v("http://127.0.0.1:8080/x", "127.0.0.1")               # loopback
    assert not v("http://169.254.169.254/latest", "169.254.169.254")   # link-local / metadata
    assert not v("http://user:pw@192.0.2.10/x", "192.0.2.10")      # userinfo
    assert not v("http://192.0.2.10:99999/x", "192.0.2.10")        # bad port


@pytest.mark.asyncio
async def test_ssdp_spoofed_location_ignored(monkeypatch):
    import asyncio

    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    spoof = SSDP_RESPONSE.replace(b"http://192.0.2.10:9197/dmr", b"http://169.254.169.254/latest/meta-data")

    class T:
        def __init__(self, p):
            self.p = p

        def sendto(self, data, addr):
            loop = asyncio.get_running_loop()
            loop.call_soon(self.p.datagram_received, spoof, ("192.0.2.10", 1900))
            other = SSDP_RESPONSE  # valid location, but sent from another host
            loop.call_soon(self.p.datagram_received, other, ("192.0.2.99", 1900))

        def close(self):
            pass

    async def ep(factory, sock=None, **kw):
        sock.close()
        p = factory()
        return T(p), p

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", ep)
    fetched = []

    async def fake_fetch(loc):
        fetched.append(loc)
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = fake_fetch
    await d._ssdp_scan(timeout=0.7)
    assert fetched == [] and d.devices == []


class _FakeContent:
    """Mimics aiohttp StreamReader: read(n) returns at most 100 bytes per call."""

    def __init__(self, body):
        self.body = body

    async def read(self, n=-1):
        n = min(n, 100) if n >= 0 else 100
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk


class _FakeResp:
    def __init__(self, status, body, length=None):
        self.status, self.content, self.content_length = status, _FakeContent(body), length

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def get(self, url, **kw):
        self.kwargs = kw
        return self.resp


@pytest.mark.asyncio
async def test_description_fetch_no_redirects_and_size_limit():
    d = disc.Discovery(lambda devs: None)
    d._session = _FakeSession(_FakeResp(200, DESCRIPTION.encode()))
    assert (await d._fetch_description("http://192.0.2.10:9197/dmr"))["model_name"] == "S85FAE"
    assert d._session.kwargs["allow_redirects"] is False
    d._session = _FakeSession(_FakeResp(302, b""))
    assert await d._fetch_description("http://192.0.2.10:9197/dmr") is None
    big = b"<root>" + b" " * (disc.MAX_DESCRIPTION_BYTES + 10) + b"</root>"
    d._session = _FakeSession(_FakeResp(200, big))                      # no Content-Length header
    assert await d._fetch_description("http://192.0.2.10:9197/dmr") is None
    d._session = _FakeSession(_FakeResp(200, b"<root/>", length=10_000_000))
    assert await d._fetch_description("http://192.0.2.10:9197/dmr") is None


@pytest.mark.asyncio
async def test_add_manual_rejects_bad_ips():
    d = disc.Discovery(lambda devs: None)
    for ip in ("127.0.0.1", "169.254.1.1", "not-an-ip", "224.0.0.1", "0.0.0.0", "::1"):
        assert await d.add_manual(ip) is None


# ------------------------------------------------------------------ hardening: interface IP follows the network

async def _start_without_loop(d):
    """start() but with the background scan loop stopped, so the test drives rescan() itself."""
    await d.start()
    d._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await d._task


@pytest.mark.asyncio
async def test_rescan_follows_lan_ip_change_and_restarts_mdns(monkeypatch):
    """Autostart before NetworkManager: get_lan_ip() is 127.0.0.1 first, the DHCP lease arrives later."""
    ips = iter(["127.0.0.1", "192.168.1.16", "192.168.1.16", "10.0.0.5"])
    monkeypatch.setattr(disc, "get_lan_ip", lambda: next(ips))
    d = disc.Discovery(lambda devs: None, rescan_interval=3600)
    events, bound = [], []

    async def fake_start_mdns():
        events.append(("start", d.interface_ip))

    async def fake_stop_mdns():
        events.append(("stop", d.interface_ip))

    async def fake_ssdp(target=None, timeout=3.0):
        bound.append(d.interface_ip)
        return []

    monkeypatch.setattr(d, "_start_mdns", fake_start_mdns)
    monkeypatch.setattr(d, "_stop_mdns", fake_stop_mdns)
    monkeypatch.setattr(d, "_ssdp_scan", fake_ssdp)
    await _start_without_loop(d)
    assert d.interface_ip == "127.0.0.1"
    await d.rescan()                       # network came up
    assert d.interface_ip == "192.168.1.16"
    await d.rescan()                       # unchanged: no restart
    await d.rescan()                       # moved to another network
    assert d.interface_ip == "10.0.0.5"
    assert bound == ["192.168.1.16", "192.168.1.16", "10.0.0.5"]   # SSDP binds to the fresh address
    assert [e for e in events if e[0] == "start"] == [("start", "127.0.0.1"), ("start", "192.168.1.16"),
                                                       ("start", "10.0.0.5")]
    assert len([e for e in events if e[0] == "stop"]) == 2          # one restart per change, none for "unchanged"
    await d.stop()


@pytest.mark.asyncio
async def test_explicit_interface_ip_is_never_replaced(monkeypatch):
    monkeypatch.setattr(disc, "get_lan_ip", lambda: "10.9.9.9")
    d = disc.Discovery(lambda devs: None, interface_ip="192.168.1.16", enable_mdns=False)

    async def fake_ssdp(target=None, timeout=3.0):
        return []

    monkeypatch.setattr(d, "_ssdp_scan", fake_ssdp)
    await _start_without_loop(d)
    await d.rescan()
    assert d.interface_ip == "192.168.1.16"
    await d.stop()


# ------------------------------------------------------------------ hardening: SSDP scan cannot be stalled

def _fake_ssdp_endpoint(monkeypatch, datagrams):
    """Replace the UDP endpoint: every sendto() makes `datagrams` [(bytes, (ip, port))] arrive."""
    class T:
        def __init__(self, p):
            self.p = p

        def sendto(self, data, addr):
            loop = asyncio.get_running_loop()
            for raw, src in datagrams:
                loop.call_soon(self.p.datagram_received, raw, src)

        def close(self):
            pass

    async def ep(factory, sock=None, **kw):
        sock.close()
        p = factory()
        return T(p), p

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", ep)


@pytest.mark.asyncio
async def test_ssdp_flood_from_one_host_fetches_one_description_and_scan_stays_bounded(monkeypatch):
    """1000 replies with different LOCATION ports from one IP, and a description server that never answers."""
    import time

    flood = [(SSDP_RESPONSE.replace(b":9197/", f":{20000 + i}/".encode()), ("192.168.1.10", 1900))
             for i in range(1000)]
    _fake_ssdp_endpoint(monkeypatch, flood)
    monkeypatch.setattr(disc, "SSDP_SCAN_DEADLINE", 1.0)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    fetched = []

    async def hanging_fetch(loc):
        fetched.append(loc)
        await asyncio.sleep(3600)

    d._fetch_description = hanging_fetch
    t0 = time.monotonic()
    await d._ssdp_scan(timeout=0.7)
    assert time.monotonic() - t0 < 4
    assert len(fetched) == 1                      # at most one reply per sender IP
    assert d.devices == []


@pytest.mark.asyncio
async def test_ssdp_many_hosts_are_fetched_concurrently_within_the_deadline(monkeypatch):
    import time

    hosts = [(SSDP_RESPONSE.replace(b"192.168.1.10", f"192.168.1.{h}".encode()), (f"192.168.1.{h}", 1900))
             for h in range(20, 60)]
    _fake_ssdp_endpoint(monkeypatch, hosts)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    active = peak = 0

    async def slow_fetch(loc):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.3)
        active -= 1
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = slow_fetch
    t0 = time.monotonic()
    await d._ssdp_scan(timeout=0.7)
    assert time.monotonic() - t0 < 0.7 + 40 * 0.3 / 2   # sequential would need 12 s
    assert 1 < peak <= disc.SSDP_FETCH_CONCURRENCY
    assert len(d.devices) == 40


@pytest.mark.asyncio
async def test_ssdp_unresponsive_first_host_does_not_block_the_others(monkeypatch):
    hosts = [(SSDP_RESPONSE.replace(b"192.168.1.10", f"192.168.1.{h}".encode()), (f"192.168.1.{h}", 1900))
             for h in (21, 22)]
    _fake_ssdp_endpoint(monkeypatch, hosts)
    monkeypatch.setattr(disc, "SSDP_SCAN_DEADLINE", 1.0)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")

    async def fetch(loc):
        if "192.168.1.21" in loc:
            await asyncio.sleep(3600)
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    assert [x.ip for x in d.devices] == ["192.168.1.22"]


# ------------------------------------------------------------------ hardening: mDNS announcements are untrusted

class _FakeInfo:
    def __init__(self, addrs, port, props=None):
        self._addrs, self.port, self.properties = addrs, port, props or CAST_TXT

    async def async_request(self, zc, timeout):
        return True

    def parsed_addresses(self):
        return self._addrs


def _patch_info(monkeypatch, info):
    import zeroconf.asyncio as zca

    monkeypatch.setattr(zca, "AsyncServiceInfo", lambda *a, **k: info)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["127.0.0.1", "169.254.10.10", "224.0.0.251", "0.0.0.0", "240.0.0.1"])
async def test_resolve_cast_rejects_unusable_addresses(monkeypatch, bad):
    d = disc.Discovery(lambda devs: None, interface_ip="192.168.1.16")
    d._azc = type("Z", (), {"zeroconf": None})()
    _patch_info(monkeypatch, _FakeInfo([bad], 8009))
    await d._resolve_cast("_googlecast._tcp.local.", "Fake._googlecast._tcp.local.")
    assert d.devices == []


@pytest.mark.asyncio
async def test_resolve_cast_skips_bad_addresses_and_uses_the_first_usable(monkeypatch):
    d = disc.Discovery(lambda devs: None, interface_ip="192.168.1.16")
    d._azc = type("Z", (), {"zeroconf": None})()
    _patch_info(monkeypatch, _FakeInfo(["127.0.0.1", "169.254.1.1", "192.168.1.10"], 8009))
    await d._resolve_cast("_googlecast._tcp.local.", "Real._googlecast._tcp.local.")
    assert [x.ip for x in d.devices] == ["192.168.1.10"]


def test_foreign_announcement_cannot_overwrite_a_live_service():
    d = disc.Discovery(lambda devs: None, interface_ip="192.168.1.16")
    real = disc.parse_cast_txt(CAST_TXT)
    d.upsert_cast("192.168.1.10", 8009, real, zc_name="Real._googlecast._tcp.local.")
    d.upsert_cast("192.168.1.10", 9, {"id": "deadbeef", "name": "Pwned", "model": "x"},
                  zc_name="Evil._googlecast._tcp.local.")
    dev = d.devices[0]
    assert dev.cast_port == 8009 and dev.id == "8f2a1b3c4d5e6f708192a3b4c5d6e7f8" and dev.name == "Goguś"
    assert d._entries["192.168.1.10"].zc_names == {"Real._googlecast._tcp.local."}
    # the evil service going away must not take the real one offline
    d._cast_removed("Evil._googlecast._tcp.local.")
    assert dev.online


def test_same_service_may_update_its_own_entry_and_a_dead_one_may_be_replaced():
    d = disc.Discovery(lambda devs: None, interface_ip="192.168.1.16")
    ip, name = "192.168.1.10", "Real._googlecast._tcp.local."
    d.upsert_cast(ip, 8009, {"id": "aa", "name": "A", "model": "m"}, zc_name=name)
    d.upsert_cast(ip, 8010, {"id": "aa", "name": "A2", "model": "m"}, zc_name=name)   # same service: update
    assert d.devices[0].cast_port == 8010 and d.devices[0].name == "A2"
    d._cast_removed(name)                                                              # service gone
    d.upsert_cast(ip, 8009, {"id": "bb", "name": "B", "model": "m"}, zc_name="New._googlecast._tcp.local.")
    assert d.devices[0].id == "bb" and d.devices[0].cast_port == 8009 and d.devices[0].online


# ------------------------------------------------------------------ hardening 2: hostile replies cannot evict real devices

def _resp(ip, port):
    return (SSDP_RESPONSE.replace(b"192.168.1.10:9197", f"{ip}:{port}".encode()), (ip, 1900))


@pytest.mark.asyncio
async def test_flood_of_hosts_does_not_mark_a_known_tv_offline(monkeypatch):
    flood = [_resp(f"10.{i // 250}.{i % 250}.7", 9197) for i in range(1000)]
    tv = _resp("192.168.1.10", 9197)
    _fake_ssdp_endpoint(monkeypatch, flood + [tv])          # the real TV answers last
    monkeypatch.setattr(disc, "SSDP_SCAN_DEADLINE", 1.0)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    d.upsert_dlna("192.168.1.10", "http://192.168.1.10:9197/dmr", {"friendly_name": "Old name", "udn": "uuid:1-2"})
    d._entries["192.168.1.10"].dlna_misses = disc.SSDP_MISSES_OFFLINE - 1

    async def fetch(loc):
        if "192.168.1.10" in loc:
            return {**disc.parse_description(DESCRIPTION), "friendly_name": "New name"}
        await asyncio.sleep(3600)

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    dev = d.devices[0] if d.devices[0].ip == "192.168.1.10" else next(x for x in d.devices if x.ip == "192.168.1.10")
    assert dev.online and dev.name == "New name"
    assert d._entries["192.168.1.10"].dlna_misses == 0


@pytest.mark.asyncio
async def test_abandoned_fetch_of_a_responding_known_host_is_not_a_miss(monkeypatch):
    _fake_ssdp_endpoint(monkeypatch, [_resp("192.168.1.10", 9197)])
    monkeypatch.setattr(disc, "SSDP_SCAN_DEADLINE", 0.5)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    d.upsert_dlna("192.168.1.10", "http://192.168.1.10:9197/dmr", {"friendly_name": "TV", "udn": "uuid:1-2"})
    e = d._entries["192.168.1.10"]
    e.dlna_misses = disc.SSDP_MISSES_OFFLINE - 1

    async def hang(loc):
        await asyncio.sleep(3600)

    d._fetch_description = hang
    await d._ssdp_scan(timeout=0.7)
    assert e.dlna_misses == disc.SSDP_MISSES_OFFLINE - 1 and e.device.online
    # a host that really stayed silent still counts
    _fake_ssdp_endpoint(monkeypatch, [])
    await d._ssdp_scan(timeout=0.7)
    assert e.dlna_misses == disc.SSDP_MISSES_OFFLINE and not e.device.online


@pytest.mark.asyncio
async def test_fast_bogus_location_does_not_displace_the_real_description(monkeypatch):
    bogus = _resp("192.168.1.10", 1)                          # same sender IP, wrong port, answers first
    real = _resp("192.168.1.10", 9197)
    _fake_ssdp_endpoint(monkeypatch, [bogus, real])
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    tried = []

    async def fetch(loc):
        tried.append(loc)
        return None if loc.endswith(":1/dmr") else disc.parse_description(DESCRIPTION)

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    assert tried == ["http://192.168.1.10:1/dmr", "http://192.168.1.10:9197/dmr"]
    assert d.devices[0].dlna_location == "http://192.168.1.10:9197/dmr"


@pytest.mark.asyncio
async def test_hanging_location_times_out_and_next_one_is_tried(monkeypatch):
    _fake_ssdp_endpoint(monkeypatch, [_resp("192.168.1.10", 1), _resp("192.168.1.10", 9197)])
    monkeypatch.setattr(disc, "SSDP_FETCH_TIMEOUT", 0.3)
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")

    async def fetch(loc):
        if loc.endswith(":1/dmr"):
            await asyncio.sleep(3600)
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    assert d.devices[0].dlna_location.endswith(":9197/dmr")


@pytest.mark.asyncio
async def test_locations_per_host_are_capped(monkeypatch):
    _fake_ssdp_endpoint(monkeypatch, [_resp("192.168.1.10", 20000 + i) for i in range(50)])
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    tried = []

    async def fetch(loc):
        tried.append(loc)
        return None

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    assert len(tried) == disc.SSDP_MAX_LOCATIONS_PER_HOST


@pytest.mark.asyncio
async def test_known_hosts_are_kept_when_the_host_limit_is_hit(monkeypatch):
    flood = [_resp(f"10.0.{i // 250}.{i % 250 + 1}", 9197) for i in range(disc.SSDP_MAX_HOSTS + 20)]
    _fake_ssdp_endpoint(monkeypatch, flood + [_resp("192.168.1.10", 9197)])
    d = disc.Discovery(lambda devs: None, interface_ip="127.0.0.1")
    d.upsert_dlna("192.168.1.10", "http://192.168.1.10:9197/dmr", {"friendly_name": "TV", "udn": "uuid:1-2"})
    tried = []

    async def fetch(loc):
        tried.append(loc)
        return disc.parse_description(DESCRIPTION)

    d._fetch_description = fetch
    await d._ssdp_scan(timeout=0.7)
    assert "http://192.168.1.10:9197/dmr" in tried and len(tried) == disc.SSDP_MAX_HOSTS
