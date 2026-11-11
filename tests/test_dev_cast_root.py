"""The developer-mode trust hook for a TEST Cast root CA (castv2.configure_dev_trust & friends).

This hook lets a virtual TV pass device-auth, so it must be impossible to switch on by accident and must
never weaken the production path. These tests pin the safety properties: off by default, only through
AIRLINX_DEV=1 plus a root file, never through settings, loud, visible in AppState, isolated pins.
"""
import asyncio
import datetime
import json
import logging
import os
import re
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from airlinx.controller import Controller
from airlinx.core.cast import castv2 as cv
from airlinx.settings import Settings

from test_cast_castv2 import FakeCastPki, FakeReceiver, _name, ctx_for, der, make_cert

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_hook(tmp_path, monkeypatch):
    cv._dev_roots.clear()
    monkeypatch.setenv("AIRLINX_KNOWN_TVS", str(tmp_path / "known_tvs.json"))
    monkeypatch.delenv("AIRLINX_DEV", raising=False)
    monkeypatch.delenv("AIRLINX_DEV_CAST_ROOT", raising=False)
    monkeypatch.setattr(cv, "DEVICE_AUTH_TIMEOUT", 0.5)
    yield
    cv._dev_roots.clear()


def pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def write_root(tmp_path, cert=None, name="root.pem") -> Path:
    cert = cert or FakeCastPki().root
    p = tmp_path / name
    p.write_bytes(pem(cert))
    return p


def controller(tmp_path) -> Controller:
    return Controller(Settings(tmp_path / "config.json"))


# ------------------------------------------------------------------ off by default
def test_off_by_default_roots_are_exactly_the_google_ones(tmp_path):
    assert not cv.dev_trust_active()
    assert cv.cast_trust_roots() == cv.production_roots() and len(cv.cast_trust_roots()) == 2
    assert controller(tmp_path).state().dev_cast_root is False
    assert cv.configure_dev_trust({}) == []


def test_a_test_root_is_not_trusted_without_the_hook(tmp_path):
    pki = FakeCastPki()
    tls = make_cert("tv", ec.generate_private_key(ec.SECP256R1()), days=2)
    resp = cv.parse_auth_response(pki.answer(cv.build_auth_challenge(b"n" * 16), der(tls)))
    with pytest.raises(cv.DeviceAuthFailed, match="does not chain to a Cast root"):
        cv.verify_auth_response(resp, der(tls), b"n" * 16)


# ------------------------------------------------------------------ the environment gate
def test_root_without_AIRLINX_DEV_is_ignored_with_a_warning(tmp_path, caplog):
    p = write_root(tmp_path)
    with caplog.at_level(logging.WARNING, logger="airlinx.cast.castv2"):
        assert cv.configure_dev_trust({"AIRLINX_DEV_CAST_ROOT": str(p)}) == []
    assert not cv.dev_trust_active()
    assert "ignoring it" in caplog.text


@pytest.mark.parametrize("flag", ["0", "true", "yes", "", " 1", "2"])
def test_only_the_exact_value_1_enables_it(tmp_path, flag):
    p = write_root(tmp_path)
    assert cv.configure_dev_trust({"AIRLINX_DEV": flag, "AIRLINX_DEV_CAST_ROOT": str(p)}) == []
    assert not cv.dev_trust_active()


def test_AIRLINX_DEV_alone_does_nothing(tmp_path):
    assert cv.configure_dev_trust({"AIRLINX_DEV": "1"}) == []
    assert not cv.dev_trust_active()


def test_enabled_with_both_variables_is_loud_and_visible_in_appstate(tmp_path, caplog):
    pki = FakeCastPki()
    p = write_root(tmp_path, pki.root)
    with caplog.at_level(logging.WARNING, logger="airlinx.cast.castv2"):
        subjects = cv.configure_dev_trust({"AIRLINX_DEV": "1", "AIRLINX_DEV_CAST_ROOT": str(p)})
    assert subjects == ["CN=Test Cast Root CA"] and cv.dev_trust_active()
    rec = [r for r in caplog.records if "DEVELOPER MODE" in r.getMessage()]
    assert rec and rec[0].levelno == logging.WARNING and "NEVER" in rec[0].getMessage()
    assert controller(tmp_path).state().dev_cast_root is True
    assert cv.cast_trust_roots()[-1] == pki.root and len(cv.cast_trust_roots()) == 3


def test_reconfiguring_without_the_variables_switches_it_off_again(tmp_path):
    p = write_root(tmp_path)
    cv.configure_dev_trust({"AIRLINX_DEV": "1", "AIRLINX_DEV_CAST_ROOT": str(p)})
    assert cv.dev_trust_active()
    cv.configure_dev_trust({})
    assert not cv.dev_trust_active()


# ------------------------------------------------------------------ a bad setup never enables anything
def _selfsigned(cn="Some Root", *, ca=True, days=30, start=-1.0, key=None):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return make_cert(cn, key, ca=ca, pathlen=1, days=days, start_offset=start), key


def test_garbage_missing_empty_and_oversized_files_leave_it_off(tmp_path, caplog):
    env = {"AIRLINX_DEV": "1"}
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    big = tmp_path / "big.pem"
    big.write_bytes(b"A" * (cv.MAX_DEV_ROOT_PEM + 1))
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    for path in (tmp_path / "missing.pem", junk, big, empty, tmp_path):
        with caplog.at_level(logging.ERROR, logger="airlinx.cast.castv2"):
            assert cv.configure_dev_trust(dict(env, AIRLINX_DEV_CAST_ROOT=str(path))) == []
        assert not cv.dev_trust_active()


def test_a_leaf_certificate_is_not_accepted_as_a_root(tmp_path):
    leaf, _ = _selfsigned(ca=False)
    assert cv.configure_dev_trust({"AIRLINX_DEV": "1", "AIRLINX_DEV_CAST_ROOT": str(write_root(tmp_path, leaf))}) == []
    assert not cv.dev_trust_active()


def test_a_root_that_is_not_self_signed_is_rejected(tmp_path):
    pki = FakeCastPki()
    for c in (pki.ica, pki.dev):          # signed by somebody else
        assert cv.configure_dev_trust({"AIRLINX_DEV": "1", "AIRLINX_DEV_CAST_ROOT": str(write_root(tmp_path, c))}) == []
        assert not cv.dev_trust_active()


def test_an_expired_or_not_yet_valid_root_is_rejected(tmp_path):
    for days, start in ((1, -10), (30, 5)):
        c, _ = _selfsigned(days=days, start=start)
        assert cv.configure_dev_trust({"AIRLINX_DEV": "1",
                                       "AIRLINX_DEV_CAST_ROOT": str(write_root(tmp_path, c))}) == []
        assert not cv.dev_trust_active()


def test_a_root_may_not_reuse_the_subject_of_a_real_cast_root():
    real = cv.production_roots()[0]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    b = (x509.CertificateBuilder().subject_name(real.subject).issuer_name(real.subject).public_key(key.public_key())
         .serial_number(1).not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
         .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True))
    fake = b.sign(key, hashes.SHA256())
    with pytest.raises(ValueError, match="real Cast root"):
        cv.set_dev_roots([fake])
    assert not cv.dev_trust_active()


def test_too_many_roots_are_rejected(tmp_path):
    certs = [_selfsigned(f"R{i}")[0] for i in range(cv.MAX_DEV_ROOTS + 1)]
    with pytest.raises(ValueError):
        cv.set_dev_roots(certs)
    assert not cv.dev_trust_active()


# ------------------------------------------------------------------ nothing persistent can switch it on
def test_settings_and_config_json_cannot_enable_it(tmp_path):
    p = write_root(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"version": 1, "dev": True, "dev_cast_root": str(p), "AIRLINX_DEV": "1",
                               "AIRLINX_DEV_CAST_ROOT": str(p), "cast": {"dev_root": str(p)}}))
    ctl = Controller(Settings(cfg))
    ctl.settings.set("dev_cast_root", True)
    ctl.settings.set("dev.cast_root", str(p))
    assert ctl.state().dev_cast_root is False and not cv.dev_trust_active()
    assert cv.cast_trust_roots() == cv.production_roots()


def test_the_environment_is_read_only_by_the_explicit_startup_call(tmp_path, monkeypatch):
    """Importing castv2 or building a Controller must not look at the environment by itself."""
    p = write_root(tmp_path)
    monkeypatch.setenv("AIRLINX_DEV", "1")
    monkeypatch.setenv("AIRLINX_DEV_CAST_ROOT", str(p))
    assert not cv.dev_trust_active()
    assert controller(tmp_path).state().dev_cast_root is False
    cv.configure_dev_trust()
    assert cv.dev_trust_active()


def test_no_shipped_launcher_or_service_file_sets_the_dev_variables():
    """install.sh writes the launcher and .desktop files: they must never export the dev switches."""
    hits = []
    for f in [ROOT / "install.sh", *(ROOT / "data").rglob("*.desktop"), *(ROOT / "data").rglob("*.service"),
              *(ROOT / "scripts").rglob("*")]:
        if f.is_file():
            try:
                if re.search(r"AIRLINX_DEV(_CAST_ROOT)?\s*=", f.read_text("utf-8")):
                    hits.append(str(f.relative_to(ROOT)))
            except UnicodeDecodeError:
                pass
    assert hits == []


def test_airlinx_app_calls_configure_dev_trust_once_at_startup():
    src = (ROOT / "airlinx" / "app.py").read_text("utf-8")
    assert src.count("configure_dev_trust()") == 1


# ------------------------------------------------------------------ what it does once on
def test_device_auth_then_passes_for_the_test_chain_and_marks_the_identity_dev(tmp_path):
    pki = FakeCastPki()
    tls = make_cert("tv", ec.generate_private_key(ec.SECP256R1()), days=2)
    nonce = b"n" * 16
    resp = cv.parse_auth_response(pki.answer(cv.build_auth_challenge(nonce), der(tls)))
    with cv.dev_roots([pki.root]):
        ident = cv.verify_auth_response(resp, der(tls), nonce)
        assert ident.dev is True and ident.subject == "CN=TEST-TV FA8F00000001"
    with pytest.raises(cv.DeviceAuthFailed):                        # context left: back to production only
        cv.verify_auth_response(resp, der(tls), nonce)


def test_dev_roots_context_restores_the_previous_state(tmp_path):
    a, b = FakeCastPki(), FakeCastPki()
    cv.set_dev_roots([a.root])
    with cv.dev_roots([b.root]):
        assert cv.cast_trust_roots()[-1] == b.root
    assert cv.cast_trust_roots()[-1] == a.root and len(cv.cast_trust_roots()) == 3


def test_pins_of_dev_identities_go_to_a_separate_file_and_never_the_real_one(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRLINX_KNOWN_TVS")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cv.known_tvs_path().name == "known_tvs.json"
    pki = FakeCastPki()
    with cv.dev_roots([pki.root]):
        assert cv.known_tvs_path() == tmp_path / "xdg" / "airlinx" / "known_tvs.dev.json"
        ident = cv.DeviceIdentity("CN=VTV", "aa", "CN=ICA", False, "bb", dev=True)
        assert cv.check_receiver_identity("tv-1", b"\x30\x00", ident) == "pinned"
        pin = json.loads(cv.known_tvs_path().read_text())["tv-1"]
        assert pin["dev"] is True and pin["auth"] == "device"
    assert not (tmp_path / "xdg" / "airlinx" / "known_tvs.json").exists()


def test_a_pin_made_for_a_test_identity_does_not_accept_a_real_one_and_vice_versa(tmp_path):
    """Same subject, different trust domain: treated as a changed identity (defence in depth)."""
    dev = cv.DeviceIdentity("CN=SAME", "aa", "CN=ICA", False, "bb", dev=True)
    real = cv.DeviceIdentity("CN=SAME", "aa", "CN=ICA", False, "bb", dev=False)
    assert cv.check_receiver_identity("a", b"\x30\x00", dev) == "pinned"
    with pytest.raises(cv.TvCertChangedError):
        cv.check_receiver_identity("a", b"\x30\x00", real)
    assert cv.check_receiver_identity("b", b"\x30\x00", real) == "pinned"
    with pytest.raises(cv.TvCertChangedError):
        cv.check_receiver_identity("b", b"\x30\x00", dev)
    assert cv.check_receiver_identity("a", b"\x30\x00", dev) == "match"


def test_channel_open_end_to_end_only_with_the_hook(tmp_path):
    """A receiver whose device cert chains to a test root: refused by default, accepted in dev mode."""
    pki = FakeCastPki()
    key = ec.generate_private_key(ec.SECP256R1())
    tls = make_cert("tv", key, days=2, start_offset=-0.5)
    ctx = ctx_for(tmp_path, tls, key, "tv")

    async def main():
        fr = FakeReceiver(ssl_ctx=ctx, auth=pki, tls_der=der(tls))
        port = await fr.start()
        try:
            with pytest.raises(cv.TvAuthFailedError):
                await cv.CastChannel.open("127.0.0.1", port, heartbeat=False, pin_key="x")
            assert [k for k in fr.received if k[2].get("type") != "_BINARY"] == []     # nothing but the challenge
            with cv.dev_roots([pki.root]):
                ch = await cv.CastChannel.open("127.0.0.1", port, heartbeat=False, pin_key="x")
                assert ch.identity is not None and ch.identity.dev and ch.auth_result == "pinned"
                await ch.close()
        finally:
            await fr.close()
    asyncio.run(asyncio.wait_for(main(), 15))
