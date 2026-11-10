"""Crash recovery for audio routing must survive a damaged state file and never touch what is not ours.
A crash can leave the user's apps muted or pinned to a null sink, so this path has to be bulletproof."""
import json

import pytest

from airlinx.core import audio as au

from test_audio import ANALOG, HDMI, FakePulse, run, _private_runtime_dir  # noqa: F401 (fixture re-export)


def crashed_pulse():
    """A pulse server where an earlier AirLinx run left its null sink behind with Firefox moved into it."""
    pulse = FakePulse()
    pulse.modules[7] = ("module-null-sink", "sink_name=airlinx_app sink_properties=x")
    pulse.sinks[90] = "airlinx_app"
    pulse.owner[90] = 7
    pulse.inputs[300]["sink"] = 90
    return pulse


BUG = pytest.mark.xfail(strict=True, reason="BUG: cleanup_orphans() calls .items() / iterates the state file values without checking "
                       "their types, so a wrong-typed state file raises at start-up instead of just unloading our sink")
GOOD = {"origins": {"300": ANALOG}, "pinned": []}


@pytest.mark.parametrize("content", [
    "",                                  # empty file
    "{not json",                         # truncated write
    "null", "42", '"text"', "[1, 2]",    # valid JSON, wrong root type
    pytest.param(json.dumps({"origins": [1, 2]}), marks=BUG),          # wrong container types
    pytest.param(json.dumps({"origins": "x", "pinned": 5}), marks=BUG),
    json.dumps({"origins": {"abc": 1, "": 2, "-5": 3}, "pinned": ["x", None, 1.5]}),
    json.dumps({"origins": {"300": "not-a-sink"}, "pinned": [300]}),
    json.dumps({"origins": {"300": None}}),
    "\x00\x01\x02",
])
def test_cleanup_survives_any_state_file_content_and_still_unloads(tmp_path, content):
    pulse = crashed_pulse()
    state = tmp_path / "routes.json"
    state.write_text(content)
    n = run(au.cleanup_orphans(pulse, state_path=state))
    assert n == 1 and 7 not in pulse.modules, "our null sink must be unloaded whatever the state file says"
    assert pulse.sink_of(300) in (ANALOG, HDMI), "the app must not stay on a sink that no longer exists"
    assert not state.exists()


def test_cleanup_restores_saved_origin_sink(tmp_path):
    pulse = crashed_pulse()
    state = tmp_path / "routes.json"
    state.write_text(json.dumps({"origins": {"300": HDMI}, "pinned": []}))   # Firefox was on HDMI
    run(au.cleanup_orphans(pulse, state_path=state))
    assert pulse.sink_of(300) == HDMI


def test_cleanup_only_touches_airlinx_prefixed_sinks_and_their_streams(tmp_path):
    pulse = crashed_pulse()
    pulse.modules[8] = ("module-null-sink", "sink_name=my_own_null_sink")
    pulse.sinks[91] = "my_own_null_sink"
    pulse.owner[91] = 8
    pulse.inputs[302]["sink"] = 91                       # Spotify on the user's own null sink
    run(au.cleanup_orphans(pulse, state_path=tmp_path / "none.json"))
    assert 8 in pulse.modules and pulse.sink_of(302) == "my_own_null_sink"
    moves = [c for c in pulse.calls if "move-sink-input" in c]
    assert all(c[2] == "300" for c in moves), moves


def test_cleanup_does_not_leave_default_sink_on_our_sink(tmp_path):
    pulse = crashed_pulse()
    pulse.default_sink = "airlinx_app"
    run(au.cleanup_orphans(pulse, state_path=tmp_path / "none.json"))
    assert pulse.default_sink in (ANALOG, HDMI)


def test_cleanup_without_pactl_is_a_quiet_noop(tmp_path):
    async def broken(argv):
        raise au.PactlError("pactl not found")
    state = tmp_path / "routes.json"
    state.write_text(json.dumps(GOOD))
    assert run(au.cleanup_orphans(broken, state_path=state)) == 0
    assert state.exists(), "keep the state: the next start can still use it"


def test_cleanup_is_idempotent(tmp_path):
    pulse = crashed_pulse()
    assert run(au.cleanup_orphans(pulse, state_path=tmp_path / "r.json")) == 1
    pulse.calls.clear()
    assert run(au.cleanup_orphans(pulse, state_path=tmp_path / "r.json")) == 0
    assert not any(c[1] in ("unload-module", "move-sink-input") for c in pulse.calls)


def test_cleanup_unloads_module_even_when_one_stream_cannot_be_moved(tmp_path):
    pulse = crashed_pulse()
    pulse.inputs[301]["sink"] = 90
    pulse.fail["move-sink-input"] = 1                    # first move fails
    n = run(au.cleanup_orphans(pulse, state_path=tmp_path / "r.json"))
    assert n == 1 and 7 not in pulse.modules


# ----------------------------------------------------------------------------- icons

def test_icon_lookup_rejects_path_tricks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    au._ICON_CACHE.clear()
    secret = tmp_path / "secret.svg"
    secret.write_text("<svg>secret</svg>")
    for bad in ("", "../secret", "/etc/passwd", ".hidden", "a/b", "../../../../etc/passwd"):
        assert au.icon_data_url(bad) is None


def test_icon_lookup_skips_oversized_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(au.Path, "home", classmethod(lambda cls: tmp_path))
    d = tmp_path / ".local/share/icons/hicolor/48x48/apps"
    d.mkdir(parents=True)
    (d / "big.png").write_bytes(b"x" * (256 * 1024 + 1))
    (d / "ok.png").write_bytes(b"\x89PNG")
    au._ICON_CACHE.clear()
    assert au.icon_data_url("big") is None
    assert au.icon_data_url("ok").startswith("data:image/png;base64,")
