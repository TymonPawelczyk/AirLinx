"""scripts/smoke_mirror.py: --window selection (pure part, no TV)."""
import importlib.util
import pathlib

import pytest

from airlinx.core.models import Source

SPEC = importlib.util.spec_from_file_location(
    "smoke_mirror", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "smoke_mirror.py")
sm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sm)

SRCS = [
    Source("monitor:DP-4", "monitor", "DP-4", (0, 0, 100, 100)),
    Source("window:0x3a00007", "window", "Terminal - zsh", (0, 0, 10, 10), xid=0x3A00007),
    Source("window:0x4c00001", "window", "Firefox - Dokumentacja", (0, 0, 10, 10), xid=0x4C00001),
    Source("window:0x5000002", "window", "Firefox - Poczta", (0, 0, 10, 10), xid=0x5000002),
]


@pytest.mark.parametrize("query", ["0x3a00007", "0X3A00007", str(0x3A00007)])
def test_window_by_xid_hex_or_decimal(query):
    assert sm.pick_window(SRCS, query).xid == 0x3A00007


def test_window_by_title_fragment_is_case_insensitive():
    assert sm.pick_window(SRCS, "terminal").xid == 0x3A00007


def test_ambiguous_title_fragment_is_an_error_listing_candidates():
    with pytest.raises(SystemExit) as e:
        sm.pick_window(SRCS, "firefox")
    assert "Dokumentacja" in str(e.value) and "Poczta" in str(e.value)


def test_unknown_window_is_an_error_listing_windows():
    with pytest.raises(SystemExit) as e:
        sm.pick_window(SRCS, "nope")
    assert "Terminal" in str(e.value)


def test_monitors_are_never_matched_as_windows():
    with pytest.raises(SystemExit):
        sm.pick_window(SRCS, "DP-4")


def test_window_flag_parses():
    assert sm.build_parser().parse_args(["--window", "Terminal"]).window == "Terminal"
    assert sm.build_parser().parse_args([]).window is None
