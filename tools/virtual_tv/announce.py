"""mDNS (``_googlecast._tcp``) and SSDP (MediaRenderer) announcements of the virtual TV.

SAFETY: announcements go to multicast groups, i.e. to every device on the link. They are therefore only
ever sent from a **network namespace that is isolated from the LAN** (see ``netns.py``): the guard
:func:`assert_isolated` refuses to continue when this process can see any interface other than loopback
and ``vtv*`` links created by ``netns.py``. There is deliberately no flag to switch the guard off.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
from typing import Optional

log = logging.getLogger("vtv.announce")

SSDP_GROUP, SSDP_PORT = "239.255.255.250", 1900
ALLOWED_PREFIXES = ("lo", "vtv")


class NotIsolatedError(RuntimeError):
    pass


def visible_interfaces() -> list[str]:
    """Interface names of THIS network namespace. (Not /sys/class/net: sysfs keeps showing the namespace it
    was mounted in, so inside ``unshare -n`` it would still list the host's interfaces.)"""
    try:
        return sorted(name for _, name in socket.if_nameindex())
    except OSError:
        return []


def assert_isolated() -> None:
    """Raise unless every interface in this network namespace is loopback or a ``vtv*`` link."""
    foreign = [i for i in visible_interfaces() if not i.startswith(ALLOWED_PREFIXES)]
    if foreign or not visible_interfaces():
        raise NotIsolatedError(
            "refusing to announce: this network namespace can see the real network "
            f"(interfaces {foreign or 'unknown'}). Run inside `tools/virtual_tv/netns.sh` "
            "(an isolated namespace with its own veth link), see docs/VIRTUAL_TV.md.")


class MdnsAnnouncer:
    """Registers ``<name>._googlecast._tcp.local.`` with the TXT record of a Chromecast-built-in TV."""

    def __init__(self, *, ip: str, port: int, device_id: str, name: str, model: str) -> None:
        self.ip, self.port, self.device_id, self.name, self.model = ip, port, device_id, name, model
        self._azc = None
        self._info = None

    async def start(self) -> None:
        assert_isolated()
        from zeroconf import IPVersion, ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        txt = {"id": self.device_id, "cd": self.device_id.upper(), "rm": "", "ve": "05", "md": self.model,
               "ic": "/setup/icon.png", "fn": self.name, "ca": "201221", "st": "0", "bs": "FA8FCA000000",
               "nf": "1", "rs": ""}
        self._info = ServiceInfo("_googlecast._tcp.local.", f"VirtualTV-{self.device_id}._googlecast._tcp.local.",
                                 addresses=[socket.inet_aton(self.ip)], port=self.port,
                                 properties={k: v.encode() for k, v in txt.items()},
                                 server=f"vtv-{self.device_id[:8]}.local.")
        self._azc = AsyncZeroconf(interfaces=[self.ip], ip_version=IPVersion.V4Only)
        await self._azc.async_register_service(self._info)
        log.info("mDNS: %s on %s:%d", self._info.name, self.ip, self.port)

    async def stop(self) -> None:
        if self._azc:
            with contextlib.suppress(Exception):
                if self._info:
                    await self._azc.async_unregister_service(self._info)
            with contextlib.suppress(Exception):
                await self._azc.async_close()
            self._azc = None


class SsdpResponder:
    """Answers M-SEARCH for MediaRenderer (and ssdp:all / rootdevice / our UUID), and sends alive NOTIFYs."""

    ST_MATCH = ("ssdp:all", "upnp:rootdevice", "urn:schemas-upnp-org:device:MediaRenderer:1",
                "urn:schemas-upnp-org:service:AVTransport:1", "urn:schemas-upnp-org:service:RenderingControl:1",
                "urn:schemas-upnp-org:service:ConnectionManager:1")

    def __init__(self, *, ip: str, location: str, udn: str) -> None:
        self.ip, self.location, self.udn = ip, location, udn
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.searches: list[tuple[str, str]] = []
        self._notify_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        assert_isolated()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", SSDP_PORT))
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_GROUP), socket.inet_aton(self.ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
        me = self

        class P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):  # noqa: ANN001
                me._on_datagram(data, addr)

        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(P, sock=sock)
        self._notify_task = asyncio.create_task(self._notify_loop())

    def _headers(self, st: str) -> str:
        usn = f"{self.udn}::{st}" if not st.startswith("uuid:") else st
        return ("CACHE-CONTROL: max-age=1800\r\nEXT:\r\nSERVER: Linux/5.0 UPnP/1.0 VirtualTV/1.0\r\n"
                f"LOCATION: {self.location}\r\nST: {st}\r\nUSN: {usn}\r\n")

    def _on_datagram(self, data: bytes, addr) -> None:  # noqa: ANN001
        text = data.decode("utf-8", "replace")
        if not text.startswith("M-SEARCH"):
            return
        st = ""
        for ln in text.split("\r\n"):
            if ln.lower().startswith("st:"):
                st = ln.split(":", 1)[1].strip()
        self.searches.append((addr[0], st))
        if st not in self.ST_MATCH and st != self.udn:
            return
        reply = "HTTP/1.1 200 OK\r\n" + self._headers(st if st != "ssdp:all" else self.ST_MATCH[2]) + "\r\n"
        if self.transport:
            self.transport.sendto(reply.encode(), addr)

    async def _notify_loop(self) -> None:
        while True:
            for nt in self.ST_MATCH[1:3]:
                msg = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n" + self._headers(nt).replace("ST:", "NT:")
                       + "NTS: ssdp:alive\r\n\r\n")
                if self.transport:
                    self.transport.sendto(msg.encode(), (SSDP_GROUP, SSDP_PORT))
            await asyncio.sleep(30)

    async def stop(self) -> None:
        if self._notify_task:
            self._notify_task.cancel()
        if self.transport:
            self.transport.close()
