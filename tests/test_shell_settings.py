import json

from airlinx.settings import DEFAULTS, SCHEMA_VERSION, Settings


def test_defaults_when_missing(tmp_path):
    s = Settings(tmp_path / "c.json")
    assert s.as_dict() == DEFAULTS
    assert not (tmp_path / "c.json").exists()


def test_update_deep_merge_and_persist(tmp_path):
    p = tmp_path / "sub" / "c.json"
    s = Settings(p)
    s.update({"mirror": {"fps": 30}, "protocol": "dlna", "version": 99})
    data = json.loads(p.read_text())
    assert data["mirror"]["fps"] == 30 and data["mirror"]["bitrate"] == 12_000_000
    assert data["protocol"] == "dlna" and data["version"] == SCHEMA_VERSION
    s2 = Settings(p)
    assert s2.get("mirror.fps") == 30 and s2.get("protocol") == "dlna"


def test_set_dotted_and_get_default(tmp_path):
    s = Settings(tmp_path / "c.json")
    s.set("media.volume", 0.5)
    assert s.get("media.volume") == 0.5 and s.get("media.auto_advance") is True
    assert s.get("nope.deep", 7) == 7


def test_invalid_values_sanitized(tmp_path):
    s = Settings(tmp_path / "c.json")
    s.update({"protocol": "airplay", "manual_devices": "x"})
    assert s.get("protocol") == "auto" and s.get("manual_devices") == []


def test_corrupt_file_backed_up(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{nope")
    s = Settings(p)
    assert s.get("protocol") == "auto"
    assert (tmp_path / "c.json.bak").exists()


def test_old_file_migrated_and_new_keys_filled(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"last_device_id": "abc"}))
    s = Settings(p)
    assert s.get("version") == SCHEMA_VERSION and s.get("last_device_id") == "abc"
    assert s.get("mirror.fps") == 60


def test_returned_copies_are_isolated(tmp_path):
    s = Settings(tmp_path / "c.json")
    d = s.as_dict()
    d["mirror"]["fps"] = 1
    assert s.get("mirror.fps") == 60


def test_v1_config_migrates_to_audio_source_system(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"version": 1, "protocol": "cast", "mirror": {"audio": False}}))
    s = Settings(p)
    assert SCHEMA_VERSION >= 2 and s.get("version") == SCHEMA_VERSION
    assert s.get("audio.source") == "system" and s.get("audio.mic") == "default"
    assert s.get("audio.app") is None
    assert s.get("mirror.audio") is False  # the on/off switch is untouched


def test_audio_source_sanitized(tmp_path):
    s = Settings(tmp_path / "c.json")
    s.update({"audio": {"source": "spdif", "mic": 5}})
    assert s.get("audio.source") == "system" and s.get("audio.mic") == "default"
    s.update({"audio": {"source": "app", "app": "firefox", "app_name": "Firefox"}})
    assert s.get("audio.source") == "app" and s.get("audio.app") == "firefox"
    s.update({"audio": {"source": "app", "app": ""}})
    assert s.get("audio.app") is None
