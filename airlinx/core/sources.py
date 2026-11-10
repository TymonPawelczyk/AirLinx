"""Capturable sources on X11: monitors (xrandr) and top-level windows (xprop/xwininfo).

Only command-line X11 tools are used (no Gtk/Xlib imports), so this module can run on
the asyncio thread. Thumbnails come from a one-shot ``ffmpeg -f x11grab`` per source.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from .models import Source

log = logging.getLogger("airlinx.sources")

MIN_WINDOW_W, MIN_WINDOW_H = 160, 120
THUMB_WIDTH = 320
THUMB_TIMEOUT = 4.0
THUMB_PARALLEL = 4
SKIP_WINDOW_TYPES = {"_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_SPLASH",
                     "_NET_WM_WINDOW_TYPE_TOOLBAR", "_NET_WM_WINDOW_TYPE_MENU", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
                     "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
                     "_NET_WM_WINDOW_TYPE_POPUP_MENU", "_NET_WM_WINDOW_TYPE_DND"}
SELF_NAMES = ("airlinx",)


def is_wayland_session(env: Mapping[str, str]) -> bool:
    """True in a Wayland session, where x11grab (via XWayland) captures a black screen.

    XDG_SESSION_TYPE decides when it is set; without it a non-empty WAYLAND_DISPLAY means Wayland.
    """
    kind = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if kind:
        return kind == "wayland"
    return bool(env.get("WAYLAND_DISPLAY"))


def display() -> str:
    return os.environ.get("DISPLAY") or ":0"


# --------------------------------------------------------------------------------------
# subprocess helper
# --------------------------------------------------------------------------------------
async def _run(*cmd: str, timeout: float = 3.0, binary: bool = False) -> Optional[bytes | str]:
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL, env=dict(os.environ, DISPLAY=display()))
    except OSError as e:
        log.warning("cannot run %s: %s", cmd[0], e)
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        log.warning("%s timed out", cmd[0])
        return None
    if proc.returncode != 0:
        return None
    return out if binary else out.decode("utf-8", "replace")


# --------------------------------------------------------------------------------------
# parsers (pure, unit-tested with fixture strings)
# --------------------------------------------------------------------------------------
@dataclass
class Monitor:
    name: str
    x: int
    y: int
    w: int
    h: int
    primary: bool = False


_MON_RE = re.compile(r"^\s*\d+:\s+\+?(\*?)(\S+)\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)")


def parse_xrandr_monitors(text: str) -> list[Monitor]:
    """Parse ``xrandr --listmonitors`` output."""
    mons = []
    for line in text.splitlines():
        m = _MON_RE.match(line)
        if not m:
            continue
        star, _label, w, h, x, y, out = m.groups()
        mons.append(Monitor(name=out, x=int(x), y=int(y), w=int(w), h=int(h), primary=bool(star)))
    mons.sort(key=lambda mo: (not mo.primary, mo.x, mo.y))
    return mons


_SCREEN_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")


def parse_xrandr_screen(text: str) -> Optional[tuple[int, int]]:
    """Root screen size from plain ``xrandr`` output (``Screen 0: ... current W x H``)."""
    m = _SCREEN_RE.search(text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_client_list(text: str) -> list[int]:
    """``xprop -root _NET_CLIENT_LIST`` → window ids (stacking order not guaranteed)."""
    if "#" not in text:
        return []
    return [int(t, 16) for t in re.findall(r"0x[0-9a-fA-F]+", text.split("#", 1)[1])]


def parse_xprop(text: str) -> dict[str, str]:
    """``xprop -id W PROP...`` → {PROP: raw value string}; missing props are omitted."""
    props: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z_][A-Za-z0-9_]*)(?:\(([^)]*)\))?\s*(=|:)\s*(.*)$", line)
        if not m:
            continue
        name, _typ, sep, val = m.groups()
        if sep == ":" and "not found" in val:
            continue
        if sep == ":" and not val.strip():
            continue
        props[name] = val.strip()
    return props


def xprop_strings(val: str) -> list[str]:
    """Quoted strings of an xprop value (xprop escapes only quotes and backslashes)."""
    return [re.sub(r"\\(.)", r"\1", s) for s in re.findall(r'"((?:[^"\\]|\\.)*)"', val)]


def xprop_ints(val: str) -> list[int]:
    return [int(x) for x in re.findall(r"-?\d+", val)]


def parse_xwininfo(text: str) -> Optional[dict]:
    vals = {}
    for key, rx in (("x", r"Absolute upper-left X:\s+(-?\d+)"), ("y", r"Absolute upper-left Y:\s+(-?\d+)"),
                    ("w", r"Width:\s+(\d+)"), ("h", r"Height:\s+(\d+)")):
        m = re.search(rx, text)
        if not m:
            return None
        vals[key] = int(m.group(1))
    m = re.search(r"Map State:\s+(\w+)", text)
    vals["viewable"] = bool(m and m.group(1) == "IsViewable")
    return vals


def visible_geometry(win: dict, frame_extents: Sequence[int] = ()) -> tuple[int, int, int, int]:
    """Subtract client-side-decoration shadows (_GTK_FRAME_EXTENTS: left, right, top, bottom)."""
    x, y, w, h = win["x"], win["y"], win["w"], win["h"]
    if len(frame_extents) == 4:
        left, right, top, bottom = frame_extents
        x, y, w, h = x + left, y + top, w - left - right, h - top - bottom
    return x, y, max(0, w), max(0, h)


def clamp_to_screen(geom: tuple[int, int, int, int], screen: Optional[tuple[int, int]]
                    ) -> tuple[int, int, int, int]:
    x, y, w, h = geom
    x2, y2 = x + w, y + h
    x, y = max(0, x), max(0, y)
    if screen:
        x2, y2 = min(x2, screen[0]), min(y2, screen[1])
    return x, y, max(0, x2 - x), max(0, y2 - y)


@dataclass
class WindowInfo:
    xid: int
    title: str
    wm_class: list[str]
    pid: Optional[int]
    geometry: tuple[int, int, int, int]
    viewable: bool
    hidden: bool
    types: list[str]


def window_from_props(xid: int, props: dict[str, str], wininfo: dict) -> WindowInfo:
    names = xprop_strings(props.get("_NET_WM_NAME", "")) or xprop_strings(props.get("WM_NAME", ""))
    ext = xprop_ints(props.get("_GTK_FRAME_EXTENTS", ""))
    pid = xprop_ints(props.get("_NET_WM_PID", ""))
    state = props.get("_NET_WM_STATE", "")
    return WindowInfo(
        xid=xid, title=names[0] if names else "", wm_class=xprop_strings(props.get("WM_CLASS", "")),
        pid=pid[0] if pid else None, geometry=visible_geometry(wininfo, ext), viewable=wininfo["viewable"],
        hidden="_NET_WM_STATE_HIDDEN" in state,
        types=re.findall(r"_NET_WM_WINDOW_TYPE_\w+", props.get("_NET_WM_WINDOW_TYPE", "")))


def is_capturable(win: WindowInfo, own_pid: Optional[int] = None) -> bool:
    if not win.viewable or win.hidden:
        return False
    if win.types and all(t in SKIP_WINDOW_TYPES for t in win.types):
        return False
    _, _, w, h = win.geometry
    if w < MIN_WINDOW_W or h < MIN_WINDOW_H:
        return False
    if own_pid is not None and win.pid == own_pid:
        return False
    hay = " ".join([win.title, *win.wm_class]).lower()
    if any(n in hay for n in SELF_NAMES):
        return False
    return True


# --------------------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------------------
async def list_monitors() -> list[Monitor]:
    out = await _run("xrandr", "--listmonitors")
    return parse_xrandr_monitors(out) if isinstance(out, str) else []


async def screen_size() -> Optional[tuple[int, int]]:
    out = await _run("xrandr", "--current")
    return parse_xrandr_screen(out) if isinstance(out, str) else None


_PROPS = ("_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE", "_NET_WM_PID",
          "_GTK_FRAME_EXTENTS")


async def window_info(xid: int) -> Optional[WindowInfo]:
    props_txt, info_txt = await asyncio.gather(_run("xprop", "-id", hex(xid), *_PROPS),
                                               _run("xwininfo", "-id", hex(xid)))
    if not isinstance(props_txt, str) or not isinstance(info_txt, str):
        return None
    info = parse_xwininfo(info_txt)
    if info is None:
        return None
    return window_from_props(xid, parse_xprop(props_txt), info)


async def window_geometry(xid: int) -> Optional[tuple[int, int, int, int]]:
    """Current visible geometry of a window (None if gone/unmapped)."""
    w = await window_info(xid)
    if w is None or not w.viewable:
        return None
    return w.geometry


async def list_windows() -> list[WindowInfo]:
    out = await _run("xprop", "-root", "_NET_CLIENT_LIST")
    if not isinstance(out, str):
        return []
    ids = parse_client_list(out)
    infos = await asyncio.gather(*(window_info(x) for x in ids))
    own = os.getpid()
    return [w for w in infos if w is not None and is_capturable(w, own)]


async def grab_thumbnail(geometry: tuple[int, int, int, int], width: int = THUMB_WIDTH,
                         timeout: float = THUMB_TIMEOUT) -> Optional[bytes]:
    x, y, w, h = geometry
    if w < 2 or h < 2:
        return None
    out = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-f", "x11grab", "-draw_mouse", "0", "-video_size", f"{w}x{h}",
                     "-i", f"{display()}+{x},{y}", "-frames:v", "1",
                     "-vf", f"scale={width}:-2:flags=bilinear", "-f", "image2pipe", "-vcodec", "png", "-",
                     timeout=timeout, binary=True)
    return out if isinstance(out, bytes) and out.startswith(b"\x89PNG") else None


async def list_sources(with_thumbnails: bool = True) -> list[Source]:
    mons, wins, screen = await asyncio.gather(list_monitors(), list_windows(), screen_size())
    sources: list[Source] = []
    for i, m in enumerate(mons, 1):
        label = f"Ekran {i} — {m.name} ({m.w}×{m.h})" + (" · główny" if m.primary else "")
        sources.append(Source(id=f"monitor:{m.name}", kind="monitor", name=label, geometry=(m.x, m.y, m.w, m.h)))
    for w in wins:
        geom = clamp_to_screen(w.geometry, screen)
        if geom[2] < MIN_WINDOW_W or geom[3] < MIN_WINDOW_H:
            continue
        sources.append(Source(id=f"window:{w.xid:#x}", kind="window", name=w.title or (w.wm_class[-1:] or ["Okno"])[0],
                              geometry=geom, xid=w.xid))
    if with_thumbnails and sources:
        sem = asyncio.Semaphore(THUMB_PARALLEL)

        async def thumb(s: Source) -> None:
            async with sem:
                s.thumbnail_png = await grab_thumbnail(s.geometry)
        await asyncio.gather(*(thumb(s) for s in sources))
    return sources
