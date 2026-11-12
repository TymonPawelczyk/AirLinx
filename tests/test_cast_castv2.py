"""Offline tests for the async CASTv2 channel: fake receivers (plain TCP and TLS), device-auth
(real openscreen test vectors + a synthetic Cast PKI) and receiver identity pinning."""
import asyncio
import base64
import datetime
import json
import os
import ssl
import stat
import struct

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from airlinx.core.cast import castv2 as cv

UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def isolated_pins(tmp_path, monkeypatch):
    path = tmp_path / "cfg" / "known_tvs.json"
    monkeypatch.setenv("AIRLINX_KNOWN_TVS", str(path))
    monkeypatch.setattr(cv, "DEVICE_AUTH_TIMEOUT", 0.3)
    return path


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_cert(cn, key, issuer_cn=None, issuer_key=None, *, ca=False, pathlen=None, days=2, start_offset=-1.0):
    """start_offset/days in days relative to now."""
    now = datetime.datetime.now(UTC)
    b = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(issuer_cn or cn))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now + datetime.timedelta(days=start_offset))
         .not_valid_after(now + datetime.timedelta(days=start_offset + days)))
    if ca:
        b = b.add_extension(x509.BasicConstraints(ca=True, path_length=pathlen), critical=True)
    return b.sign(issuer_key or key, hashes.SHA256())


def der(c):
    return c.public_bytes(serialization.Encoding.DER)


def ctx_for(tmp_path, cert, key, name):
    cp, kp = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    return ctx


def server_ctx(tmp_path, name, days=30, start_offset=-1.0):
    """Self-signed server TLS context (like a Cast TV) -> (ctx, der)."""
    key = ec.generate_private_key(ec.SECP256R1())
    cert = make_cert(name, key, days=days, start_offset=start_offset)
    return ctx_for(tmp_path, cert, key, name), der(cert)


class FakeCastPki:
    """Synthetic Cast PKI: root -> ICA -> device certificate."""

    def __init__(self, device_cn="TEST-TV FA8F00000001"):
        self.root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.root = make_cert("Test Cast Root CA", self.root_key, ca=True, pathlen=1, days=3650)
        ica_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ica = make_cert("Test Cast ICA", ica_key, "Test Cast Root CA", self.root_key, ca=True, pathlen=0,
                             days=3650)
        self.ica_key = ica_key
        self.device(device_cn)

    def device(self, cn):
        self.dev_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.dev = make_cert(cn, self.dev_key, "Test Cast ICA", self.ica_key, days=3650)
        return self

    def answer(self, challenge: bytes, tls_der: bytes, *, tamper=False) -> bytes:
        ch = cv._pb_fields(cv._pb_fields(challenge)[1][0])
        nonce = bytes(ch[2][0])
        sig = self.dev_key.sign(nonce + tls_der, padding.PKCS1v15(), hashes.SHA256())
        if tamper:
            sig = bytes([sig[0] ^ 1]) + sig[1:]
        return cv.build_auth_response(signature=sig, client_cert=der(self.dev), intermediates=[der(self.ica)],
                                      nonce=nonce)


def test_protobuf_roundtrip():
    m = cv.CastMessage("sender-x", "receiver-0", cv.NS_RECEIVER, payload_utf8='{"type":"PING","ż":1}')
    d = cv.decode_message(cv.encode_message(m))
    assert (d.source_id, d.destination_id, d.namespace, d.payload_utf8) == (
        "sender-x", "receiver-0", cv.NS_RECEIVER, '{"type":"PING","ż":1}')
    b = cv.CastMessage("a", "b", "ns", payload_binary=b"\x00\x01")
    assert cv.decode_message(cv.encode_message(b)).payload_binary == b"\x00\x01"
    # known-good bytes produced by the official protobuf (pychromecast cast_channel_pb2)
    ref = bytes.fromhex("08001201611a016222026e7328003207"
                        "7b2261223a317d")
    assert cv.encode_message(cv.CastMessage("a", "b", "ns", payload_utf8='{"a":1}')) == ref


class FakeReceiver:
    """Speaks just enough CASTv2 for the tests."""

    def __init__(self, apps=None, ssl_ctx=None, auth=None, tls_der=None):
        self.ssl_ctx = ssl_ctx
        self.auth = auth          # FakeCastPki answering device-auth (None: stay silent like old receivers)
        self.tls_der = tls_der
        self.tamper = False
        self.apps = apps or []
        self.received = []
        self.writers = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, ssl=self.ssl_ctx)
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    def send(self, w, ns, payload, src="receiver-0", dst="sender-airlinx"):
        m = cv.CastMessage(src, dst, ns, payload_utf8=json.dumps(payload))
        w.write(cv.frame_message(m))

    def status(self, rid=0):
        return {"type": "RECEIVER_STATUS", "requestId": rid, "status": {"applications": self.apps}}

    async def _client(self, r, w):
        self.writers.append(w)
        try:
            while True:
                (ln,) = struct.unpack(">I", await r.readexactly(4))
                m = cv.decode_message(await r.readexactly(ln))
                if m.payload_binary is not None:
                    self.received.append((m.namespace, m.destination_id, {"type": "_BINARY"}))
                    if m.namespace == cv.NS_DEVICEAUTH and self.auth is not None:
                        out = cv.CastMessage("receiver-0", m.source_id, cv.NS_DEVICEAUTH,
                                             payload_binary=self.auth.answer(m.payload_binary, self.tls_der,
                                                                             tamper=self.tamper))
                        w.write(cv.frame_message(out))
                        await w.drain()
                    continue
                p = json.loads(m.payload_utf8)
                self.received.append((m.namespace, m.destination_id, p))
                await self.handle(w, m, p)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def handle(self, w, m, p):
        t = p.get("type")
        if m.namespace == cv.NS_RECEIVER and t == "GET_STATUS":
            self.send(w, cv.NS_RECEIVER, self.status(p["requestId"]))
        elif m.namespace == cv.NS_RECEIVER and t == "LAUNCH":
            self.send(w, cv.NS_RECEIVER, {"type": "LAUNCH_STATUS", "requestId": p["requestId"],
                                          "status": "USER_ALLOWED"})
            await asyncio.sleep(0.05)
            self.apps = [{"appId": p["appId"], "displayName": "Chrome Mirroring", "sessionId": "S1",
                          "transportId": "T1", "namespaces": [{"name": cv.NS_WEBRTC}]}]
            self.send(w, cv.NS_RECEIVER, self.status(p["requestId"]))
        elif m.namespace == cv.NS_RECEIVER and t == "STOP":
            self.apps = []
            self.send(w, cv.NS_RECEIVER, self.status(p["requestId"]))
        await w.drain()


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 10))


def test_status_launch_stop_and_correlation():
    async def main():
        fr = FakeReceiver(apps=[{"appId": "E8C28D3C", "displayName": "Backdrop", "isIdleScreen": True}])
        port = await fr.start()
        ch = await cv.CastChannel.open("127.0.0.1", port, use_tls=False)
        try:
            assert await ch.get_running_app() is None  # idle screen ignored
            progress = []
            app = await ch.launch(cv.MIRRORING_APP_ID, on_progress=progress.append)
            assert app["transportId"] == "T1" and progress == ["USER_ALLOWED"]
            assert (await ch.get_running_app())["sessionId"] == "S1"
            await ch.connect_virtual("T1")
            await ch.stop_app("S1")
            assert await ch.get_running_app() is None
        finally:
            await ch.close()
            await fr.close()
        kinds = [(ns, p["type"]) for ns, _, p in fr.received]
        assert kinds[0] == (cv.NS_CONNECTION, "CONNECT")
        assert (cv.NS_RECEIVER, "LAUNCH") in kinds and (cv.NS_RECEIVER, "STOP") in kinds
        assert any(d == "T1" and p["type"] == "CONNECT" for _, d, p in fr.received)
        assert kinds[-1] == (cv.NS_CONNECTION, "CLOSE")
    run(main())


def test_ping_pong_and_peer_close_detected():
    async def main():
        fr = FakeReceiver()
        port = await fr.start()
        ch = await cv.CastChannel.open("127.0.0.1", port, use_tls=False, heartbeat=False)
        await asyncio.sleep(0.05)
        w = fr.writers[0]
        fr.send(w, cv.NS_HEARTBEAT, {"type": "PING"})
        await w.drain()
        await asyncio.sleep(0.1)
        assert (cv.NS_HEARTBEAT, "receiver-0", {"type": "PONG"}) in fr.received
        w.close()
        await asyncio.wait_for(ch.closed.wait(), 2)
        with pytest.raises(cv.ChannelClosed):
            await ch.send(cv.NS_RECEIVER, {"type": "GET_STATUS"})
        await ch.close()
        await fr.close()
    run(main())


def test_request_timeout_and_launch_error():
    async def main():
        fr = FakeReceiver()

        async def handle(w, m, p):
            if p.get("type") == "LAUNCH":
                fr.send(w, cv.NS_RECEIVER, {"type": "LAUNCH_ERROR", "requestId": p["requestId"],
                                            "reason": "NOT_FOUND"})
                await w.drain()
        fr.handle = handle
        port = await fr.start()
        ch = await cv.CastChannel.open("127.0.0.1", port, use_tls=False, heartbeat=False)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await ch.request(cv.NS_RECEIVER, {"type": "GET_STATUS"}, timeout=0.2)
            with pytest.raises(cv.CastError, match="NOT_FOUND"):
                await ch.launch("BAD")
        finally:
            await ch.close()
            await fr.close()
    run(main())


VIZIO_CHAIN_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDsTCCApmgAwIBAgICA+UwDQYJKoZIhvcNAQELBQAwfDELMAkGA1UEBhMCVVMxEzARBgNVBAg
MCkNhbGlmb3JuaWExFjAUBgNVBAcMDU1vdW50YWluIFZpZXcxEzARBgNVBAoMCkdvb2dsZSBJbm
MxDTALBgNVBAsMBENhc3QxHDAaBgNVBAMME0Nhc3QgVFYgSUNBIChWaXppbykwHhcNMTUxMTExM
DIwNjE5WhcNMTYxMTEwMDIwNjE5WjCBiTETMBEGA1UECAwKQ2FsaWZvcm5pYTELMAkGA1UEBhMC
VVMxFjAUBgNVBAcMDU1vdW50YWluIFZpZXcxGDAWBgNVBAsMD0Nhc3QgVFYgKFZpemlvKTETMBE
GA1UECgwKR29vZ2xlIEluYzEeMBwGA1UEAwwVOVYwMDAwVkIgRkE4RkNBNzg0RDAxMIIBIjANBg
kqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqUupyRqYnnGwIAkdu3x86d0pPbVDTtgUsz/RDtHOT
P1esktbGar39bnn97OxM6dr0mJnsmKAUSGe21zSFuWktOeD4kMdztVqskpt6rh6nRuoHz8ZteCj
2xK+0hniZs7QyQNtq5LbbCwkZDveRN5avXKmeJTJQMrZb1ywt4v4UUDoWc1SmQy+VozGBe1Muyb
Y2gThF+mljT3Ru8RVtI+Yd1MSm42uEimPBb+akNEiIAkPTxyf3pqYs3QpZOv6l5GSwLNCdTGtpV
uNb98n29SXUiP6jlnAIWMPyxPRWn/A4hD7BZHUscKL+OQmv8iYVQADUeW83d9+dOO1yHsQb5wIh
QIDAQABoy8wLTAJBgNVHRMEAjAAMAsGA1UdDwQEAwIHgDATBgNVHSUEDDAKBggrBgEFBQcDAjAN
BgkqhkiG9w0BAQsFAAOCAQEAYUryUCBoi0UNgWok+7uhmus2TNth/aafAxvAYpXI/LZ2/T1Dcf1
93bj6aAKN6HZ181hzGneJJC+R0xhUoHWAkaW4WR+8XwmHvg5ig1LwRVYIvf/jLpb5IxjvuD6pJ7
LuHUA2+cq72fpPH9loQUhXz5pvJaFqaHlUy5RoX4zDfecUGCdfX2WoTUlJu7i81kMT6e9WdMmp8
GikAnFDRpjR7+Fk751/iiveGeJ5X6g4dY4LhfgU2ISsh1c9UvphCvad1rKamnNH+JlJYEyxfB7o
0/NPDmIQX6ZplBMIeJsG7VdS2rd4rNoIJi80ThUIwAOWv6bOXGMivtDomZS6Qva5Vw==
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIDzDCCArSgAwIBAgIBMTANBgkqhkiG9w0BAQsFADB1MQswCQYDVQQGEwJVUzETMBEGA1UECAw
KQ2FsaWZvcm5pYTEWMBQGA1UEBwwNTW91bnRhaW4gVmlldzETMBEGA1UECgwKR29vZ2xlIEluYz
ENMAsGA1UECwwEQ2FzdDEVMBMGA1UEAwwMQ2FzdCBSb290IENBMB4XDTE1MDQyMDIxMDMzOFoXD
TI1MDQxNzIxMDMzOFowfDELMAkGA1UEBhMCVVMxEzARBgNVBAgMCkNhbGlmb3JuaWExFjAUBgNV
BAcMDU1vdW50YWluIFZpZXcxEzARBgNVBAoMCkdvb2dsZSBJbmMxDTALBgNVBAsMBENhc3QxHDA
aBgNVBAMME0Nhc3QgVFYgSUNBIChWaXppbykwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAo
IBAQDUAvw2108W2+KVjimaK11HjswyeHqy8kasvzWtwsp6Pr5QS1ABd5CjojFRMdZ6MitDvU75t
jkrBfKXvd67Gbxdcxd9lB6bTOhoN37WNuWg+3VSeINMJoSISFTYc/z390o/aKaje0aWAzhe8yFN
WZFIZgrq7WmNsoZTr0HoysBVY338cpi/svSruyzzPJIJwwH2aI52wXygOw32pNWSsePdnE/oBNK
bWXBy3aBgU+7HNsZIRbfZGzvr637FB7Kdfc/xa64CJd6vhDCMmNnhbY3v9gpjv77NQgn1r+vxzN
mi54YyCwpTZiMvUeaEZMa3D5ivJHUzXePYlmHXV6ZjiKJPAgMBAAGjYDBeMA8GA1UdEwQIMAYBA
f8CAQAwHQYDVR0OBBYEFARx4BQcRXXbxd8+EWpXeZ3QNxIYMB8GA1UdIwQYMBaAFHyaHn3feVS8
18xeypmGRXlldCgZMAsGA1UdDwQEAwIBBjANBgkqhkiG9w0BAQsFAAOCAQEAhjZv3uyPSry33py
9ngM/VxFiiEbuz20hWEMx369GNbiJNig+88SVirKRZv5/P2RagmOBiUoPNFyyvAqA0iZ0tGm+Ob
1hrejoP8Bbk9CRWUrs8qY27INMujdA776EcrkTeKrE0rws5RqQ2Qfbyqot9Mytw0dO/0RqnyXVu
iDhrpvfJIylhX3THJMibUmJ663uhqY7aDSKJKUaEUD0okEWPxEeVibuTjBbnXF+klj80dOOp+Ar
JB/DvrhkbmEFiBj67r6x4GAmv8toYNhqx9cgtISfwOsK+l478FInEzkU5KgS6vhLGV5S5oZqvoN
8PTEmZtf7oUE82PulKEn5PysFqw==
-----END CERTIFICATE-----
"""

GEN1_CHAIN_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDrDCCApSgAwIBAgIEU8xPLDANBgkqhkiG9w0BAQUFADB9MQswCQYDVQQGEwJVUzETMBEGA1U
ECAwKQ2FsaWZvcm5pYTEWMBQGA1UEBwwNTW91bnRhaW4gVmlldzETMBEGA1UECgwKR29vZ2xlIE
luYzESMBAGA1UECwwJR29vZ2xlIFRWMRgwFgYDVQQDDA9FdXJla2EgR2VuMSBJQ0EwHhcNMTQwN
zIwMjMyMjIwWhcNMzQwNzE1MjMyMjIwWjCBgTELMAkGA1UEBhMCVVMxEzARBgNVBAgTCkNhbGlm
b3JuaWExEzARBgNVBAoTCkdvb2dsZSBJbmMxFjAUBgNVBAcTDU1vdW50YWluIFZpZXcxEjAQBgN
VBAsTCUdvb2dsZSBUVjEcMBoGA1UEAxMTMlpaQkc5IEZBOEZDQTNFRjkxQTCCASIwDQYJKoZIhv
cNAQEBBQADggEPADCCAQoCggEBAKV56Srec2ePlqDP6cqFPuwU4MOs7MOcGDrvda6qy6tWC7Bms
qipMA/hn77iUiBZsw3TbUQnVfmM4ZQ2RENzcrAJ68cmc+lPxmRr8x1Xu5FzZ+kcyU8glLLqdiXY
EKRboFhC7BM05O1XOLvzCls4zuZuMrGNFBW+YoBmFiXFYWBhapZC3RhhlSEZFuQWbb/MUSDzwr/
CRbn4tKHMv4Fkw5HAnhLa+yXfgCGwqOd9GejqUKsO/aajAHkM7lIHmvkthI4MVk0Koc+Ih487pg
sOt18LqubZVEkbjCqpRpx1CGbErWnw2ptPvMCEC6e7mrYHcYgmuzQ7m+eUlhthEUiTYC0CAwEAA
aMvMC0wCQYDVR0TBAIwADALBgNVHQ8EBAMCB4AwEwYDVR0lBAwwCgYIKwYBBQUHAwIwDQYJKoZI
hvcNAQEFBQADggEBAGuKgGXHJXQ1M7P4uXB8wPPuT2h6g29YJ62rUvZ7BrlWTknJT0Owaw68zep
LhFQ4ydIzbVV3hA2InCmP3U24ZMxMJcA/9qNPAqPrtE1ZIQNIQh6slAdZa0qM6Us30/5fpUL6lg
AfD1RIJxA4RWYZKP78SjJz1Lybx3Zbt0Jist9GtvaJGZjZrdPncnJKayGaIln8gzHd6MVEGZp7a
IQZ2h4NDlnrwyhMFTjg1WvnmQJ63bEvjSyjMGhY0JOUaDp/UMxnExn+1+cYAW9LrosZXtRDNJTl
1zX4auAnNMHkt8uCF8Jhy80X2wU0fj85oYbRsm+jBMtRayznY1TR0WoPBAo=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIDhzCCAm+gAwIBAgIBATANBgkqhkiG9w0BAQUFADB8MQswCQYDVQQGEwJVUzETMBEGA1UECAw
KQ2FsaWZvcm5pYTEWMBQGA1UEBwwNTW91bnRhaW4gVmlldzETMBEGA1UECgwKR29vZ2xlIEluYz
ESMBAGA1UECwwJR29vZ2xlIFRWMRcwFQYDVQQDDA5FdXJla2EgUm9vdCBDQTAeFw0xMjEyMTkwM
DQ3MTJaFw0zMjEyMTQwMDQ3MTJaMH0xCzAJBgNVBAYTAlVTMRMwEQYDVQQIDApDYWxpZm9ybmlh
MRYwFAYDVQQHDA1Nb3VudGFpbiBWaWV3MRMwEQYDVQQKDApHb29nbGUgSW5jMRIwEAYDVQQLDAl
Hb29nbGUgVFYxGDAWBgNVBAMMD0V1cmVrYSBHZW4xIElDQTCCASIwDQYJKoZIhvcNAQEBBQADgg
EPADCCAQoCggEBALwigL2A9johADuudl41fz3DZFxVlIY0LwWHKM33aYwXs1CnuIL638dDLdZ+q
6BvtxNygKRHFcEgmVDN7BRiCVukmM3SQbY2Tv/oLjIwSoGoQqNsmzNuyrL1U2bgJ1OGGoUepzk/
SneO+1RmZvtYVMBeOcf1UAYL4IrUzuFqVR+LFwDmaaMn5gglaTwSnY0FLNYuojHetFJQ1iBJ3nG
g+a0gQBLx3SXr1ea4NvTWj3/KQ9zXEFvmP1GKhbPz//YDLcsjT5ytGOeTBYysUpr3TOmZer5ufk
0K48YcqZP6OqWRXRy9ZuvMYNyGdMrP+JIcmH1X+mFHnquAt+RIgCqSxRsCAwEAAaMTMBEwDwYDV
R0TBAgwBgEB/wIBATANBgkqhkiG9w0BAQUFAAOCAQEAi9Shsc9dzXtsSEpBH1MvGC0yRf+eq9Nz
Ph8i1+r6AeZzAw8rxiW7pe7F9UXLJBIqrcJdBfR69cKbEBZa0QpzxRY5oBDK0WiFnvueJoOOWPN
3oE7l25e+LQBf9ZTbsZ1la/3w0QRR38ySppktcfVN1SP+MxyptKvFvxq40YDvicniH5xMSDui+g
IK3IQBiocC+1nup0wEfXSZh2olRK0WquxONRt8e4TJsT/hgnDlDefZbfqVtsXkHugRm9iy86T9E
/ODT/cHFCC7IqWmj9a126l0eOKTDeUjLwUX4LKXZzRND5x2Q3umIUpWBfYqfPJ/EpSCJikH8Ats
bHkUsHTVbA==
-----END CERTIFICATE-----
"""

GEN1_SIG_SHA1_B64 = "Ctq1QFyOU4naZ0coq2QN7Lgf1nUol1/gEVE1KnDY9k3o0C7geXU6Jb9AD23RIOOCvQWHVwEedrf017MQSmyK+T3n62LpX3OrbiL1WU3Eo5XDvnsEWjZn7nGy6GC+qiyQNtfwQijUKZ8wqhBPKuFyZ8y1RHt/iUWfw51q8Hh3bZ8TWDUJjHGvNEsYxwfS8gNI4kB1O+szdI0ztEXiWVaLx05gx+zI0zIWILDHDRRLaL95rX5HXV21jLbDJ7nYJXDAjRImUeit3vjoPkfQ3xF9NFCoiYlZk4o9iK/VHug0LphiOcEiBvc+mP1vOkXQtzrlqjg1LOl4ceLwb2CVwGBfww=="

GEN1_SIG_SHA256_B64 = "KHPkhPyRNRgMlbl2huwO0N49CRgS820hnooUYJxxb9KGv0Z5Sz+Pw8QeuQa5PGoldmnTx3t10uDsvlEvWpKDttNv9AzitLbbCeU+eHLjpFJM9A0o/cGkygnq+3uQHgAsyYq3X6gypY+cNRK32otoCGqKJDcqLbHlBsal+KyusUswaJXpZoZpVX+ism/hUzq2MOETZdtvHorqeYjPMW0/chau4rFXINQRaK20mXJUgIuyxevMfyPyrw7+71Zxbgr3CP02JC02Ii7wgcT21MCExfmbGTMYePAAWO/9R1dsZPhtpkDCUN63lA5GI3J9EHkQaw8ld5ogd0gZ1VE/7Nw7rA=="


def _json_kinds(fr):
    return [p["type"] for _, _, p in fr.received if p["type"] != "_BINARY"]


def test_real_cast_device_chains_verify_to_embedded_roots():
    t = datetime.datetime(2016, 1, 1, tzinfo=UTC).timestamp()
    vizio = [der(c) for c in x509.load_pem_x509_certificates(VIZIO_CHAIN_PEM.encode())]
    path = cv.verify_device_chain(vizio[0], vizio[1:], t)
    assert path[-1].subject.rfc4514_string().startswith("CN=Cast Root CA")
    gen1 = [der(c) for c in x509.load_pem_x509_certificates(GEN1_CHAIN_PEM.encode())]
    path = cv.verify_device_chain(gen1[0], gen1[1:], t)   # SHA-1 signed Eureka chain
    assert path[-1].subject.rfc4514_string().startswith("CN=Eureka Root CA")
    for h, b in ((cv.HASH_SHA1, GEN1_SIG_SHA1_B64), (cv.HASH_SHA256, GEN1_SIG_SHA256_B64)):
        cv.verify_signed_data(path[0], b"STRING", base64.b64decode(b), h)
    with pytest.raises(cv.DeviceAuthFailed):
        cv.verify_signed_data(path[0], b"STRINH", base64.b64decode(GEN1_SIG_SHA256_B64), cv.HASH_SHA256)
    with pytest.raises(cv.DeviceAuthFailed, match="chain"):
        cv.verify_device_chain(vizio[0], [], t)                    # missing ICA
    with pytest.raises(cv.DeviceAuthFailed, match="validity"):
        cv.verify_device_chain(vizio[0], vizio[1:], datetime.datetime(2026, 1, 1, tzinfo=UTC).timestamp())


def test_auth_response_verification_rules():
    pki = FakeCastPki()
    tls_key = ec.generate_private_key(ec.SECP256R1())
    tls = der(make_cert("tls", tls_key, days=2))
    nonce = os.urandom(16)
    ok = cv.parse_auth_response(pki.answer(cv.build_auth_challenge(nonce), tls))
    ident = cv.verify_auth_response(ok, tls, nonce, roots=[pki.root])
    assert ident.subject == "CN=TEST-TV FA8F00000001" and not ident.audio_only
    with pytest.raises(cv.DeviceAuthFailed, match="nonce"):
        cv.verify_auth_response(ok, tls, os.urandom(16), roots=[pki.root])
    with pytest.raises(cv.DeviceAuthFailed, match="does not verify"):   # signature bound to the TLS cert
        cv.verify_auth_response(ok, der(make_cert("other", tls_key, days=2)), nonce, roots=[pki.root])
    with pytest.raises(cv.DeviceAuthFailed, match="chain"):
        cv.verify_auth_response(ok, tls, nonce)                       # not a Google root
    long_tls = der(make_cert("tls", tls_key, days=30))
    resp = cv.parse_auth_response(pki.answer(cv.build_auth_challenge(nonce), long_tls))
    with pytest.raises(cv.DeviceAuthFailed, match="lifetime"):
        cv.verify_auth_response(resp, long_tls, nonce, roots=[pki.root])
    err = cv._pb_ld(3, cv._varint(1 << 3) + cv._varint(1))
    with pytest.raises(cv.DeviceAuthUnavailable):
        cv.parse_auth_response(err)


def test_device_auth_end_to_end_pins_device_not_tls(tmp_path, isolated_pins, monkeypatch):
    pki = FakeCastPki()
    monkeypatch.setattr(cv, "cast_trust_roots", lambda: [pki.root])

    def tv(name, days=2, start=-0.5):
        key = ec.generate_private_key(ec.SECP256R1())
        c = make_cert(name, key, days=days, start_offset=start)
        return FakeReceiver(ssl_ctx=ctx_for(tmp_path, c, key, name), auth=pki, tls_der=der(c))

    async def main():
        a = tv("a")
        pa = await a.start()
        ch = await cv.CastChannel.open("127.0.0.1", pa, heartbeat=False, pin_key="tv-1")
        assert ch.identity.subject == "CN=TEST-TV FA8F00000001" and ch.auth_result == "pinned"
        await ch.close()
        pin = json.loads(isolated_pins.read_text())["tv-1"]
        assert pin["auth"] == "device" and pin["device_subject"] == "CN=TEST-TV FA8F00000001"
        assert stat.S_IMODE(os.stat(isolated_pins).st_mode) == 0o600
        # TLS certificate rotated (even an *older* notBefore): same device identity -> accepted
        b = tv("b", start=-1.5)
        pb_ = await b.start()
        ch = await cv.CastChannel.open("127.0.0.1", pb_, heartbeat=False, pin_key="tv-1")
        assert ch.auth_result == "match"
        await ch.close()
        # a different genuine Cast device under the same id -> changed identity
        pki.device("OTHER-TV FA8F00000002")
        c = tv("c")
        pc = await c.start()
        with pytest.raises(cv.TvCertChangedError):
            await cv.CastChannel.open("127.0.0.1", pc, heartbeat=False, pin_key="tv-1")
        assert _json_kinds(c) == []                        # nothing but the challenge was sent
        # forged signature -> auth failure
        d = tv("d")
        d.tamper = True
        pd = await d.start()
        with pytest.raises(cv.TvAuthFailedError):
            await cv.CastChannel.open("127.0.0.1", pd, heartbeat=False, pin_key="tv-2")
        assert _json_kinds(d) == []
        # receiver that authenticated before but now stays silent -> suspicious
        e = tv("e")
        e.auth = None
        pe = await e.start()
        with pytest.raises(cv.TvCertChangedError):
            await cv.CastChannel.open("127.0.0.1", pe, heartbeat=False, pin_key="tv-1")
        assert cv.forget_tv_cert("tv-1") and not cv.forget_tv_cert("tv-1")
        for f in (a, b, c, d, e):
            await f.close()
    run(main())


def test_tls_fallback_pinning_tolerates_rotation_only(tmp_path, isolated_pins):
    """Receivers without device-auth: TOFU on the TLS cert, re-pinned on a plausible rotation."""
    async def main():
        ctx1, der1 = server_ctx(tmp_path, "r1", days=2, start_offset=-1.0)
        ctx2, der2 = server_ctx(tmp_path, "r2", days=2, start_offset=-0.5)   # later notBefore, short
        ctx3, _ = server_ctx(tmp_path, "r3", days=2, start_offset=-1.5)      # older notBefore
        ctx4, _ = server_ctx(tmp_path, "r4", days=30, start_offset=-0.1)     # long-lived
        tvs = [FakeReceiver(ssl_ctx=c) for c in (ctx1, ctx2, ctx3, ctx4)]
        ports = [await t.start() for t in tvs]

        async def connect(i):
            ch = await cv.CastChannel.open("127.0.0.1", ports[i], heartbeat=False, pin_key="tv")
            res = ch.auth_result
            await ch.close()
            return res
        assert await connect(0) == "pinned"
        assert json.loads(isolated_pins.read_text())["tv"]["tls"]["sha256"] == cv.cert_fingerprint(der1)
        assert await connect(0) == "match"
        assert await connect(1) == "rotated"
        assert json.loads(isolated_pins.read_text())["tv"]["tls"]["sha256"] == cv.cert_fingerprint(der2)
        for i in (2, 3):
            with pytest.raises(cv.TvCertChangedError) as ei:
                await connect(i)
            assert ei.value.code == "tv_cert_changed" and "Certyfikat" in ei.value.message_pl
            assert _json_kinds(tvs[i]) == []                 # no CONNECT, no OFFER
        assert cv.forget_tv_cert("tv")
        assert await connect(3) == "pinned"
        for t in tvs:
            await t.close()
    run(main())


def test_pin_store_corrupt_file_and_legacy_entries(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text("{not json")
    assert cv.check_cert_pin("192.0.2.10", "aa") is True
    assert cv.check_cert_pin("192.0.2.10", "aa") is False
    with pytest.raises(cv.TvCertChangedError):
        cv.check_cert_pin("192.0.2.10", "bb")
    isolated_pins.write_text(json.dumps({"old": {"sha256": "cc"}}))   # format of the first TOFU version
    with pytest.raises(cv.TvCertChangedError):
        cv.check_receiver_identity("old", b"not-a-cert", None)


# ----------------------------------------------------------------------------- licensing / docs
@pytest.mark.parametrize("module", ["castv2.py", "rtp.py"])
def test_openscreen_derived_modules_carry_bsd_attribution(module):
    from pathlib import Path

    import airlinx.core.cast as cast_pkg

    head = "\n".join((Path(cast_pkg.__file__).parent / module).read_text(encoding="utf-8").splitlines()[:5])
    assert "openscreen" in head and "BSD-3-Clause" in head and "The Chromium Authors" in head
    assert "THIRD_PARTY_NOTICES.md" in head


def test_castv2_docstring_describes_device_authentication():
    from airlinx.core.cast import castv2

    doc = " ".join((castv2.__doc__ or "").split())
    assert "performs Cast device authentication" in doc
    assert "is not performed" not in doc


# --- known_tvs.json hardening -------------------------------------------------------------------

def test_corrupt_known_tvs_is_moved_to_bak_not_overwritten(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text('{"tv": {"auth": "tofu", ', "utf-8")
    assert cv.forget_tv_cert("x") is False          # reads via _load_pins
    bak = isolated_pins.with_name("known_tvs.json.bak")
    assert bak.read_text("utf-8") == '{"tv": {"auth": "tofu", '
    assert not isolated_pins.exists()


def test_corrupt_known_tvs_backup_survives_the_following_pin_write(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text("not json", "utf-8")
    assert cv.check_cert_pin("tv", "ab" * 32) is True
    assert isolated_pins.with_name("known_tvs.json.bak").read_text("utf-8") == "not json"
    assert "tv" in json.loads(isolated_pins.read_text("utf-8"))


def test_non_object_root_is_also_backed_up(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text("[1, 2]", "utf-8")
    cv._load_pins(isolated_pins)
    assert isolated_pins.with_name("known_tvs.json.bak").read_text("utf-8") == "[1, 2]"


def test_unreadable_but_existing_file_is_left_in_place(isolated_pins):
    isolated_pins.mkdir(parents=True)               # a directory: read fails with OSError, nothing to back up
    assert cv._load_pins(isolated_pins) == {}
    assert isolated_pins.is_dir()


@pytest.mark.parametrize("bad", ["string", 5, None, [1], True])
def test_non_dict_entry_counts_as_no_entry(isolated_pins, bad):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text(json.dumps({"tv": bad, "other": {"auth": "tofu", "tls": {"sha256": "aa"}}}), "utf-8")
    assert cv.check_cert_pin("tv", "cd" * 32) is True       # no AttributeError, pinned anew
    pins = json.loads(isolated_pins.read_text("utf-8"))
    assert pins["tv"]["tls"]["sha256"] == "cd" * 32
    assert pins["other"]["tls"]["sha256"] == "aa"           # valid neighbours are preserved


def test_check_receiver_identity_with_non_dict_entry(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text(json.dumps({"tv": "garbage"}), "utf-8")
    key = ec.generate_private_key(ec.SECP256R1())
    assert cv.check_receiver_identity("tv", der(make_cert("tv", key)), None) == "pinned"


def test_load_pins_strict_still_raises_and_keeps_the_file(isolated_pins):
    isolated_pins.parent.mkdir(parents=True)
    isolated_pins.write_text("{broken", "utf-8")
    with pytest.raises(ValueError):
        cv.load_pins_strict(isolated_pins)
    assert isolated_pins.read_text("utf-8") == "{broken"
