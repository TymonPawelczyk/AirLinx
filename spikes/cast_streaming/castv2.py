"""Minimalny klient CASTv2 (TLS :8009, ramki 4B długości + protobuf CastMessage).

Używa tylko cast_channel_pb2 z pychromecast (sam protobuf), bez reszty biblioteki,
żeby mieć pełną kontrolę nad namespace'ami (urn:x-cast:com.google.cast.webrtc).
"""
from __future__ import annotations

import json
import logging
import queue
import socket
import ssl
import struct
import threading
import time

from pychromecast import cast_channel_pb2 as pb

log = logging.getLogger("castv2")

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_WEBRTC = "urn:x-cast:com.google.cast.webrtc"
NS_REMOTING = "urn:x-cast:com.google.cast.remoting"

MIRRORING_APP_ID = "0F5096E8"  # "Chrome Mirroring" (audio+video)


class CastChannel:
    def __init__(self, host: str, port: int = 8009, sender_id: str = "sender-airlinx"):
        self.host = host
        self.port = port
        self.sender_id = sender_id
        self._req = 1
        self._lock = threading.Lock()
        self._inbox: "queue.Queue[tuple[str, str, str, dict | None, str]]" = queue.Queue()
        self._listeners = []  # callables(ns, src, dst, payload_dict)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # TV ma self-signed cert (uwierzytelnienie urządzenia pomijamy)
        raw = socket.create_connection((host, port), timeout=10)
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(None)
        self._alive = True
        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx.start()
        self._hb = threading.Thread(target=self._hb_loop, daemon=True)
        self._hb.start()

    # --- low level -------------------------------------------------------
    def next_request_id(self) -> int:
        with self._lock:
            self._req += 1
            return self._req

    def send(self, namespace: str, payload: dict, dest: str = "receiver-0") -> None:
        msg = pb.CastMessage()
        msg.protocol_version = pb.CastMessage.CASTV2_1_0
        msg.source_id = self.sender_id
        msg.destination_id = dest
        msg.namespace = namespace
        msg.payload_type = pb.CastMessage.STRING
        msg.payload_utf8 = json.dumps(payload, separators=(",", ":"))
        data = msg.SerializeToString()
        if namespace != NS_HEARTBEAT:
            log.debug("TX %s -> %s %s", namespace, dest, msg.payload_utf8[:2000])
        with self._lock:
            self.sock.sendall(struct.pack(">I", len(data)) + data)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def _rx_loop(self) -> None:
        try:
            while self._alive:
                (ln,) = struct.unpack(">I", self._recv_exact(4))
                msg = pb.CastMessage()
                msg.ParseFromString(self._recv_exact(ln))
                payload = None
                if msg.payload_type == pb.CastMessage.STRING:
                    try:
                        payload = json.loads(msg.payload_utf8)
                    except ValueError:
                        payload = {"_raw": msg.payload_utf8}
                if msg.namespace == NS_HEARTBEAT:
                    if payload and payload.get("type") == "PING":
                        self.send(NS_HEARTBEAT, {"type": "PONG"}, msg.source_id)
                    continue
                log.debug("RX %s <- %s %s", msg.namespace, msg.source_id,
                          msg.payload_utf8[:4000] if msg.payload_type == pb.CastMessage.STRING else "<binary>")
                for cb in list(self._listeners):
                    try:
                        cb(msg.namespace, msg.source_id, msg.destination_id, payload)
                    except Exception:  # noqa: BLE001
                        log.exception("listener failed")
                self._inbox.put((msg.namespace, msg.source_id, msg.destination_id, payload,
                                 msg.payload_utf8))
        except Exception as e:  # noqa: BLE001
            if self._alive:
                log.warning("rx loop ended: %s", e)
            self._alive = False

    def _hb_loop(self) -> None:
        while self._alive:
            time.sleep(5)
            try:
                self.send(NS_HEARTBEAT, {"type": "PING"})
            except Exception:  # noqa: BLE001
                return

    def add_listener(self, cb) -> None:
        self._listeners.append(cb)

    def wait_for(self, pred, timeout: float = 10.0):
        """Czeka na wiadomość spełniającą pred(ns, src, payload). Pozostałe odrzuca."""
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("timeout waiting for message")
            try:
                ns, src, dst, payload, raw = self._inbox.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("timeout waiting for message") from None
            if pred(ns, src, payload):
                return ns, src, payload, raw

    # --- high level ------------------------------------------------------
    def connect(self, dest: str = "receiver-0") -> None:
        self.send(NS_CONNECTION, {"type": "CONNECT", "origin": {},
                                  "userAgent": "AirLinx-spike", "senderInfo": {
                                      "sdkType": 2, "version": "X11; Linux x86_64",
                                      "browserVersion": "131.0", "platform": 4,
                                      "connectionType": 1}}, dest)

    def get_status(self) -> dict:
        rid = self.next_request_id()
        self.send(NS_RECEIVER, {"type": "GET_STATUS", "requestId": rid})
        _, _, p, _ = self.wait_for(lambda ns, s, p: ns == NS_RECEIVER and p and p.get("requestId") == rid)
        return p

    def launch(self, app_id: str, timeout: float = 20.0) -> dict:
        rid = self.next_request_id()
        self.send(NS_RECEIVER, {"type": "LAUNCH", "requestId": rid, "appId": app_id,
                                "language": "pl-PL", "supportedAppTypes": ["WEB"]})
        deadline = time.time() + timeout
        while True:
            _, _, p, _ = self.wait_for(lambda ns, s, p: ns == NS_RECEIVER and p is not None,
                                       timeout=max(0.1, deadline - time.time()))
            if p.get("type") == "LAUNCH_ERROR":
                raise RuntimeError(f"LAUNCH_ERROR: {p}")
            status = p.get("status")
            if not isinstance(status, dict):  # np. LAUNCH_STATUS {"status":"USER_ALLOWED"}
                log.info("launch progress: %s", p)
                continue
            apps = status.get("applications") or []
            for a in apps:
                if a.get("appId") == app_id and a.get("transportId"):
                    return a

    def stop(self, session_id: str) -> None:
        rid = self.next_request_id()
        self.send(NS_RECEIVER, {"type": "STOP", "requestId": rid, "sessionId": session_id})

    def close(self) -> None:
        self._alive = False
        try:
            self.send(NS_CONNECTION, {"type": "CLOSE"})
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
