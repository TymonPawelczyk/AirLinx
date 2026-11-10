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
