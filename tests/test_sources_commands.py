"""list_sources() end to end with stub xrandr/xprop/xwininfo/ffmpeg executables on PATH: the glue between the
parsers (own window excluded, hidden windows dropped, geometry clamped to the screen, thumbnails optional)."""
import asyncio
import os
import stat
import textwrap

import pytest

from airlinx.core import sources as s

from test_sources import CLIENT_LIST, XPROP_DOCKER, XPROP_TERMINAL, XRANDR_HEAD, XRANDR_LISTMONITORS, XWININFO


def stub(bindir, name, body):
    p = bindir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def x11(tmp_path, monkeypatch):
    """Stub X tools. Window 0x2c0000a = terminal (viewable), 0x800004 = hidden docker app, 0x4400003 = AirLinx itself."""
    b = tmp_path / "bin"
    b.mkdir()
    own = XPROP_TERMINAL.replace("2588", str(os.getpid())).replace("◑ Streaming", "AirLinx")
    stub(b, "xrandr", f"""
        case "$1" in
          --listmonitors) cat <<'EOT'
        {XRANDR_LISTMONITORS}EOT
          ;;
          --current) cat <<'EOT'
        {XRANDR_HEAD}
        EOT
          ;;
        esac
    """.replace("\n        ", "\n"))
    (tmp_path / "terminal.txt").write_text(XPROP_TERMINAL)
    (tmp_path / "docker.txt").write_text(XPROP_DOCKER)
    (tmp_path / "own.txt").write_text(own)
    (tmp_path / "client.txt").write_text(CLIENT_LIST)
    (tmp_path / "wininfo.txt").write_text(XWININFO)
    stub(b, "xprop", f"""
        d="{tmp_path}"
        if [ "$1" = "-root" ]; then cat "$d/client.txt"; exit 0; fi
        case "$2" in
          0x2c0000a) cat "$d/terminal.txt";;
          0x800004) cat "$d/docker.txt";;
          0x4400003) cat "$d/own.txt";;
          *) exit 1;;
        esac
    """)
    stub(b, "xwininfo", f"""cat "{tmp_path}/wininfo.txt"\n""")
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setenv("DISPLAY", ":42")
    return b


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 15))


def test_list_sources_returns_monitors_then_capturable_windows(x11):
    srcs = run(s.list_sources(with_thumbnails=False))
    kinds = [(x.kind, x.id) for x in srcs]
    assert kinds == [("monitor", "monitor:DP-4"), ("monitor", "monitor:DP-2.8"), ("window", "window:0x2c0000a")]
    mon = srcs[0]
    assert mon.geometry == (0, 420, 4096, 2304) and "główny" in mon.name and mon.name.startswith("Ekran 1")
    win = srcs[2]
    assert win.xid == 0x2C0000A and win.geometry == (1958 + 94, 410 + 74, 2220 - 188, 2344 - 188)
    assert all(x.thumbnail_png is None for x in srcs)


def test_own_window_and_hidden_windows_are_not_offered(x11):
    names = [x.name for x in run(s.list_sources(with_thumbnails=False))]
    assert not any("AirLinx" in n for n in names)           # 0x4400003 has our own pid
    assert not any("Docker" in n for n in names)            # 0x800004 is hidden


def test_thumbnails_are_attached_only_when_ffmpeg_returns_a_png(x11):
    stub(x11, "ffmpeg", """printf '\\x89PNG\\r\\n\\x1a\\nDATA'\n""")
    srcs = run(s.list_sources(with_thumbnails=True))
    assert len(srcs) == 3 and all(x.thumbnail_png and x.thumbnail_png.startswith(b"\x89PNG") for x in srcs)
    stub(x11, "ffmpeg", "printf 'not an image'\n")
    assert all(x.thumbnail_png is None for x in run(s.list_sources(with_thumbnails=True)))
    stub(x11, "ffmpeg", "exit 1\n")
    assert all(x.thumbnail_png is None for x in run(s.list_sources(with_thumbnails=True)))


def test_missing_x_tools_yield_an_empty_list_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))          # nothing on PATH
    assert run(s.list_sources(with_thumbnails=False)) == []
    assert run(s.list_monitors()) == [] and run(s.screen_size()) is None and run(s.list_windows()) == []


def test_failing_tool_is_treated_as_no_data(x11):
    stub(x11, "xrandr", "echo 'Can not open display' >&2; exit 1\n")
    assert run(s.list_monitors()) == [] and run(s.screen_size()) is None


def test_hung_tool_is_killed_after_the_timeout(x11, monkeypatch):
    stub(x11, "slowtool", "sleep 30\n")
    import time
    t0 = time.monotonic()
    assert run(s._run("slowtool", timeout=0.3)) is None
    assert time.monotonic() - t0 < 3


def test_window_geometry_is_none_for_a_gone_window(x11):
    assert run(s.window_geometry(0x1)) is None                 # xprop exits 1
    assert run(s.window_geometry(0x2C0000A)) == (1958 + 94, 410 + 74, 2220 - 188, 2344 - 188)


def test_thumbnail_of_a_degenerate_region_is_skipped_without_running_ffmpeg(x11):
    stub(x11, "ffmpeg", "touch \"$0.ran\"\nprintf '\\x89PNG'\n")
    assert run(s.grab_thumbnail((0, 0, 1, 1))) is None
    assert not (x11 / "ffmpeg.ran").exists()
