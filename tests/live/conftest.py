"""Live tests against a real TV. Opt-in: `AIRLINX_TV_IP=<tv-ip> pytest -m tv tests/live`.

Safety rules (same as scripts/smoke_*.py):
- never force: when something is already running on the TV (or any ffmpeg is running on this PC), skip;
- every run ends with STOP and an idle TV, asserted;
- the TV pin file is a temp file, so the run also proves device-auth from scratch and never edits the real pins.
"""
import os

import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "/tests/live/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.tv)


@pytest.fixture(scope="session")
def tv_ip():
    ip = os.environ.get("AIRLINX_TV_IP")
    if not ip:
        pytest.skip("AIRLINX_TV_IP not set")
    return ip


@pytest.fixture(scope="session")
def pins_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("pins") / "known_tvs.json"
    old = os.environ.get("AIRLINX_KNOWN_TVS")
    os.environ["AIRLINX_KNOWN_TVS"] = str(path)
    yield path
    if old is None:
        os.environ.pop("AIRLINX_KNOWN_TVS", None)
    else:
        os.environ["AIRLINX_KNOWN_TVS"] = old
