"""Throw-away PKI of a virtual Cast receiver.

A real Cast TV owns (1) a short-lived self-signed TLS certificate and (2) a long-lived *device
certificate* issued by Google (root -> ICA -> device) whose key signs ``nonce || TLS certificate``
in the device-auth reply. The virtual TV mirrors that shape with a **test** root CA that is
generated in memory on every start. Its private keys never touch the disk; only the public root
certificate can be exported (PEM) so that AirLinx can be told to trust it in developer mode
(see ``airlinx.core.cast.castv2.configure_dev_trust``).
"""
from __future__ import annotations

import datetime
import ssl
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

UTC = datetime.timezone.utc


def _name(cn: str, org: str = "AirLinx Virtual TV (TEST ONLY)") -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject: x509.Name, issuer: x509.Name, pub, sign_key, *, days: float, start_days: float = -1.0,
          ca: bool = False, pathlen: Optional[int] = None, digital_signature: bool = False,
          san_ips: tuple[str, ...] = ()) -> x509.Certificate:
    import ipaddress

    now = datetime.datetime.now(UTC)
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
         .serial_number(x509.random_serial_number())
         .not_valid_before(now + datetime.timedelta(days=start_days))
         .not_valid_after(now + datetime.timedelta(days=start_days + days)))
    if ca:
        b = b.add_extension(x509.BasicConstraints(ca=True, path_length=pathlen), critical=True)
        b = b.add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                          crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
    elif digital_signature:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                          crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
    if san_ips:
        b = b.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(i)) for i in san_ips]),
                            critical=False)
    return b.sign(sign_key, hashes.SHA256())


def der(c: x509.Certificate) -> bytes:
    return c.public_bytes(serialization.Encoding.DER)


@dataclass
class DeviceIdentity:
    """Device certificate chain + key (what the TV signs the device-auth reply with)."""
    key: rsa.RSAPrivateKey
    cert: x509.Certificate
    ica: x509.Certificate

    @property
    def subject(self) -> str:
        return self.cert.subject.rfc4514_string()


class TestPki:
    """Root -> ICA -> device certificate, all generated now, all in memory."""

    __test__ = False  # not a pytest class

    def __init__(self, device_cn: str = "VTV-0001") -> None:
        self.root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.root_name = _name("AirLinx Virtual TV Test Root CA (NOT A REAL CAST ROOT)")
        self.root = _cert(self.root_name, self.root_name, self.root_key.public_key(), self.root_key,
                          days=3650, ca=True, pathlen=1)
        self.ica_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ica_name = _name("AirLinx Virtual TV Test ICA")
        self.ica = _cert(self.ica_name, self.root_name, self.ica_key.public_key(), self.root_key, days=3650,
                         ca=True, pathlen=0)
        self.identity = self.new_identity(device_cn)

    def new_identity(self, device_cn: str) -> DeviceIdentity:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = _cert(_name(device_cn), self.ica_name, key.public_key(), self.ica_key, days=3650,
                     digital_signature=True)
        return DeviceIdentity(key, cert, self.ica)

    @property
    def root_pem(self) -> bytes:
        """Public part only: safe to write to disk, useless for impersonation without the keys."""
        return self.root.public_bytes(serialization.Encoding.PEM)

    def write_root_pem(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.root_pem)
        return path


@dataclass
class TlsIdentity:
    """Self-signed, short-lived TLS certificate of the receiver (Cast rule: valid now, <= 4 days)."""
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate
    _tmp: list = field(default_factory=list, repr=False)

    @classmethod
    def generate(cls, cn: str, ips: tuple[str, ...] = (), days: float = 3.0) -> "TlsIdentity":
        key = ec.generate_private_key(ec.SECP256R1())
        name = _name(cn)
        return cls(key, _cert(name, name, key.public_key(), key, days=days, start_days=-0.05, san_ips=ips))

    @property
    def der(self) -> bytes:
        return der(self.cert)

    def server_context(self) -> ssl.SSLContext:
        """ssl needs files for the chain; they live in a private temp dir for the load only."""
        with tempfile.TemporaryDirectory(prefix="vtv-tls-") as d:
            cp, kp = Path(d) / "c.pem", Path(d) / "k.pem"
            cp.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))
            kp.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
            kp.chmod(0o600)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cp, kp)
        return ctx
