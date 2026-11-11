"""Shared data models for AirLinx. Contract: docs/ARCHITECTURE.md."""
from __future__ import annotations

import base64
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .. import __version__


@dataclass
class Device:
    id: str
    name: str
    model: str
    ip: str
    cast_port: Optional[int] = None
    dlna_location: Optional[str] = None
    caps: set[str] = field(default_factory=set)  # {"cast_mirror", "cast_media", "dlna"}
    online: bool = True


@dataclass
class Source:
    id: str
    kind: Literal["monitor", "window"]
    name: str
    geometry: tuple[int, int, int, int]  # x, y, w, h
    xid: Optional[int] = None
    thumbnail_png: Optional[bytes] = None


@dataclass
class MirrorOptions:
    preset: Literal["game", "cinema"] = "game"
    max_height: int = 1080
    fps: int = 60
    bitrate: int = 12_000_000
    audio: bool = True
    target_delay_ms: int = 100
    codec: Literal["auto", "h264", "hevc"] = "h264"   # auto = HEVC when the TV and ffmpeg can, else H.264
    # what the TV hears (core/audio.py); persisted in settings "audio", not "mirror"
    audio_source: str = "system"           # "system" | "mic" | "system_mic" | "app"
    audio_mic: str = "default"             # pulse source name or "default"
    audio_app: Optional[str] = None        # app key (application.process.binary / application.name)
    audio_mic_name: Optional[str] = None   # labels for messages
    audio_app_name: Optional[str] = None


@dataclass
class Stats:
    latency_ms: Optional[float]
    fps: float
    bitrate: float
    width: int
    height: int
    rtt_ms: Optional[float]
    loss: float
    protocol: str
    encoder: str
    stages: dict[str, float] = field(default_factory=dict)  # capture/encode/network/tv (ms)


@dataclass
class MediaItem:
    id: str
    path_or_url: str
    title: str
    duration: Optional[float]
    kind: Literal["video", "audio", "image"]
    thumbnail_png: Optional[bytes] = None
    subtitles: list[str] = field(default_factory=list)
    subtitle_track: Optional[str] = None  # selected entry of `subtitles` (path/url) or None = off


@dataclass
class PlaybackState:
    state: Literal["idle", "loading", "playing", "paused", "buffering", "ended", "error"]
    position: float = 0.0
    duration: Optional[float] = None
    volume: Optional[float] = 1.0  # None = player cannot report it reliably
    muted: bool = False
    item_id: Optional[str] = None
    error: Optional[str] = None


MirrorStateName = Literal["idle", "connecting", "waiting_tv", "live", "reconnecting", "stopped", "error"]


@dataclass
class MirrorStatus:
    """Mirroring status as seen by the UI (controller-owned)."""
    state: str = "idle"                      # MirrorStateName
    detail: Optional[str] = None
    device_id: Optional[str] = None
    source_id: Optional[str] = None
    protocol: Optional[str] = None           # "cast" | "dlna" | None
    options: MirrorOptions = field(default_factory=MirrorOptions)
    stats: Optional[Stats] = None
    started_at: Optional[float] = None       # unix time when it went live
    audio: Optional[dict] = None             # {"source": effective, "requested": chosen, "label", "note"}


@dataclass
class AppState:
    """Full application state, emitted as the `state` event and returned by app.getState."""
    devices: list[Device] = field(default_factory=list)
    selected_device_id: Optional[str] = None
    discovering: bool = False
    mode: str = "idle"                       # "idle" | "mirror" | "media"
    protocol: str = "auto"                   # setting: "auto" | "cast" | "dlna"
    mirror: MirrorStatus = field(default_factory=MirrorStatus)
    queue: list[MediaItem] = field(default_factory=list)
    playback: PlaybackState = field(default_factory=lambda: PlaybackState(state="idle"))
    media_protocol: Optional[str] = None     # "cast" | "dlna" while media is active
    settings: dict[str, Any] = field(default_factory=dict)
    version: str = __version__
    encoder: Optional[str] = None            # H.264 encoder for mirroring: "h264_nvenc" | "libx264" | "" (none found)
                                             # | None (not probed yet)
    wayland: bool = False                    # Wayland session: screen capture may be black (files still play)
    dev_cast_root: bool = False              # developer mode: a TEST Cast root CA is trusted (castv2.dev_trust_active);
                                             # never persisted, never set by the settings, shown as a warning in the UI
    hevc_encoder: Optional[str] = None       # H.265 encoder: "hevc_nvenc" | "libx265" | "" (none found) | None (not probed)


class AirlinxError(Exception):
    """Error with a stable code and a Polish user-facing message."""

    def __init__(self, code: str, message_pl: str):
        super().__init__(f"{code}: {message_pl}")
        self.code = code
        self.message_pl = message_pl


class TvBusyError(AirlinxError):
    def __init__(self, app_name: str = ""):
        super().__init__("tv_busy", f"Na telewizorze działa {app_name or 'inna aplikacja'}. Przerwać?")
        self.app_name = app_name


class TvNotFoundError(AirlinxError):
    def __init__(self, detail: str = ""):
        super().__init__("tv_not_found", "Nie znaleziono telewizora w sieci." + (f" ({detail})" if detail else ""))


class PermissionPendingError(AirlinxError):
    def __init__(self) -> None:
        super().__init__("permission_pending", "Zaakceptuj połączenie na telewizorze.")


def to_json(obj: Any) -> Any:
    """Dataclass/sets/bytes → JSON-safe structure (bytes become PNG data URLs)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, bytes):
        return "data:image/png;base64," + base64.b64encode(obj).decode()
    if isinstance(obj, (set, frozenset)):
        return sorted(to_json(x) for x in obj)
    if isinstance(obj, (list, tuple)):
        return [to_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_json(v) for k, v in obj.items()}
    return obj
