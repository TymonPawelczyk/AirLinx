"""Device discovery: mDNS `_googlecast._tcp` + SSDP MediaRenderer, merged per IP.

A physical TV advertising both Cast and DLNA becomes a single Device with caps
{"cast_mirror", "cast_media", "dlna"}. Devices that disappear stay in the list with online=False.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import socket
import time
import defusedxml.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp

from .media_server import get_lan_ip, redact_url, scrub_urls
from .. import __version__
from .models import Device

log = logging.getLogger(__name__)

CAST_SERVICE = "_googlecast._tcp.local."
SSDP_ADDR = ("239.255.255.250", 1900)
DMR_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
RESCAN_INTERVAL = 60.0
SSDP_TIMEOUT = 3.0
SSDP_MISSES_OFFLINE = 2          # consecutive scans without an SSDP reply → dlna side offline
CAST_CAPS = {"cast_mirror", "cast_media"}
SAMSUNG_DMR_PORTS = (9197,)
MAX_DESCRIPTION_BYTES = 256 * 1024


# ------------------------------------------------------------------ parsing helpers

def parse_ssdp_response(data: bytes) -> Optional[dict]:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not lines or not (lines[0].startswith("HTTP/1.1 200") or lines[0].startswith("NOTIFY")):
        return None
    headers = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers if "location" in headers else None


def build_msearch(st: str = DMR_ST, mx: int = 2, host: str = "239.255.255.250:1900") -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {host}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {mx}\r\n"
            f"ST: {st}\r\n"
            f"USER-AGENT: Linux/1.0 UPnP/1.1 AirLinx/{__version__}\r\n\r\n").encode()


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_description(xml_text: str | bytes) -> dict:
    """UPnP device description → {friendly_name, model_name, manufacturer, udn, device_type, services:{type: controlURL}}."""
    root = ET.fromstring(xml_text)
    dev = None
    for el in root.iter():
        if _strip_ns(el.tag) == "device":
            dt = next((c.text for c in el if _strip_ns(c.tag) == "deviceType"), "") or ""
            if "MediaRenderer" in dt or dev is None:
                dev = el
                if "MediaRenderer" in dt:
                    break
    out: dict = {"services": {}}
    if dev is None:
        return out
    for c in dev:
        t = _strip_ns(c.tag)
        if t == "friendlyName":
            out["friendly_name"] = (c.text or "").strip()
        elif t == "modelName":
            out["model_name"] = (c.text or "").strip()
        elif t == "manufacturer":
            out["manufacturer"] = (c.text or "").strip()
        elif t == "UDN":
            out["udn"] = (c.text or "").strip()
        elif t == "deviceType":
            out["device_type"] = (c.text or "").strip()
    for svc in dev.iter():
        if _strip_ns(svc.tag) == "service":
            vals = {_strip_ns(x.tag): (x.text or "").strip() for x in svc}
            if vals.get("serviceType"):
                out["services"][vals["serviceType"]] = vals.get("controlURL", "")
    return out


def parse_cast_txt(props: dict) -> dict:
    def g(k: str) -> str:
        v = props.get(k.encode()) if isinstance(next(iter(props), b""), bytes) else props.get(k)
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="replace")
        return v or ""
    return {"id": g("id"), "name": g("fn"), "model": g("md"), "ca": g("ca"), "st": g("st")}


def valid_location(location: str, sender_ip: str) -> bool:
    """Accept an SSDP LOCATION only if it points back at the datagram's sender over http(s),
    and that address is a real unicast LAN/WAN host (no loopback / link-local / multicast)."""
    try:
        u = urlparse(location)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
            return False
        host = ipaddress.ip_address(u.hostname)
        sender = ipaddress.ip_address(sender_ip)
        _ = u.port  # raises ValueError on garbage ports
    except ValueError:
        return False
    if host != sender:
        return False
    return not (host.is_loopback or host.is_link_local or host.is_multicast or host.is_unspecified
                or host.is_reserved)


def normalize_udn(udn: str) -> str:
    return re.sub(r"^uuid:", "", udn or "").replace("-", "").lower()


# ------------------------------------------------------------------ state

@dataclass
class _Entry:
    device: Device
    cast_seen: Optional[float] = None
    cast_alive: bool = False
    dlna_misses: int = 0
    dlna_alive: bool = False
    manual: bool = False
    zc_names: set = field(default_factory=set)


class Discovery:
    def __init__(self, on_change: Callable[[list[Device]], None], *, interface_ip: Optional[str] = None,
                 rescan_interval: float = RESCAN_INTERVAL, enable_mdns: bool = True):
        self.on_change = on_change
        self.interface_ip = interface_ip
        self._explicit_ip = interface_ip is not None   # an address given by the caller is never auto-replaced
        self._started = False
        self.rescan_interval = rescan_interval
        self.enable_mdns = enable_mdns
        self._entries: dict[str, _Entry] = {}   # keyed by IP
        self._azc = None
        self._browser = None
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._bg: set[asyncio.Task] = set()
        self._scan_lock = asyncio.Lock()
        self._last_snapshot: Optional[list] = None

    @property
    def devices(self) -> list[Device]:
        return sorted((e.device for e in self._entries.values()), key=lambda d: (not d.online, d.name.lower()))

    # ---------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if self.interface_ip is None:
            self.interface_ip = get_lan_ip()
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4))
        if self.enable_mdns:
            await self._start_mdns()
        self._started = True
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        for t in list(self._bg):
            t.cancel()
        await self._stop_mdns()
        if self._session:
            await self._session.close()
        self._session = None
        self._started = False

    async def _stop_mdns(self) -> None:
        if self._browser:
            with contextlib.suppress(Exception):
                await self._browser.async_cancel()
        if self._azc:
            with contextlib.suppress(Exception):
                await self._azc.async_close()
        self._browser = self._azc = None

    async def _refresh_interface(self) -> None:
        """Autostart can run before the network is up (get_lan_ip() == 127.0.0.1) and DHCP can move us later:
        re-derive the address on every scan and, when it changed, rebind SSDP (it reads self.interface_ip per scan)
        and restart mDNS (zeroconf is bound to one interface)."""
        if self._explicit_ip or not self._started:
            return
        ip = get_lan_ip()
        if ip == self.interface_ip:
            return
        log.info("LAN address changed %s -> %s, rebinding discovery", self.interface_ip, ip)
        old, self.interface_ip = self.interface_ip, ip
        if self.enable_mdns:
            await self._stop_mdns()
            try:
                await self._start_mdns()
            except Exception:
                self.interface_ip = old        # mDNS is down now: keep the mismatch so the next scan retries
                raise

    async def rescan(self) -> None:
        async with self._scan_lock:
            await self._refresh_interface()
            await self._ssdp_scan()
            for ip, e in list(self._entries.items()):
                if e.manual and not e.device.online:
                    await self.add_manual(ip, _notify=False)
        self._notify()

    async def _loop(self) -> None:
        while True:
            try:
                await self.rescan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discovery scan failed")
            await asyncio.sleep(self.rescan_interval)

    # ---------------------------------------------------------- mDNS
    async def _start_mdns(self) -> None:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

        try:
            self._azc = AsyncZeroconf(interfaces=[self.interface_ip], ip_version=IPVersion.V4Only)
        except Exception:
            log.warning("zeroconf on %s failed, falling back to all interfaces", self.interface_ip)
            self._azc = AsyncZeroconf(ip_version=IPVersion.V4Only)

        def handler(zeroconf, service_type, name, state_change):  # called in the loop thread
            if state_change is ServiceStateChange.Removed:
                self._cast_removed(name)
            else:
                self._spawn(self._resolve_cast(service_type, name))

        self._browser = AsyncServiceBrowser(self._azc.zeroconf, [CAST_SERVICE], handlers=[handler])

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _resolve_cast(self, service_type: str, name: str) -> None:
        from zeroconf.asyncio import AsyncServiceInfo

        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self._azc.zeroconf, 3000):
            log.debug("could not resolve %s", name)
            return
        addrs = [a for a in info.parsed_addresses() if ":" not in a]
        if not addrs:
            return
        txt = parse_cast_txt(info.properties or {})
        self.upsert_cast(addrs[0], info.port or 8009, txt, zc_name=name)

    def _cast_removed(self, name: str) -> None:
        for e in self._entries.values():
            if name in e.zc_names:
                e.zc_names.discard(name)
                e.cast_alive = False
                self._update_online(e)
        self._notify()

    # ---------------------------------------------------------- merge
    def _entry(self, ip: str) -> _Entry:
        e = self._entries.get(ip)
        if e is None:
            e = _Entry(Device(id=f"ip:{ip}", name=ip, model="", ip=ip, caps=set()))
            self._entries[ip] = e
        return e

    def upsert_cast(self, ip: str, port: int, txt: dict, zc_name: Optional[str] = None) -> Device:
        e = self._entry(ip)
        d = e.device
        if txt.get("id"):
            d.id = txt["id"].replace("-", "").lower()
        if txt.get("name"):
            d.name = txt["name"]
        if txt.get("model"):
            d.model = txt["model"]
        d.cast_port = port
        d.caps |= CAST_CAPS
        e.cast_alive, e.cast_seen = True, time.monotonic()
        if zc_name:
            e.zc_names.add(zc_name)
        self._update_online(e)
        self._notify()
        return d

    def upsert_dlna(self, ip: str, location: str, desc: dict) -> Device:
        e = self._entry(ip)
        d = e.device
        d.dlna_location = location
        d.caps.add("dlna")
        if not e.cast_alive and d.cast_port is None:   # Cast metadata wins when present
            if desc.get("udn"):
                d.id = "dlna:" + normalize_udn(desc["udn"])
        if desc.get("friendly_name") and (d.name == ip or not e.cast_alive and d.cast_port is None):
            d.name = desc["friendly_name"]
        if desc.get("model_name") and not d.model:
            d.model = desc["model_name"]
        e.dlna_alive, e.dlna_misses = True, 0
        self._update_online(e)
        self._notify()
        return d

    @staticmethod
    def _update_online(e: _Entry) -> None:
        e.device.online = e.cast_alive or e.dlna_alive

    def _notify(self) -> None:
        snap = [(d.id, d.name, d.model, d.ip, d.cast_port, d.dlna_location, tuple(sorted(d.caps)), d.online)
                for d in self.devices]
        if snap == self._last_snapshot:
            return
        self._last_snapshot = snap
        try:
            self.on_change(self.devices)
        except Exception:
            log.exception("on_change callback failed")

    # ---------------------------------------------------------- SSDP
    async def _ssdp_scan(self, target: Optional[tuple[str, int]] = None, timeout: float = SSDP_TIMEOUT) -> list[dict]:
        loop = asyncio.get_running_loop()
        responses: list[dict] = []

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                h = parse_ssdp_response(data)
                if h:
                    h["_addr"] = addr[0]
                    responses.append(h)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip or "0.0.0.0"))
        sock.bind((self.interface_ip or "0.0.0.0", 0))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
        try:
            dest = target or SSDP_ADDR
            msg = build_msearch(host=f"{dest[0]}:{dest[1]}")
            for _ in range(2):
                transport.sendto(msg, dest)
                await asyncio.sleep(0.3)
            await asyncio.sleep(max(timeout - 0.6, 0.2))
        finally:
            transport.close()

        seen_ips = set()
        by_loc = {}
        for h in responses:
            st = h.get("st", "") or h.get("nt", "")
            if "MediaRenderer" not in st:
                continue
            if not valid_location(h["location"], h["_addr"]):
                log.debug("ignoring SSDP LOCATION %s from %s", redact_url(h["location"]), h["_addr"])
                continue
            by_loc[h["location"]] = h
        for loc, h in by_loc.items():
            ip = h["_addr"]
            if ip in seen_ips:
                continue
            desc = await self._fetch_description(loc)
            if desc is None:
                continue
            seen_ips.add(ip)
            self.upsert_dlna(ip, loc, desc)
        if target is None:
            for ip, e in self._entries.items():
                if "dlna" in e.device.caps and ip not in seen_ips:
                    e.dlna_misses += 1
                    if e.dlna_misses >= SSDP_MISSES_OFFLINE:
                        e.dlna_alive = False
                        self._update_online(e)
        return list(by_loc.values())

    async def _fetch_description(self, location: str) -> Optional[dict]:
        try:
            assert self._session
            async with self._session.get(location, allow_redirects=False) as r:
                if r.status != 200:
                    return None
                if (r.content_length or 0) > MAX_DESCRIPTION_BYTES:
                    log.debug("description %s too large", redact_url(location))
                    return None
                body = b""
                while len(body) <= MAX_DESCRIPTION_BYTES:   # StreamReader.read(n) may return less than n
                    chunk = await r.content.read(MAX_DESCRIPTION_BYTES + 1 - len(body))
                    if not chunk:
                        break
                    body += chunk
                if len(body) > MAX_DESCRIPTION_BYTES:
                    log.debug("description %s too large", redact_url(location))
                    return None
                return parse_description(body)
        except Exception as e:
            log.debug("description %s failed: %s", redact_url(location), scrub_urls(str(e)))
            return None

    # ---------------------------------------------------------- manual
    async def add_manual(self, ip: str, _notify: bool = True) -> Optional[Device]:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return None
        if addr.version != 4 or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            return None
        ip = str(addr)
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4))
        found = False
        # Cast: eureka_info on :8008 (plain HTTP); some firmwares only serve :8443 (https, self-signed)
        for url in (f"http://{ip}:8008/setup/eureka_info?params=name,device_info",
                    f"https://{ip}:8443/setup/eureka_info?params=name,device_info"):
            try:
                async with self._session.get(url, ssl=False, allow_redirects=False) as r:
                    if r.status == 200:
                        data = json.loads(await r.text())
                        di = data.get("device_info") or {}
                        txt = {"id": (di.get("ssdp_udn") or data.get("ssdp_udn") or "").replace("-", ""),
                               "name": data.get("name", ""), "model": di.get("model_name", "")}
                        self.upsert_cast(ip, 8009, txt)
                        found = True
                        break
            except Exception as e:
                log.debug("eureka %s: %s", redact_url(url), scrub_urls(str(e)))
        if not found and await _tcp_open(ip, 8009):
            self.upsert_cast(ip, 8009, {})
            found = True
        # DLNA: unicast M-SEARCH, then well-known Samsung DMR location
        try:
            resp = await self._ssdp_scan(target=(ip, 1900), timeout=1.5)
        except OSError:
            resp = []
        if any(h["_addr"] == ip for h in resp):
            found = True
        else:
            for port in SAMSUNG_DMR_PORTS:
                loc = f"http://{ip}:{port}/dmr"
                desc = await self._fetch_description(loc)
                if desc is not None:
                    self.upsert_dlna(ip, loc, desc)
                    found = True
                    break
        e = self._entries.get(ip)
        if not found or e is None:
            return None
        e.manual = True
        if _notify:
            self._notify()
        return e.device


async def _tcp_open(ip: str, port: int, timeout: float = 1.5) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()
        return True
    except Exception:
        return False


def description_base(location: str) -> str:
    return urljoin(location, "/")
