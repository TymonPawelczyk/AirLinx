import pytest

pytest.importorskip("gi")
app = pytest.importorskip("airlinx.app")

from airlinx.core.models import AppState, Device, MediaItem, MirrorStatus, PlaybackState  # noqa: E402

SALON = Device("g", "Salon", "S85F", "1.2.3.4", 8009, None, {"cast_mirror"})


def test_tray_summary_states():
    st = AppState(devices=[SALON], selected_device_id="g")
    s = app.tray_summary(st)
    assert s == {"status": "Gotowe · Salon", "tv_name": "Salon", "mirroring": False, "can_mirror": True, "icon": "idle"}
    st.mirror = MirrorStatus(state="live")
    s = app.tray_summary(st)
    assert s["status"] == "Udostępniasz ekran → Salon" and s["icon"] == "streaming" and s["mirroring"]
    st.mirror = MirrorStatus(state="error")
    assert app.tray_summary(st)["icon"] == "error"
    st.mirror = MirrorStatus()
    st.mode = "media"
    st.queue = [MediaItem("i", "/a.mp4", "Wydmy", 10, "video")]
    st.playback = PlaybackState("playing", item_id="i")
    assert app.tray_summary(st)["status"] == "Odtwarzam: Wydmy"
    assert app.tray_summary(AppState(discovering=True))["status"] == "Szukam telewizora…"
    assert app.tray_summary(AppState())["can_mirror"] is False


def test_autostart_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    app.set_autostart(True)
    f = tmp_path / "autostart" / "io.github.airlinx.desktop"
    txt = f.read_text()
    assert "--no-window" in txt and "@EXEC@" not in txt
    app.set_autostart(False)
    assert not f.exists()


def test_autostart_exec_is_quoted_for_a_home_with_spaces(tmp_path, monkeypatch):
    GLib = pytest.importorskip("gi.repository.GLib")

    home = tmp_path / "my $HOME"
    launcher = home / ".local/bin/airlinx"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("HOME", str(home))
    app.set_autostart(True)
    txt = (home / ".config/autostart/io.github.airlinx.desktop").read_text()
    exec_line = next(ln for ln in txt.splitlines() if ln.startswith("Exec="))
    assert exec_line.startswith('Exec="')
    kf = GLib.KeyFile()
    kf.load_from_data(txt, len(txt.encode()), GLib.KeyFileFlags.NONE)
    assert GLib.shell_parse_argv(kf.get_string("Desktop Entry", "Exec"))[1] == [str(launcher), "--no-window"]


def test_desktop_exec_quote_escapes_reserved_characters():
    assert app.desktop_exec_quote("/a b") == '"/a b"'
    assert app.desktop_exec_quote('a"b$c`d') == '"a\\\\"b\\\\$c\\\\`d"'
    assert app.desktop_exec_quote("a\\b") == '"a\\\\\\\\b"'
    assert app.desktop_exec_quote("50%") == '"50%%"'


def test_quit_fallback_outlasts_shutdown_timeouts():
    from airlinx.controller import Controller

    assert app.quit_fallback_seconds() > Controller.SHUTDOWN_TOTAL
    assert Controller.SHUTDOWN_TOTAL >= Controller.SHUTDOWN_STOP_TIMEOUT + Controller.SHUTDOWN_SERVICE_TIMEOUT


def test_pychromecast_dial_warning_is_silenced():
    import logging

    from airlinx import app

    app.configure_logging(debug=False)
    dial = logging.getLogger("pychromecast.dial")
    assert not dial.isEnabledFor(logging.WARNING) and dial.isEnabledFor(logging.ERROR)
    app.configure_logging(debug=True)
    assert not dial.isEnabledFor(logging.WARNING)
    assert logging.getLogger("pychromecast").isEnabledFor(logging.INFO)
