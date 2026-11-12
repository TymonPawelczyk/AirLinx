"""One version string (airlinx.__version__) reaches the UI state, the CLI and the SSDP User-Agent."""
import re

import pytest

import airlinx
from airlinx.core.discovery import build_msearch
from airlinx.core.models import AppState


def test_package_version_is_the_release():
    assert airlinx.__version__ == "0.1.1"
    assert re.fullmatch(r"\d+\.\d+\.\d+", airlinx.__version__)


def test_app_state_defaults_to_package_version():
    assert AppState().version == airlinx.__version__


def test_controller_state_reports_package_version(tmp_path):
    from airlinx.controller import Controller
    from airlinx.settings import Settings

    ctl = Controller(Settings(tmp_path / "config.json"))
    assert ctl.state().version == airlinx.__version__


def test_ssdp_user_agent_carries_version():
    msg = build_msearch().decode()
    assert f"AirLinx/{airlinx.__version__}" in msg


def test_cli_version_flag(capsys):
    pytest.importorskip("gi")
    app = pytest.importorskip("airlinx.app")
    with pytest.raises(SystemExit) as e:
        app.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"AirLinx {airlinx.__version__}"
