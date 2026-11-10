"""Persistent JSON settings in ~/.config/airlinx/config.json."""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3

DEFAULTS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "protocol": "auto",            # "auto" | "cast" | "dlna"
    "last_device_id": None,
    "manual_devices": [],          # list of IPs added by hand
    "close_to_tray": True,
    "start_hidden": False,
    "autostart": False,
    "notifications": True,
    "mirror": {
        "preset": "game",
        "max_height": 1080,
        "fps": 60,
        "bitrate": 12_000_000,
        "audio": True,
        "target_delay_ms": 100,
        "codec": "h264",           # "auto" | "h264" | "hevc" (auto = HEVC when the TV and ffmpeg can)
    },
    "audio": {                     # what the TV hears while mirroring (switch: mirror.audio)
        "source": "system",        # "system" | "mic" | "system_mic" | "app"
        "mic": "default",          # pulse source name or "default" (@DEFAULT_SOURCE@)
        "mic_name": None,          # display label of the chosen mic
        "app": None,               # stable app key (application.process.binary / application.name)
        "app_name": None,          # display label of the chosen app
    },
    "last_source_id": None,
    "media": {"volume": 1.0, "auto_advance": True},
    "ui": {},                      # free-form UI prefs (theme, last tab, ...)
}

ALLOWED_PROTOCOLS = ("auto", "cast", "dlna")
AUDIO_SOURCES = ("system", "mic", "system_mic", "app")
CODECS = ("auto", "h264", "hevc")


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "airlinx"


def _deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _reset_wrong_types(data: dict, defaults: dict, prefix: str = "") -> None:
    """Valid JSON with a value of the wrong kind (dict vs not, number vs not) would crash the app at
    start: put the default back for that key only."""
    for k, dv in defaults.items():
        if k not in data:
            continue
        v = data[k]
        if isinstance(dv, dict):
            if isinstance(v, dict):
                _reset_wrong_types(v, dv, f"{prefix}{k}.")
                continue
        elif not _is_number(dv) or _is_number(v):
            continue
        log.warning("config: %s%s has the wrong type (%r); using the default", prefix, k, v)
        data[k] = copy.deepcopy(dv)


def _migrate(data: dict) -> dict:
    ver = data.get("version", 0)
    if not isinstance(ver, int) or ver < 1:
        data["version"] = ver = 1
    if ver < 2:  # v2: audio source selection; old configs keep the previous behaviour (system sound)
        audio = data.get("audio") if isinstance(data.get("audio"), dict) else {}
        audio.setdefault("source", "system")
        data["audio"] = audio
        data["version"] = ver = 2
    if ver < 3:  # v3: video codec; existing users stay on H.264 whatever the default becomes later
        mirror = data.get("mirror") if isinstance(data.get("mirror"), dict) else {}
        mirror.setdefault("codec", "h264")
        data["mirror"] = mirror
        data["version"] = 3
    return data


class Settings:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else config_dir() / "config.json"
        self._lock = threading.RLock()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # --- persistence -----------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("config root is not an object")
                self._data = _deep_merge(DEFAULTS, _migrate(raw))
                self._data["version"] = SCHEMA_VERSION
            except FileNotFoundError:
                self._data = copy.deepcopy(DEFAULTS)
            except Exception as e:
                log.warning("config %s unreadable (%s); using defaults", self.path, e)
                try:
                    self.path.replace(self.path.with_suffix(".json.bak"))
                except OSError:
                    pass
                self._data = copy.deepcopy(DEFAULTS)
            self._sanitize()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config.", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def _sanitize(self) -> None:
        _reset_wrong_types(self._data, DEFAULTS)
        if self._data.get("protocol") not in ALLOWED_PROTOCOLS:
            self._data["protocol"] = "auto"
        mirror = self._data.get("mirror")
        if isinstance(mirror, dict) and mirror.get("codec") not in CODECS:
            mirror["codec"] = DEFAULTS["mirror"]["codec"]
        if not isinstance(self._data.get("manual_devices"), list):
            self._data["manual_devices"] = []
        audio = self._data.get("audio")
        if not isinstance(audio, dict):
            audio = self._data["audio"] = copy.deepcopy(DEFAULTS["audio"])
        if audio.get("source") not in AUDIO_SOURCES:
            audio["source"] = "system"
        if not isinstance(audio.get("mic"), str) or not audio["mic"]:
            audio["mic"] = "default"
        for k in ("app", "app_name", "mic_name"):
            if not isinstance(audio.get(k), str) or not audio[k]:
                audio[k] = None

    # --- access ------------------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self._data
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    return default
                cur = cur[part]
            return copy.deepcopy(cur)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        parts = key.split(".")
        patch: dict[str, Any] = {}
        node = patch
        for p in parts[:-1]:
            node[p] = {}
            node = node[p]
        node[parts[-1]] = value
        self.update(patch, save=save)

    def update(self, patch: dict[str, Any], save: bool = True) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise TypeError("patch must be an object")
        patch = {k: v for k, v in patch.items() if k != "version"}
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            self._sanitize()
            if save:
                self.save()
            return copy.deepcopy(self._data)
