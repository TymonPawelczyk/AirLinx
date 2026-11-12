"""Settings file safety: migration from older versions, hand-edited or damaged files, atomic saves.
(Value types and sanitising rules live in test_shell_settings.py.)"""
import json
import os
import threading

import pytest

from airlinx import settings as st
from airlinx.settings import DEFAULTS, SCHEMA_VERSION, Settings


def write(path, obj):
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return path


@pytest.mark.parametrize("version", [None, 0, -3, "2", "x", 1.5, [], {}])
def test_garbage_version_is_treated_as_v1_and_migrated(tmp_path, version):
    p = write(tmp_path / "c.json", {"version": version, "last_device_id": "d1"})
    s = Settings(p)
    assert s.get("version") == SCHEMA_VERSION
    assert s.get("audio.source") == "system" and s.get("last_device_id") == "d1"


def test_unversioned_file_gets_every_default(tmp_path):
    s = Settings(write(tmp_path / "c.json", {}))
    assert s.as_dict() == DEFAULTS


def test_migration_keeps_an_existing_audio_choice(tmp_path):
    p = write(tmp_path / "c.json", {"version": 1, "audio": {"source": "mic", "mic": "alsa_input.x"}})
    s = Settings(p)
    assert s.get("audio.source") == "mic" and s.get("audio.mic") == "alsa_input.x"


def test_migration_survives_audio_of_the_wrong_type(tmp_path):
    s = Settings(write(tmp_path / "c.json", {"version": 1, "audio": "loud"}))
    assert s.get("audio.source") == "system" and s.get("audio.mic") == "default"


def test_unknown_keys_survive_a_load_save_roundtrip(tmp_path):
    p = write(tmp_path / "c.json", {"version": SCHEMA_VERSION, "future_feature": {"on": True}, "ui": {"theme": "light"}})
    s = Settings(p)
    s.set("media.volume", 0.3)
    data = json.loads(p.read_text())
    assert data["future_feature"] == {"on": True} and data["ui"]["theme"] == "light" and data["media"]["volume"] == 0.3


def test_file_from_a_newer_version_is_loaded_not_discarded(tmp_path):
    p = write(tmp_path / "c.json", {"version": SCHEMA_VERSION + 5, "protocol": "dlna"})
    s = Settings(p)
    assert s.get("protocol") == "dlna"
    assert not (tmp_path / "c.json.bak").exists()


@pytest.mark.parametrize("content", ["", "[]", "null", "12", '"s"', "{broken", "\x00\x00", "{'single': 'quotes'}"])
def test_unreadable_or_wrong_root_is_backed_up_and_defaults_used(tmp_path, content):
    p = write(tmp_path / "c.json", content)
    s = Settings(p)
    assert s.as_dict() == DEFAULTS
    assert (tmp_path / "c.json.bak").read_text() == content, "the user's file must be kept, not deleted"
    assert not p.exists()


def test_non_utf8_file_is_backed_up(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b'{"x": "\xff\xfe"}')
    s = Settings(p)
    assert s.as_dict() == DEFAULTS and (tmp_path / "c.json.bak").exists()


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "sub" / "c.json"
    s = Settings(p)
    for i in range(5):
        s.set("media.volume", i / 10)
    assert sorted(x.name for x in p.parent.iterdir()) == ["c.json"]
    assert json.loads(p.read_text())["media"]["volume"] == 0.4


def test_failed_save_keeps_the_previous_file_intact_and_cleans_up(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    s = Settings(p)
    s.set("protocol", "cast")
    before = p.read_text()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st.os, "replace", boom)
    with pytest.raises(OSError):
        s.set("protocol", "dlna")
    monkeypatch.undo()
    assert p.read_text() == before
    assert [x.name for x in tmp_path.iterdir()] == ["c.json"], "temp file must be removed after a failed save"


def test_save_false_does_not_touch_disk(tmp_path):
    p = tmp_path / "c.json"
    s = Settings(p)
    s.set("media.volume", 0.1, save=False)
    assert not p.exists() and s.get("media.volume") == 0.1


def test_default_path_follows_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    s = Settings()
    s.set("protocol", "cast")
    assert (tmp_path / "xdg/airlinx/config.json").exists()


def test_concurrent_updates_do_not_corrupt_the_file(tmp_path):
    p = tmp_path / "c.json"
    s = Settings(p)
    errors = []

    def worker(n):
        try:
            for i in range(25):
                s.set(f"ui.k{n}", i)
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    data = json.loads(p.read_text())
    assert {data["ui"][f"k{n}"] for n in range(6)} == {24}
    assert Settings(p).as_dict() == data


def test_update_rejects_non_object_patch_and_cannot_change_version(tmp_path):
    s = Settings(tmp_path / "c.json")
    with pytest.raises(TypeError):
        s.update(["protocol"])
    s.update({"version": 0})
    assert s.get("version") == SCHEMA_VERSION


def test_get_walks_dotted_keys_through_non_dicts_safely(tmp_path):
    s = Settings(tmp_path / "c.json")
    assert s.get("protocol.deeper", "dflt") == "dflt"
    assert s.get("", "dflt") == "dflt"


# --- value validation (range / enum / kind) -----------------------------------------------------

import math  # noqa: E402

BAD_VALUES = [
    ("media.volume", -0.1), ("media.volume", 1.5), ("media.volume", math.nan), ("media.volume", math.inf),
    ("media.volume", -math.inf), ("media.volume", "loud"), ("media.volume", True), ("media.volume", None),
    ("mirror.fps", 0), ("mirror.fps", -5), ("mirror.fps", 100000), ("mirror.fps", 29.5), ("mirror.fps", math.nan),
    ("mirror.fps", math.inf), ("mirror.fps", True), ("mirror.fps", "60"),
    ("mirror.bitrate", 0), ("mirror.bitrate", -1), ("mirror.bitrate", 10**12), ("mirror.bitrate", math.inf),
    ("mirror.max_height", 0), ("mirror.max_height", 99999), ("mirror.max_height", "1080"),
    ("mirror.target_delay_ms", -1), ("mirror.target_delay_ms", 10**9), ("mirror.target_delay_ms", math.nan),
    ("mirror.preset", "ultra"), ("mirror.preset", 3), ("mirror.preset", None),
    ("mirror.audio", "yes"), ("mirror.audio", 1), ("mirror.audio", None),
    ("media.auto_advance", 0), ("media.auto_advance", "true"),
    ("close_to_tray", 1), ("close_to_tray", "no"), ("start_hidden", None), ("autostart", "x"),
    ("notifications", []),
    ("audio.source", "bluetooth"), ("audio.source", 5), ("audio.source", None),
    ("audio.mic", 5), ("audio.mic", ""), ("audio.app", 7), ("audio.app_name", ["x"]), ("audio.mic_name", 3),
    ("last_device_id", 5), ("last_device_id", ["a"]), ("last_source_id", {"a": 1}),
    ("protocol", 3), ("protocol", None),
]


def _default(key):
    cur = DEFAULTS
    for part in key.split("."):
        cur = cur[part]
    return cur


@pytest.mark.parametrize("key,bad", BAD_VALUES, ids=[f"{k}={v!r}" for k, v in BAD_VALUES])
def test_invalid_value_in_a_loaded_file_resets_to_the_default(tmp_path, caplog, key, bad):
    patch: dict = {}
    node = patch
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = bad
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": SCHEMA_VERSION, **patch}))   # json.dumps emits NaN/Infinity literals
    with caplog.at_level("WARNING"):
        s = Settings(p)
    assert s.get(key) == _default(key)
    assert key in caplog.text, "a reset must be logged"


@pytest.mark.parametrize("key,bad", BAD_VALUES, ids=[f"{k}={v!r}" for k, v in BAD_VALUES])
def test_invalid_value_through_update_resets_only_that_key(tmp_path, key, bad):
    s = Settings(tmp_path / "c.json")
    s.set("mirror.fps", 30) if key != "mirror.fps" else None
    s.set(key, bad)
    assert s.get(key) == _default(key)
    if key != "mirror.fps":
        assert s.get("mirror.fps") == 30
    assert json.loads((tmp_path / "c.json").read_text())      # file stays strictly valid for the next load


@pytest.mark.parametrize("key,good", [
    ("media.volume", 0), ("media.volume", 0.0), ("media.volume", 1), ("media.volume", 0.37),
    ("mirror.fps", 30), ("mirror.fps", 24), ("mirror.bitrate", 28_000_000), ("mirror.max_height", 2160),
    ("mirror.max_height", 1440), ("mirror.preset", "cinema"), ("mirror.target_delay_ms", 0),
    ("mirror.audio", False), ("audio.source", "app"), ("audio.source", "system_mic"), ("audio.source", "mic"),
    ("last_device_id", "uuid:abc"), ("last_device_id", None), ("close_to_tray", False), ("protocol", "cast"),
])
def test_valid_values_are_kept(tmp_path, key, good):
    s = Settings(tmp_path / "c.json")
    s.set(key, good)
    assert Settings(tmp_path / "c.json").get(key) == good


def test_every_shipped_ui_profile_is_accepted(tmp_path):
    s = Settings(tmp_path / "c.json")
    for o in ({"preset": "game", "max_height": 1080, "fps": 60, "bitrate": 12_000_000},
              {"preset": "game", "max_height": 1440, "fps": 60, "bitrate": 18_000_000},
              {"preset": "cinema", "max_height": 2160, "fps": 30, "bitrate": 28_000_000}):
        s.update({"mirror": o})
        assert {k: s.get(f"mirror.{k}") for k in o} == o


def test_integral_float_from_json_is_normalised_to_int(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"mirror": {"fps": 30.0}}')
    v = Settings(p).get("mirror.fps")
    assert v == 30 and isinstance(v, int)
