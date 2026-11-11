# Portions derived from Chromium openscreen (BSD-3-Clause, Copyright 2018 The Chromium Authors); see THIRD_PARTY_NOTICES.md.
"""Async CASTv2 control channel (TLS :8009).

Wire format: ``uint32 BE length`` + protobuf ``CastMessage`` (proto2). The message has
only seven scalar fields, so it is encoded/decoded by hand here (no generated protobuf
code, no dependency on a protobuf runtime version).

Features: virtual connections (CONNECT/CLOSE), heartbeat PING/PONG with dead-peer
detection, request/response correlation (``requestId`` or ``seqNum``), receiver
GET_STATUS / LAUNCH / STOP and message listeners for app namespaces.

The TV's TLS certificate is self-signed and rotates every few days, so it is not trusted on
its own: :meth:`CastChannel.open` performs Cast device authentication (``deviceauth``
AuthChallenge, signature chained to the embedded Cast/Eureka root CAs) and pins the device
identity in ``known_tvs.json``. Receivers that never answer device-auth fall back to a
rotation-tolerant TLS trust-on-first-use pin (see "Receiver authentication" below).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import itertools
import json
import logging
import os
import ssl
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from ..models import AirlinxError

log = logging.getLogger("airlinx.cast.castv2")

NS_CONNECTION = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
NS_WEBRTC = "urn:x-cast:com.google.cast.webrtc"
NS_REMOTING = "urn:x-cast:com.google.cast.remoting"

RECEIVER_ID = "receiver-0"
MIRRORING_APP_ID = "0F5096E8"         # "Chrome Mirroring" (audio + video)
DEFAULT_MEDIA_RECEIVER_ID = "CC1AD845"
IDLE_APP_IDS = {"E8C28D3C"}           # Backdrop / idle screen

HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 16.0
MAX_MESSAGE = 64 * 1024

PAYLOAD_STRING, PAYLOAD_BINARY = 0, 1


class CastError(Exception):
    """Protocol-level failure (LAUNCH_ERROR, bad response, closed channel)."""


class ChannelClosed(CastError):
    pass


# --------------------------------------------------------------------------------------
# Minimal protobuf codec for CastMessage
# --------------------------------------------------------------------------------------
@dataclass
class CastMessage:
    source_id: str
    destination_id: str
    namespace: str
    payload_utf8: Optional[str] = None
    payload_binary: Optional[bytes] = None
    protocol_version: int = 0  # CASTV2_1_0

    @property
    def payload_type(self) -> int:
        return PAYLOAD_BINARY if self.payload_binary is not None else PAYLOAD_STRING

    def json(self) -> Optional[dict]:
        if self.payload_utf8 is None:
            return None
        try:
            v = json.loads(self.payload_utf8)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf: bytes, p: int) -> tuple[int, int]:
    shift = res = 0
    while True:
        if p >= len(buf):
            raise ValueError("truncated varint")
        b = buf[p]
        p += 1
        res |= (b & 0x7F) << shift
        if not b & 0x80:
            return res, p
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def encode_message(m: CastMessage) -> bytes:
    def ld(field: int, data: bytes) -> bytes:
        return _varint(field << 3 | 2) + _varint(len(data)) + data

    out = _varint(1 << 3) + _varint(m.protocol_version)
    out += ld(2, m.source_id.encode()) + ld(3, m.destination_id.encode()) + ld(4, m.namespace.encode())
    out += _varint(5 << 3) + _varint(m.payload_type)
    if m.payload_binary is not None:
        out += ld(7, m.payload_binary)
    else:
        out += ld(6, (m.payload_utf8 or "").encode())
    return out


def decode_message(buf: bytes) -> CastMessage:
    fields: dict[int, Any] = {}
    p = 0
    while p < len(buf):
        key, p = _read_varint(buf, p)
        num, wt = key >> 3, key & 7
        if wt == 0:
            fields[num], p = _read_varint(buf, p)
        elif wt == 2:
            ln, p = _read_varint(buf, p)
            if p + ln > len(buf):
                raise ValueError("truncated field")
            fields[num] = buf[p:p + ln]
            p += ln
        elif wt == 5:
            p += 4
        elif wt == 1:
            p += 8
        else:
            raise ValueError(f"unsupported wire type {wt}")

    def s(n: int) -> str:
        v = fields.get(n, b"")
        return v.decode("utf-8", "replace") if isinstance(v, bytes) else ""

    ptype = fields.get(5, PAYLOAD_STRING)
    return CastMessage(
        protocol_version=fields.get(1, 0) if isinstance(fields.get(1, 0), int) else 0,
        source_id=s(2), destination_id=s(3), namespace=s(4),
        payload_utf8=s(6) if ptype == PAYLOAD_STRING else None,
        payload_binary=bytes(fields.get(7, b"")) if ptype == PAYLOAD_BINARY else None,
    )


def frame_message(m: CastMessage) -> bytes:
    data = encode_message(m)
    return struct.pack(">I", len(data)) + data


# --------------------------------------------------------------------------------------
# Receiver authentication: Cast device-auth + pinning (~/.config/airlinx/known_tvs.json, 0600)
#
# The TLS certificate of a Cast receiver is self-signed and *ephemeral*: openscreen/Chrome
# require its lifetime to be <= 4 days, so receivers rotate it. Pinning it would lock the
# user out after the first rotation. Instead (as Chrome does, openscreen
# cast/sender/channel/cast_auth_util.cc) we send an AuthChallenge on
# ``urn:x-cast:com.google.cast.tp.deviceauth``; the receiver signs ``nonce || TLS cert DER``
# with its long-lived *device certificate*, whose chain must lead to the Cast Root CA or the
# Eureka Root CA (embedded below, from openscreen cast/common/certificate/*-inc.h). We pin
# the device certificate subject (stable identity of the TV).
#
# Fallback for receivers that do not answer device-auth: TLS TOFU that tolerates
# rotation (new cert with a later notBefore and a short, Cast-style lifetime is re-pinned);
# anything else is rejected. A TV that once passed device-auth must always pass it.
# --------------------------------------------------------------------------------------
NS_DEVICEAUTH = "urn:x-cast:com.google.cast.tp.deviceauth"
DEVICE_AUTH_TIMEOUT = 3.0
MAX_TLS_CERT_LIFETIME = 4 * 86400          # kMaxSelfSignedCertLifetimeInDays
CLOCK_SKEW = 300
AUDIO_ONLY_POLICY_OID = "1.3.6.1.4.1.11129.2.5.2"
SIG_PKCS1V15, SIG_PSS = 1, 2
HASH_SHA1, HASH_SHA256 = 0, 1

CAST_ROOT_CA_B64 = (
    "MIIDxTCCAq2gAwIBAgIBAjANBgkqhkiG9w0BAQUFADB1MQswCQYDVQQGEwJVUzETMBEGA1UECAwKQ2FsaWZvcm5pYTEWMBQG"
    "A1UEBwwNTW91bnRhaW4gVmlldzETMBEGA1UECgwKR29vZ2xlIEluYzENMAsGA1UECwwEQ2FzdDEVMBMGA1UEAwwMQ2FzdCBS"
    "b290IENBMB4XDTE0MDQwMjE3MzQyNloXDTM0MDMyODE3MzQyNlowdTELMAkGA1UEBhMCVVMxEzARBgNVBAgMCkNhbGlmb3Ju"
    "aWExFjAUBgNVBAcMDU1vdW50YWluIFZpZXcxEzARBgNVBAoMCkdvb2dsZSBJbmMxDTALBgNVBAsMBENhc3QxFTATBgNVBAMM"
    "DENhc3QgUm9vdCBDQTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBALrZZZ3aOdPBd/bU0K6PWAhoOUqV7XDP/XkI"
    "qarl6binLaBnR4qeyc9wswWHaRHscJiXw+bDw+u9xrA9/E/BXjif2s9zMAZbeTfBXoyHR5SaQZIq1pXEcVwnXQixgMaSvRvj"
    "QZeh7HWfVZ4+n48cx2VkB9OzlqEEn5HE3gp7bNnIwHgxoBlCqeiD48788c7CLiRGlQkZysBGsuUButdP87/2aa2ZBPqgBzkO"
    "5t9RRwfA5KlcS5TFL7OgMH/nlWuyrzIN8YzVbct7R6cIq8sno03PSlrxBdH4YsUQKnRpquZLlvub2GPkWGbTrYpu/3te+aVW"
    "Hi2CMVvw4iTmQUofrhMCAwEAAaNgMF4wDwYDVR0TBAgwBgEB/wIBAjAdBgNVHQ4EFgQUfJoefd95VLzXzF7KmYZFeWV0KBkw"
    "HwYDVR0jBBgwFoAUfJoefd95VLzXzF7KmYZFeWV0KBkwCwYDVR0PBAQDAgEGMA0GCSqGSIb3DQEBBQUAA4IBAQCA9Fr7PSgZ"
    "USDX1PsSl0pl8lg1kncwavHXtlEaf5rNx3sDQq1VagCv8OEGwr1reHXb/kERU0o5u5o6xlk0Lywz47LWXH/deOtxWznag5DF"
    "MeI/I+/a6ystd17ew0PSyWtZgsrV7fqhZFvL8Q0aYuGc6KcYcPBfF5b47Ybbrh3gzz5dLu4WbZUrPP2X8wVaJGhNObb45Fi6"
    "9eAmeFHFW11OCeVsR4t6Wi6JU+bMNlsmPPhyQwKC0ivN8NOj7BM+UtWDPQfcHUNlejMCAaPOt9ZgUTsJwiOKMv6YGWBik4XN"
    "NEbb1SMPedp3ACoCbYNYzgN3NeGjIJPCSqKkRhx1LB9N"
)
EUREKA_ROOT_CA_B64 = (
    "MIIDwzCCAqugAwIBAgIBATANBgkqhkiG9w0BAQUFADB8MQswCQYDVQQGEwJVUzETMBEGA1UECAwKQ2FsaWZvcm5pYTEWMBQG"
    "A1UEBwwNTW91bnRhaW4gVmlldzETMBEGA1UECgwKR29vZ2xlIEluYzESMBAGA1UECwwJR29vZ2xlIFRWMRcwFQYDVQQDDA5F"
    "dXJla2EgUm9vdCBDQTAeFw0xMjEyMTcyMjM5MzNaFw0zMjEyMTIyMjM5MzNaMHwxCzAJBgNVBAYTAlVTMRMwEQYDVQQIDApD"
    "YWxpZm9ybmlhMRYwFAYDVQQHDA1Nb3VudGFpbiBWaWV3MRMwEQYDVQQKDApHb29nbGUgSW5jMRIwEAYDVQQLDAlHb29nbGUg"
    "VFYxFzAVBgNVBAMMDkV1cmVrYSBSb290IENBMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuRHQ6hLcMuHfXDNr"
    "GXMdnZ7QOXa/pYQJpv1ubencjzZO6YgCvZ/06ET9TPWaAlZqRypjbFhFzHxmJNx5ecMqpLKLoPeitc0Gftu+7AyG8g0kYHSE"
    "yikjhALYp+078ewmR1TjsS3mZA/2csXpmFIXwPzyLCDIQPhHyTKeO5exi/WYJHBjZhnBUugEBT1fjbzYS693mG8feNG2UCdN"
    "5OwUaWcfWK+poBEmPJQyB3/X6Wkfrj9PY4qPidbyGXhcIY6xtlfYwOHufW7d8ToKavG6//mDL9y1pCAXYzbvyGIZzFbOsuox"
    "iUt4WMG/AxOZ4BLyiKqblNrddnkXHjTRCsQHRQIDAQABo1AwTjAdBgNVHQ4EFgQURE4qR1jYuUiR9k/OdKkdMpqNjekwHwYD"
    "VR0jBBgwFoAURE4qR1jYuUiR9k/OdKkdMpqNjekwDAYDVR0TBAUwAwEB/zANBgkqhkiG9w0BAQUFAAOCAQEAP8gmoG5cBUB5"
    "oZipM95odIXurrccM1mwEd6f9E/T61EJfUd+blGF9FTNg5glsbqwV+yT2xLi7FFJepZzm8iWbYWM0+E8+jLiWAx3bYcMNAGq"
    "MKl24MDn214b6RAwpOAJSSa5WM1aB+VQdd6aO/ZTfrFTXkUnTxfjCDOyUAq79PwllyneQXUw+nc4qmWKc0/qEXvrfBdgJw68"
    "PnZS2IvtGvjrN7sR/a5wFwr+4K0Gsx9pinIEwsAzC9YvY0wzERS4YjaIxQNlARmj7wC7bw6S/zQcodYx0Fxen5l9x8q9fHIL"
    "9Fylfm4EqNKZLFEBFP6iSPB+voQNtNPi8w593ov1Mw=="
)


def production_roots() -> list[x509.Certificate]:
    return [x509.load_der_x509_certificate(base64.b64decode("".join(b)))
            for b in (CAST_ROOT_CA_B64, EUREKA_ROOT_CA_B64)]


# --------------------------------------------------------------------------------------
# Developer trust hook (tools/virtual_tv, docs/VIRTUAL_TV.md)
#
# A virtual TV cannot carry a Google-issued device certificate, so its device-auth reply chains to a
# throw-away TEST root CA instead. AirLinx may be told to trust such a root, but only like this:
#   * off by default; the list below is empty unless something sets it in THIS process;
#   * never read from config.json / settings (nothing persistent can enable it);
#   * from the environment only when BOTH AIRLINX_DEV=1 and AIRLINX_DEV_CAST_ROOT=<PEM file> are set;
#   * every activation logs a loud warning and AppState.dev_cast_root makes the UI show it;
#   * a TV authenticated through a dev root is pinned with dev=true and in a separate pin file
#     (known_tvs.dev.json), so the test identity can neither replace nor be mistaken for a real TV;
#   * tests inject roots with set_dev_roots()/dev_roots(), without touching the environment.
# The risk it guards against: whoever holds the private key of a trusted dev root can impersonate any
# TV to this AirLinx process (device-auth is what stops a LAN attacker from posing as the TV).
# --------------------------------------------------------------------------------------
DEV_ENV = "AIRLINX_DEV"
DEV_ROOT_ENV = "AIRLINX_DEV_CAST_ROOT"
MAX_DEV_ROOT_PEM = 64 * 1024
MAX_DEV_ROOTS = 4
_dev_roots: list[x509.Certificate] = []


def cast_trust_roots() -> list[x509.Certificate]:
    return production_roots() + list(_dev_roots)


def dev_trust_active() -> bool:
    return bool(_dev_roots)


def dev_trust_subjects() -> list[str]:
    return [c.subject.rfc4514_string() for c in _dev_roots]


def _check_dev_root(c: x509.Certificate, now: float) -> None:
    if any(c.subject == p.subject for p in production_roots()):
        raise ValueError("a dev root must not reuse the subject of a real Cast root")
    try:
        bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        raise ValueError("dev root has no basicConstraints") from None
    if not bc.ca:
        raise ValueError("dev root is not a CA certificate")
    if c.subject != c.issuer or not _issued_by(c, c):
        raise ValueError("dev root is not self-signed")
    if not _not_before(c) - CLOCK_SKEW <= now <= _not_after(c) + CLOCK_SKEW:
        raise ValueError("dev root is not valid now")


def set_dev_roots(certs: list[x509.Certificate]) -> list[str]:
    """Replace the dev roots of this process (``[]`` switches the hook off). Validates every certificate.
    Meant for tests and for :func:`configure_dev_trust`; logs a loud warning when anything is trusted."""
    now = time.time()
    if len(certs) > MAX_DEV_ROOTS:
        raise ValueError("too many dev roots")
    for c in certs:
        _check_dev_root(c, now)
    _dev_roots[:] = list(certs)
    subjects = dev_trust_subjects()
    if subjects:
        log.warning("DEVELOPER MODE: trusting TEST Cast root CA(s) %s for device authentication. A holder of "
                    "its key can impersonate any TV to this AirLinx. NEVER use this for daily use.", subjects)
    return subjects


class dev_roots:  # noqa: N801 (context manager)
    """``with dev_roots([cert]):`` injects test roots without the environment and restores the old state."""

    def __init__(self, certs: list[x509.Certificate]):
        self.certs = certs
        self._old: list[x509.Certificate] = []

    def __enter__(self) -> list[str]:
        self._old = list(_dev_roots)
        return set_dev_roots(self.certs)

    def __exit__(self, *exc: object) -> None:
        _dev_roots[:] = self._old


def configure_dev_trust(environ: Optional[dict] = None) -> list[str]:
    """Process start-up: enable the dev roots from the environment, only with AIRLINX_DEV=1 *and* a root file.
    Never raises: a bad setup leaves the hook off. Returns the trusted subjects."""
    env = os.environ if environ is None else environ
    path, flag = env.get(DEV_ROOT_ENV), env.get(DEV_ENV)
    _dev_roots.clear()
    if not path:
        return []
    if flag != "1":
        log.warning("%s is set but %s=1 is not: ignoring it (developer trust stays off)", DEV_ROOT_ENV, DEV_ENV)
        return []
    try:
        p = Path(path)
        if p.stat().st_size > MAX_DEV_ROOT_PEM:
            raise ValueError("file too large")
        certs = x509.load_pem_x509_certificates(p.read_bytes())
        return set_dev_roots(certs)
    except (OSError, ValueError) as e:
        _dev_roots.clear()
        log.error("ignoring %s=%s: %s (developer trust stays off)", DEV_ROOT_ENV, path, e)
        return []


class TvCertChangedError(AirlinxError):
    def __init__(self, key: str = ""):
        super().__init__("tv_cert_changed", "Certyfikat telewizora się zmienił — możliwe podszywanie się. "
                                            "Potwierdź w ustawieniach, jeśli wymieniono TV.")
        self.key = key


class TvAuthFailedError(AirlinxError):
    def __init__(self, detail: str = ""):
        super().__init__("tv_auth_failed", "Nie udało się potwierdzić, że to prawdziwe urządzenie Google Cast.")
        self.detail = detail


class DeviceAuthUnavailable(CastError):
    """Receiver did not take part in device-auth (no reply / AuthError)."""


class DeviceAuthFailed(CastError):
    """Receiver answered, but the answer does not verify (forgery, untrusted chain, ...)."""


@dataclass
class DeviceIdentity:
    subject: str          # RFC 4514 subject of the device certificate (pinned)
    spki_sha256: str
    issuer: str
    audio_only: bool
    tls_sha256: str
    dev: bool = False     # chains to a developer-mode TEST root (see the hook above), not to a Google root


def _pb_fields(buf: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    p = 0
    while p < len(buf):
        key, p = _read_varint(buf, p)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, p = _read_varint(buf, p)
        elif wt == 2:
            ln, p = _read_varint(buf, p)
            if p + ln > len(buf):
                raise ValueError("truncated field")
            v, p = buf[p:p + ln], p + ln
        elif wt == 5:
            v, p = buf[p:p + 4], p + 4
        elif wt == 1:
            v, p = buf[p:p + 8], p + 8
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.setdefault(num, []).append(v)
    return out


def _pb_ld(field_no: int, data: bytes) -> bytes:
    return _varint(field_no << 3 | 2) + _varint(len(data)) + data


def build_auth_challenge(nonce: bytes, hash_algorithm: int = HASH_SHA256) -> bytes:
    """DeviceAuthMessage{challenge{signature_algorithm, sender_nonce, hash_algorithm}}."""
    ch = _varint(1 << 3) + _varint(SIG_PKCS1V15) + _pb_ld(2, nonce) + _varint(3 << 3) + _varint(hash_algorithm)
    return _pb_ld(1, ch)


def build_auth_response(*, signature: bytes, client_cert: bytes, intermediates: list[bytes], nonce: bytes,
                        hash_algorithm: int = HASH_SHA256, signature_algorithm: int = SIG_PKCS1V15) -> bytes:
    """Receiver side (used by tests / fake receivers)."""
    r = _pb_ld(1, signature) + _pb_ld(2, client_cert) + b"".join(_pb_ld(3, i) for i in intermediates)
    r += _varint(4 << 3) + _varint(signature_algorithm) + _pb_ld(5, nonce) + _varint(6 << 3) + _varint(hash_algorithm)
    return _pb_ld(2, r)


def parse_auth_response(buf: bytes) -> dict:
    try:
        msg = _pb_fields(buf)
        if 3 in msg:
            err = _pb_fields(msg[3][0]).get(1, [0])[0]
            raise DeviceAuthUnavailable(f"receiver AuthError type {err}")
        if 2 not in msg:
            raise DeviceAuthUnavailable("DeviceAuthMessage without response")
        r = _pb_fields(msg[2][0])
    except ValueError as e:
        raise DeviceAuthFailed(f"malformed AuthResponse: {e}") from e
    if 1 not in r or 2 not in r:
        raise DeviceAuthFailed("AuthResponse lacks signature or certificate")
    return dict(signature=bytes(r[1][0]), client_cert=bytes(r[2][0]),
                intermediates=[bytes(x) for x in r.get(3, [])],
                signature_algorithm=r.get(4, [SIG_PKCS1V15])[-1], nonce=bytes(r.get(5, [b""])[-1]),
                hash_algorithm=r.get(6, [HASH_SHA1])[-1])


def _utc(c: x509.Certificate, attr: str) -> float:
    v = getattr(c, attr + "_utc", None)  # cryptography >= 42
    if v is None:
        v = getattr(c, attr).replace(tzinfo=datetime.timezone.utc)
    return v.timestamp()


def _not_before(c: x509.Certificate) -> float:
    return _utc(c, "not_valid_before")


def _not_after(c: x509.Certificate) -> float:
    return _utc(c, "not_valid_after")


def check_tls_cert_validity(peer: x509.Certificate, now: float) -> None:
    """openscreen VerifyTLSCertificateValidity: valid now and lifetime capped at 4 days."""
    if now < _not_before(peer):
        raise DeviceAuthFailed("TLS certificate not yet valid")
    if _not_after(peer) < now:
        raise DeviceAuthFailed("TLS certificate expired")
    if _not_after(peer) > now + MAX_TLS_CERT_LIFETIME:
        raise DeviceAuthFailed("TLS certificate lifetime too long")


def verify_device_chain(leaf_der: bytes, intermediates: list[bytes], now: float,
                        roots: Optional[list[x509.Certificate]] = None) -> list[x509.Certificate]:
    """Path leaf → … → trusted root (signatures, validity, CA/pathlen). Returns the path."""
    roots = cast_trust_roots() if roots is None else roots
    try:
        leaf = x509.load_der_x509_certificate(leaf_der)
        pool = [x509.load_der_x509_certificate(d) for d in intermediates]
    except ValueError as e:
        raise DeviceAuthFailed(f"unparseable device certificate: {e}") from e
    path = [leaf]
    cur = leaf
    for _ in range(6):
        for cand, is_root in [(r, True) for r in roots] + [(i, False) for i in pool]:
            if cand.subject != cur.issuer or cand in path:
                continue
            if not _issued_by(cur, cand):
                continue
            path.append(cand)
            cur = cand
            if is_root:
                _check_path(path, now)
                return path
            break
        else:
            raise DeviceAuthFailed(f"device certificate does not chain to a Cast root ({cur.issuer.rfc4514_string()})")
    raise DeviceAuthFailed("device certificate chain too long")


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    """Signature check by hand: cryptography's verify_directly_issued_by rejects SHA-1, which
    the Cast/Eureka roots and older device chains still use."""
    key = issuer.public_key()
    h = cert.signature_hash_algorithm
    if h is None:
        return False
    try:
        if isinstance(key, rsa.RSAPublicKey):
            params = getattr(cert, "signature_algorithm_parameters", None)
            pad = params if isinstance(params, (padding.PSS, padding.PKCS1v15)) else padding.PKCS1v15()
            key.verify(cert.signature, cert.tbs_certificate_bytes, pad, h)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(h))
        else:
            return False
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _check_path(path: list[x509.Certificate], now: float) -> None:
    for c in path:
        if not _not_before(c) - CLOCK_SKEW <= now <= _not_after(c) + CLOCK_SKEW:
            raise DeviceAuthFailed(f"certificate outside validity: {c.subject.rfc4514_string()}")
    for depth, c in enumerate(path[1:], start=1):  # issuers must be CAs; pathlen counts CAs below
        try:
            bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound:
            if depth < len(path) - 1:
                raise DeviceAuthFailed(f"intermediate without basicConstraints: {c.subject.rfc4514_string()}")
            continue  # legacy roots are trusted anchors
        if not bc.ca:
            raise DeviceAuthFailed(f"issuer is not a CA: {c.subject.rfc4514_string()}")
        if bc.path_length is not None and depth - 1 > bc.path_length:
            raise DeviceAuthFailed(f"pathlen constraint violated: {c.subject.rfc4514_string()}")
    try:
        ku = path[0].extensions.get_extension_for_class(x509.KeyUsage).value
        if not ku.digital_signature:
            raise DeviceAuthFailed("device certificate not allowed to sign")
    except x509.ExtensionNotFound:
        pass


def _is_audio_only(path: list[x509.Certificate]) -> bool:
    for c in path:
        try:
            pol = c.extensions.get_extension_for_class(x509.CertificatePolicies).value
        except x509.ExtensionNotFound:
            continue
        if any(p.policy_identifier.dotted_string == AUDIO_ONLY_POLICY_OID for p in pol):
            return True
    return False


def verify_signed_data(cert: x509.Certificate, data: bytes, signature: bytes, hash_algorithm: int,
                       signature_algorithm: int = SIG_PKCS1V15) -> None:
    h: hashes.HashAlgorithm = hashes.SHA256() if hash_algorithm == HASH_SHA256 else hashes.SHA1()
    key = cert.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey):
            pad = (padding.PSS(padding.MGF1(h), getattr(padding.PSS, "AUTO", padding.PSS.MAX_LENGTH)) if signature_algorithm == SIG_PSS
                   else padding.PKCS1v15())
            key.verify(signature, data, pad, h)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, data, ec.ECDSA(h))
        else:
            raise DeviceAuthFailed(f"unsupported device key {type(key).__name__}")
    except InvalidSignature as e:
        raise DeviceAuthFailed("device signature over TLS certificate does not verify") from e


def verify_auth_response(resp: dict, peer_der: bytes, nonce: bytes, now: Optional[float] = None,
                         roots: Optional[list[x509.Certificate]] = None) -> DeviceIdentity:
    now = time.time() if now is None else now
    try:
        peer = x509.load_der_x509_certificate(peer_der)
    except ValueError as e:
        raise DeviceAuthFailed(f"unparseable TLS certificate: {e}") from e
    check_tls_cert_validity(peer, now)
    if resp["nonce"] and resp["nonce"] != nonce:
        raise DeviceAuthFailed("sender nonce mismatch")
    path = verify_device_chain(resp["client_cert"], resp["intermediates"], now, roots)
    verify_signed_data(path[0], resp["nonce"] + peer_der, resp["signature"], resp["hash_algorithm"],
                       resp["signature_algorithm"])
    leaf = path[0]
    spki = leaf.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return DeviceIdentity(subject=leaf.subject.rfc4514_string(), spki_sha256=hashlib.sha256(spki).hexdigest(),
                          issuer=leaf.issuer.rfc4514_string(), audio_only=_is_audio_only(path),
                          tls_sha256=hashlib.sha256(peer_der).hexdigest(), dev=path[-1] in _dev_roots)


def known_tvs_path() -> Path:
    override = os.environ.get("AIRLINX_KNOWN_TVS")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    # developer trust must never read or write the real pin store
    return Path(base) / "airlinx" / ("known_tvs.dev.json" if _dev_roots else "known_tvs.json")


def cert_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def load_pins_strict(path: Path) -> dict[str, dict]:
    """Pins; {} only when the file does not exist. Raises OSError / ValueError when it exists but cannot
    be read or parsed (callers that guard a downgrade must fail closed on that)."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("known_tvs root is not an object")
    return data


def _load_pins(path: Path) -> dict[str, dict]:
    try:
        return load_pins_strict(path)
    except (OSError, ValueError) as e:
        log.warning("cannot read %s (%s); treating as empty", path, e)
        return {}


def _save_pins(path: Path, pins: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(pins, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _tls_info(peer_der: bytes) -> dict:
    info: dict[str, Any] = {"sha256": cert_fingerprint(peer_der)}
    with contextlib.suppress(ValueError):
        c = x509.load_der_x509_certificate(peer_der)
        info.update(not_before=int(_not_before(c)), not_after=int(_not_after(c)))
    return info


def _is_plausible_rotation(old: dict, new: dict, now: float) -> bool:
    """New self-signed TLS cert looks like a Cast rotation (later notBefore, short lifetime)."""
    nb, na, old_nb = new.get("not_before"), new.get("not_after"), old.get("not_before")
    if nb is None or na is None or old_nb is None:
        return False
    return (nb > old_nb and nb <= now + CLOCK_SKEW and na >= now
            and na - nb <= MAX_TLS_CERT_LIFETIME + CLOCK_SKEW)


def check_receiver_identity(key: str, peer_der: bytes, identity: Optional[DeviceIdentity],
                            now: Optional[float] = None) -> str:
    """Pin / compare. Returns "pinned" | "match" | "rotated"; raises TvCertChangedError."""
    now = time.time() if now is None else now
    path = known_tvs_path()
    pins = _load_pins(path)
    entry = pins.get(key)
    tls = _tls_info(peer_der)
    if identity is not None:
        if entry and entry.get("device_subject") and entry["device_subject"] != identity.subject:
            log.error("Cast device identity for %s changed: %s -> %s", key, entry["device_subject"],
                      identity.subject)
            raise TvCertChangedError(key)
        if entry and entry.get("device_subject") and bool(entry.get("dev")) != identity.dev:
            log.error("Cast device identity for %s switched between a real and a developer-test root", key)
            raise TvCertChangedError(key)
        result = "match" if entry and entry.get("device_subject") else "pinned"
        pins[key] = {"auth": "device", "device_subject": identity.subject, "device_spki": identity.spki_sha256,
                     "device_issuer": identity.issuer, "tls": tls,
                     "first_seen": (entry or {}).get("first_seen", int(now))}
        if identity.dev:
            pins[key]["dev"] = True
        _save_pins(path, pins)
        return result
    # no device-auth from this receiver
    if entry and entry.get("device_subject"):
        log.error("receiver %s passed device-auth before but not now", key)
        raise TvCertChangedError(key)
    old_tls = (entry or {}).get("tls") or ({"sha256": entry["sha256"]} if entry and "sha256" in entry else None)
    if old_tls is None:
        result = "pinned"
    elif old_tls.get("sha256") == tls["sha256"]:
        return "match"
    elif _is_plausible_rotation(old_tls, tls, now):
        result = "rotated"
        log.info("TLS certificate of %s rotated (notBefore %s -> %s)", key, old_tls.get("not_before"),
                 tls.get("not_before"))
    else:
        log.error("TLS certificate of %s changed suspiciously: %s -> %s", key, old_tls, tls)
        raise TvCertChangedError(key)
    pins[key] = {"auth": "tofu", "tls": tls, "first_seen": (entry or {}).get("first_seen", int(now))}
    _save_pins(path, pins)
    return result


def check_cert_pin(key: str, fingerprint: str) -> bool:
    """Plain fingerprint TOFU (kept for tooling/tests). True when newly pinned."""
    path = known_tvs_path()
    pins = _load_pins(path)
    entry = pins.get(key)
    if entry is not None:
        old = (entry.get("tls") or {}).get("sha256") or entry.get("sha256")
        if old != fingerprint:
            raise TvCertChangedError(key)
        return False
    pins[key] = {"auth": "tofu", "tls": {"sha256": fingerprint}, "first_seen": int(time.time())}
    _save_pins(path, pins)
    return True


def forget_tv_cert(device_id: str) -> bool:
    """Drop the pinned identity (e.g. after the TV was replaced). True if something was removed."""
    path = known_tvs_path()
    pins = _load_pins(path)
    if pins.pop(device_id, None) is None:
        return False
    _save_pins(path, pins)
    return True


# --------------------------------------------------------------------------------------
# Channel
# --------------------------------------------------------------------------------------
Listener = Callable[[CastMessage, Optional[dict]], None]


def _app_is_idle(app: dict) -> bool:
    return bool(app.get("isIdleScreen")) or app.get("appId") in IDLE_APP_IDS


class CastChannel:
    """One TLS connection to a Cast receiver. All methods must run on the same loop."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 sender_id: str = "sender-airlinx", *, heartbeat: bool = True):
        self._reader = reader
        self._writer = writer
        self.sender_id = sender_id
        self._ids = itertools.count(1)
        self._listeners: list[Listener] = []
        self._connected: set[str] = set()
        self.closed = asyncio.Event()
        self.close_reason: Optional[str] = None
        self._last_rx = time.monotonic()
        self.identity: Optional[DeviceIdentity] = None
        self.auth_result: Optional[str] = None
        self._rx_task = asyncio.create_task(self._rx_loop(), name="castv2-rx")
        self._hb_task = (asyncio.create_task(self._hb_loop(), name="castv2-hb") if heartbeat else None)

    # ---- construction --------------------------------------------------------------
    @classmethod
    async def open(cls, host: str, port: int = 8009, *, timeout: float = 8.0,
                   use_tls: bool = True, heartbeat: bool = True,
                   sender_id: str = "sender-airlinx", pin_key: Optional[str] = None) -> "CastChannel":
        """Connect and authenticate the receiver (TLS only): Cast device-auth, then identity
        pinning under ``pin_key`` (default: host). Raises TvAuthFailedError / TvCertChangedError
        before any CONNECT or app message is sent."""
        ctx: Optional[ssl.SSLContext] = None
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            # Self-signed, rotating receiver certificate: no CA validation possible here; the
            # receiver is authenticated by device-auth (signature over this exact certificate).
            ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout)
        ch = cls(reader, writer, sender_id, heartbeat=heartbeat)
        try:
            if use_tls:
                sslobj = writer.get_extra_info("ssl_object")
                der = sslobj.getpeercert(binary_form=True) if sslobj else None
                if not der:
                    raise TvAuthFailedError("receiver presented no TLS certificate")
                await ch._authenticate(pin_key or host, der)
            await ch.connect_virtual(RECEIVER_ID)
        except BaseException:
            await ch.close(graceful=False)
            raise
        return ch

    async def _authenticate(self, key: str, peer_der: bytes) -> None:
        nonce = os.urandom(16)
        identity: Optional[DeviceIdentity] = None
        try:
            waiter = asyncio.ensure_future(self.wait_for(
                lambda m, p: m.namespace == NS_DEVICEAUTH and m.payload_binary is not None,
                timeout=DEVICE_AUTH_TIMEOUT))
            await asyncio.sleep(0)
            await self.send_binary(NS_DEVICEAUTH, build_auth_challenge(nonce))
            m, _ = await waiter
            assert m.payload_binary is not None
            resp = parse_auth_response(m.payload_binary)
            identity = verify_auth_response(resp, peer_der, nonce)
            log.info("Cast device-auth OK for %s: %s (issuer %s)%s", key, identity.subject, identity.issuer,
                     " [audio-only]" if identity.audio_only else "")
        except DeviceAuthFailed as e:
            log.error("Cast device-auth FAILED for %s: %s", key, e)
            raise TvAuthFailedError(str(e)) from e
        except (DeviceAuthUnavailable, asyncio.TimeoutError) as e:
            log.warning("receiver %s did not complete device-auth (%s); falling back to TLS pinning", key,
                        e or "timeout")
        self.identity = identity
        self.auth_result = check_receiver_identity(key, peer_der, identity)

    @property
    def alive(self) -> bool:
        return not self.closed.is_set()

    def next_id(self) -> int:
        return next(self._ids)

    # ---- low level -------------------------------------------------------------------
    async def send(self, namespace: str, payload: dict, dest: str = RECEIVER_ID) -> None:
        if self.closed.is_set():
            raise ChannelClosed(self.close_reason or "channel closed")
        text = json.dumps(payload, separators=(",", ":"))
        if namespace != NS_HEARTBEAT:
            log.debug("TX %s -> %s %s", namespace, dest, text[:1500])
        m = CastMessage(self.sender_id, dest, namespace, payload_utf8=text)
        try:
            self._writer.write(frame_message(m))
            await self._writer.drain()
        except (OSError, ConnectionError) as e:
            self._mark_closed(f"send failed: {e}")
            raise ChannelClosed(str(e)) from e

    async def send_binary(self, namespace: str, data: bytes, dest: str = RECEIVER_ID) -> None:
        if self.closed.is_set():
            raise ChannelClosed(self.close_reason or "channel closed")
        m = CastMessage(self.sender_id, dest, namespace, payload_binary=data)
        try:
            self._writer.write(frame_message(m))
            await self._writer.drain()
        except (OSError, ConnectionError) as e:
            self._mark_closed(f"send failed: {e}")
            raise ChannelClosed(str(e)) from e

    def add_listener(self, cb: Listener) -> Callable[[], None]:
        self._listeners.append(cb)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(cb)
        return remove

    async def wait_for(self, pred: Callable[[CastMessage, Optional[dict]], bool],
                       timeout: float = 10.0) -> tuple[CastMessage, Optional[dict]]:
        """Wait for the next message satisfying ``pred``; registers before returning control."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()

        def cb(m: CastMessage, p: Optional[dict]) -> None:
            if not fut.done():
                try:
                    if pred(m, p):
                        fut.set_result((m, p))
                except Exception as e:  # noqa: BLE001
                    fut.set_exception(e)

        remove = self.add_listener(cb)
        closed = asyncio.ensure_future(self.closed.wait())
        try:
            done, _ = await asyncio.wait({fut, closed}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            if closed in done:
                raise ChannelClosed(self.close_reason or "channel closed")
            raise asyncio.TimeoutError("timeout waiting for cast message")
        finally:
            remove()
            closed.cancel()
            if not fut.done():
                fut.cancel()

    async def request(self, namespace: str, payload: dict, dest: str = RECEIVER_ID, *,
                      id_field: str = "requestId", timeout: float = 10.0) -> dict:
        """Send ``payload`` with a fresh id in ``id_field`` and await the reply with that id."""
        rid = self.next_id()
        payload = dict(payload, **{id_field: rid})
        waiter = asyncio.ensure_future(self.wait_for(
            lambda m, p: m.namespace == namespace and p is not None and p.get(id_field) == rid,
            timeout=timeout))
        await asyncio.sleep(0)  # let the waiter register its listener
        try:
            await self.send(namespace, payload, dest)
        except BaseException:
            waiter.cancel()
            raise
        _, p = await waiter
        assert p is not None
        return p

    async def connect_virtual(self, dest: str) -> None:
        await self.send(NS_CONNECTION, {
            "type": "CONNECT", "origin": {}, "userAgent": "AirLinx",
            "senderInfo": {"sdkType": 2, "version": "X11; Linux x86_64", "browserVersion": "131.0",
                           "platform": 4, "connectionType": 1}}, dest)
        self._connected.add(dest)

    # ---- receiver API ------------------------------------------------------------------
    async def get_status(self, timeout: float = 8.0) -> dict:
        p = await self.request(NS_RECEIVER, {"type": "GET_STATUS"}, timeout=timeout)
        return p.get("status") or {}

    async def get_running_app(self, timeout: float = 8.0) -> Optional[dict]:
        """The non-idle application currently running on the receiver (or None)."""
        status = await self.get_status(timeout)
        for app in status.get("applications") or []:
            if not _app_is_idle(app):
                return app
        return None

    async def launch(self, app_id: str, *, timeout: float = 30.0,
                     on_progress: Optional[Callable[[str], None]] = None) -> dict:
        """LAUNCH ``app_id`` and return its application entry (with transportId/sessionId)."""
        rid = self.next_id()
        q: asyncio.Queue = asyncio.Queue()
        remove = self.add_listener(lambda m, p: q.put_nowait(p) if m.namespace == NS_RECEIVER and p else None)
        try:
            await self.send(NS_RECEIVER, {"type": "LAUNCH", "requestId": rid, "appId": app_id,
                                          "language": "pl-PL", "supportedAppTypes": ["WEB"]})
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise asyncio.TimeoutError("LAUNCH timeout")
                get = asyncio.ensure_future(q.get())
                closed = asyncio.ensure_future(self.closed.wait())
                done, _ = await asyncio.wait({get, closed}, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                closed.cancel()
                if get not in done:
                    get.cancel()
                    if closed in done:
                        raise ChannelClosed(self.close_reason or "channel closed")
                    raise asyncio.TimeoutError("LAUNCH timeout")
                p = get.result()
                ptype = p.get("type")
                if ptype in ("LAUNCH_ERROR", "INVALID_REQUEST") and p.get("requestId") in (rid, None, 0):
                    raise CastError(f"{ptype}: {p.get('reason') or p}")
                status = p.get("status")
                if ptype == "LAUNCH_STATUS" or isinstance(status, str):
                    if on_progress and isinstance(status, str):
                        on_progress(status)
                    continue
                if isinstance(status, dict):
                    for a in status.get("applications") or []:
                        if a.get("appId") == app_id and a.get("transportId"):
                            return a
        finally:
            remove()

    async def stop_app(self, session_id: str, *, wait: bool = True, timeout: float = 5.0) -> None:
        payload = {"type": "STOP", "sessionId": session_id}
        if not wait:
            await self.send(NS_RECEIVER, dict(payload, requestId=self.next_id()))
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await self.request(NS_RECEIVER, payload, timeout=timeout)

    # ---- internals ---------------------------------------------------------------------
    def _mark_closed(self, reason: str) -> None:
        if not self.closed.is_set():
            self.close_reason = reason
            self.closed.set()
            log.info("cast channel closed: %s", reason)

    async def _rx_loop(self) -> None:
        try:
            while True:
                (ln,) = struct.unpack(">I", await self._reader.readexactly(4))
                if ln > MAX_MESSAGE:
                    raise CastError(f"message too large ({ln})")
                m = decode_message(await self._reader.readexactly(ln))
                self._last_rx = time.monotonic()
                p = m.json()
                if m.namespace == NS_HEARTBEAT:
                    if p and p.get("type") == "PING":
                        with contextlib.suppress(CastError):
                            await self.send(NS_HEARTBEAT, {"type": "PONG"}, m.source_id)
                    continue
                if m.namespace == NS_CONNECTION and p and p.get("type") == "CLOSE":
                    log.info("receiver closed virtual connection %s", m.source_id)
                    self._connected.discard(m.source_id)
                log.debug("RX %s <- %s %s", m.namespace, m.source_id, (m.payload_utf8 or "<binary>")[:2000])
                for cb in list(self._listeners):
                    try:
                        cb(m, p)
                    except Exception:  # noqa: BLE001
                        log.exception("cast listener failed")
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            self._mark_closed(f"connection lost: {e!r}")
        except Exception as e:  # noqa: BLE001
            log.exception("cast rx loop failed")
            self._mark_closed(f"rx error: {e!r}")

    async def _hb_loop(self) -> None:
        try:
            while not self.closed.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if time.monotonic() - self._last_rx > HEARTBEAT_TIMEOUT:
                    self._mark_closed("heartbeat timeout")
                    break
                with contextlib.suppress(CastError):
                    await self.send(NS_HEARTBEAT, {"type": "PING"})
        except asyncio.CancelledError:
            pass
        finally:
            if self.closed.is_set():
                self._abort_transport()

    def _abort_transport(self) -> None:
        with contextlib.suppress(Exception):
            self._writer.close()

    async def close(self, graceful: bool = True) -> None:
        if graceful and not self.closed.is_set():
            for dest in list(self._connected)[::-1]:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.send(NS_CONNECTION, {"type": "CLOSE"}, dest), 1.0)
        self._mark_closed(self.close_reason or "closed by sender")
        for t in (self._rx_task, self._hb_task):
            if t and not t.done():
                t.cancel()
        for t in (self._rx_task, self._hb_task):
            if t:
                with contextlib.suppress(BaseException):
                    await t
        self._abort_transport()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)


async def get_running_app(host: str, port: int = 8009, timeout: float = 8.0,
                          pin_key: Optional[str] = None) -> Optional[dict]:
    """Convenience: open a channel, query the running app, close."""
    ch = await CastChannel.open(host, port, timeout=timeout, heartbeat=False, pin_key=pin_key)
    try:
        return await ch.get_running_app(timeout)
    finally:
        await ch.close()


__all__ = [
    "CastChannel", "CastMessage", "CastError", "ChannelClosed", "TvCertChangedError", "forget_tv_cert",
    "check_cert_pin", "cert_fingerprint", "known_tvs_path", "TvAuthFailedError", "DeviceIdentity",
    "check_receiver_identity", "verify_auth_response", "configure_dev_trust", "dev_trust_active", "set_dev_roots",
    "dev_roots", "cast_trust_roots", "verify_device_chain", "NS_DEVICEAUTH", "encode_message", "decode_message",
    "frame_message", "get_running_app", "MIRRORING_APP_ID", "NS_RECEIVER", "NS_WEBRTC", "NS_MEDIA",
    "NS_REMOTING", "NS_CONNECTION", "NS_HEARTBEAT", "RECEIVER_ID",
]
