"""Offline tests for source enumeration parsing (airlinx.core.sources)."""
import pytest

from airlinx.core import sources as s

XRANDR_LISTMONITORS = """Monitors: 2
 0: +*DP-4 4096/597x2304/336+0+420  DP-4
 1: +DP-2.8 5120/697x2880/392+4096+0  DP-2.8
"""

XRANDR_HEAD = "Screen 0: minimum 8 x 8, current 9216 x 2880, maximum 32767 x 32767\nDP-4 connected primary"

CLIENT_LIST = "_NET_CLIENT_LIST(WINDOW): window id # 0x2c0000a, 0x800004, 0x4400003\n"

XPROP_TERMINAL = """_NET_WM_NAME(UTF8_STRING) = "◑ Streaming \\"ekranu\\" na telewizor"
WM_NAME(STRING) = "fallback"
WM_CLASS(STRING) = "gnome-terminal-server", "Gnome-terminal"
_NET_WM_STATE(ATOM) = _NET_WM_STATE_FOCUSED
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL
_NET_WM_PID(CARDINAL) = 2588
_GTK_FRAME_EXTENTS(CARDINAL) = 94, 94, 74, 114
"""

XPROP_DOCKER = """_NET_WM_NAME(UTF8_STRING) = "Containers - Docker Desktop"
WM_CLASS(STRING) = "docker-desktop", "docker-desktop"
_NET_WM_STATE(ATOM) = _NET_WM_STATE_HIDDEN
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL
_NET_WM_PID(CARDINAL) = 5436
_GTK_FRAME_EXTENTS:  not found.
"""

XWININFO = """
xwininfo: Window id: 0x2c0000a "x"

  Absolute upper-left X:  1958
  Absolute upper-left Y:  410
  Relative upper-left X:  0
  Relative upper-left Y:  0
  Width: 2220
  Height: 2344
  Depth: 32
  Map State: IsViewable
"""


def test_parse_monitors():
    mons = s.parse_xrandr_monitors(XRANDR_LISTMONITORS)
    assert [(m.name, m.x, m.y, m.w, m.h, m.primary) for m in mons] == [
        ("DP-4", 0, 420, 4096, 2304, True), ("DP-2.8", 4096, 0, 5120, 2880, False)]


def test_parse_screen_and_client_list():
    assert s.parse_xrandr_screen(XRANDR_HEAD) == (9216, 2880)
    assert s.parse_client_list(CLIENT_LIST) == [0x2C0000A, 0x800004, 0x4400003]
    assert s.parse_client_list("_NET_CLIENT_LIST:  no such atom on any window.") == []


def test_parse_window_with_csd_extents():
    props = s.parse_xprop(XPROP_TERMINAL)
    info = s.parse_xwininfo(XWININFO)
    w = s.window_from_props(0x2C0000A, props, info)
    assert w.title == '◑ Streaming "ekranu" na telewizor'
    assert w.wm_class == ["gnome-terminal-server", "Gnome-terminal"] and w.pid == 2588
    assert w.geometry == (1958 + 94, 410 + 74, 2220 - 188, 2344 - 188)
    assert w.viewable and not w.hidden and w.types == ["_NET_WM_WINDOW_TYPE_NORMAL"]
    assert s.is_capturable(w, own_pid=1)
    assert not s.is_capturable(w, own_pid=2588)


def test_hidden_tiny_and_self_windows_skipped():
    info = s.parse_xwininfo(XWININFO)
    hidden = s.window_from_props(1, s.parse_xprop(XPROP_DOCKER), info)
    assert "_GTK_FRAME_EXTENTS" not in s.parse_xprop(XPROP_DOCKER)
    assert hidden.hidden and not s.is_capturable(hidden)
    tiny = s.window_from_props(2, s.parse_xprop(XPROP_TERMINAL), dict(info, w=200, h=200))
    assert not s.is_capturable(tiny)
    me = s.window_from_props(3, s.parse_xprop('_NET_WM_NAME(UTF8_STRING) = "AirLinx"\n'), info)
    assert not s.is_capturable(me)
    dock = s.window_from_props(4, s.parse_xprop("_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_DOCK\n"), info)
    assert not s.is_capturable(dock)


def test_clamp_to_screen():
    assert s.clamp_to_screen((-32, 464, 2107, 2324), (9216, 2880)) == (0, 464, 2075, 2324)
    assert s.clamp_to_screen((9000, 2800, 500, 500), (9216, 2880)) == (9000, 2800, 216, 80)


# ----------------------------------------------------------------------------- Wayland session
@pytest.mark.parametrize("env, expected", [
    ({"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, True),
    ({"XDG_SESSION_TYPE": "wayland"}, True),
    ({"XDG_SESSION_TYPE": "Wayland"}, True),
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, True),          # no session type, Wayland socket set
    ({"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "wayland-0"}, False),  # nested compositor inside Xorg
    ({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}, False),
    ({"XDG_SESSION_TYPE": "tty"}, False),
    ({"WAYLAND_DISPLAY": ""}, False),
    ({}, False),
])
def test_is_wayland_session(env, expected):
    from airlinx.core.sources import is_wayland_session

    assert is_wayland_session(env) is expected
