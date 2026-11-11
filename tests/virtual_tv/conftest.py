"""Virtual-TV end-to-end tests (marker ``vtv``; opt-in: ``pytest -m vtv tests/virtual_tv``).

Everything here talks to a receiver that lives in this process on 127.0.0.1. It never touches the LAN:
no mDNS/SSDP is sent, nothing connects to a real device.
"""
import os

import pytest
import pytest_asyncio

from airlinx.core.cast import castv2

from tools.virtual_tv.cast_receiver import CastReceiver
from tools.virtual_tv.pki import TestPki

import vtvkit


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/tests/virtual_tv/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.vtv)
            if not vtvkit.ffmpeg_ok():
                item.add_marker(pytest.mark.skip(reason="ffmpeg/ffprobe not installed"))


@pytest.fixture(scope="session")
def pki():
    """One throw-away test PKI for the whole run (RSA keygen is the slow part)."""
    return TestPki("VTV-TEST-0001")


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch, pki):
    """Temp pin file and the injected test root (no environment involved: that path has its own tests)."""
    monkeypatch.setenv("AIRLINX_KNOWN_TVS", str(tmp_path / "known_tvs.json"))
    monkeypatch.delenv("AIRLINX_DEV", raising=False)
    monkeypatch.delenv("AIRLINX_DEV_CAST_ROOT", raising=False)
    with castv2.dev_roots([pki.root]):
        yield


@pytest.fixture
def synthetic(monkeypatch):
    vtvkit.patch_capture(monkeypatch)


@pytest_asyncio.fixture
async def make_tv(tmp_path, pki):
    """Factory: ``tv = await make_tv(fail=None, ...)``; every receiver is stopped at teardown."""
    made = []

    async def factory(**kw) -> CastReceiver:
        kw.setdefault("port", 0)
        kw.setdefault("out_dir", tmp_path / f"tv{len(made)}")
        kw.setdefault("pki", pki)
        kw.setdefault("launch_delay", 0.05)
        tv = CastReceiver(**kw)
        await tv.start()
        made.append(tv)
        return tv

    yield factory
    for tv in made:
        await tv.stop()
