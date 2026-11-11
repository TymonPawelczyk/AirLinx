"""Gtk.Application: window with the WebKit UI, tray, lifecycle.

Window decision: client-side decorated window with an *empty* titlebar widget.
The HTML UI (docs/design/final) draws its own title bar + window controls, so
we keep GTK's CSD shadow and resize edges but no native header. The HTML header
calls window.startDrag / window.minimize / window.toggleMaximize / window.close
(local GTK-thread bridge methods) to behave like a real title bar.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

from . import __version__, runtime  # noqa: E402
from .bridge import Bridge  # noqa: E402
from .controller import Controller, UiHooks  # noqa: E402
from .core.models import AppState  # noqa: E402
from .settings import Settings, config_dir  # noqa: E402
from .tray import Tray  # noqa: E402

log = logging.getLogger(__name__)

APP_ID = "io.github.airlinx"
ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"
DATA_DIR = ROOT / "data"
ICON_DIR = DATA_DIR / "icons"

CSS = b"""
window.airlinx, window.airlinx.background { background-color: #000000; }
.airlinx-titlebar { min-height: 0; padding: 0; margin: 0; border: none; background: #000000; box-shadow: none; }
"""

MEDIA_PATTERNS = {
    "Wideo": ["*.mp4", "*.mkv", "*.webm", "*.avi", "*.mov", "*.m4v", "*.ts", "*.mpg", "*.mpeg", "*.wmv"],
    "Muzyka": ["*.mp3", "*.flac", "*.m4a", "*.aac", "*.ogg", "*.opus", "*.wav"],
    "Zdjęcia": ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp"],
}


def tray_summary(st: AppState) -> dict[str, Any]:
    """Map AppState → tray status line / icon / menu flags (pure, testable)."""
    dev = next((d for d in st.devices if d.id == st.selected_device_id), None)
    tv = dev.name if dev else None
    m = st.mirror.state
    mirroring = m in ("connecting", "waiting_tv", "live", "reconnecting")
    icon = "idle"
    if m == "live":
        status, icon = f"Udostępniasz ekran → {tv or 'TV'}", "streaming"
    elif m in ("connecting", "waiting_tv"):
        status, icon = f"Łączę z {tv or 'telewizorem'}…", "streaming"
    elif m == "reconnecting":
        status, icon = "Odzyskuję połączenie…", "streaming"
    elif m == "error":
        status, icon = "Udostępnianie przerwane", "error"
    elif st.mode == "media" and st.playback.state in ("playing", "buffering", "loading", "paused"):
        item = next((q for q in st.queue if q.id == st.playback.item_id), None)
        verb = "Pauza" if st.playback.state == "paused" else "Odtwarzam"
        status, icon = f"{verb}: {item.title if item else 'multimedia'}", "streaming"
    elif tv:
        status = f"Gotowe · {tv}" + ("" if dev and dev.online else " (offline)")
    elif st.discovering:
        status = "Szukam telewizora…"
    else:
        status = "Nie znaleziono telewizora"
    can = bool(dev and dev.online)
    return {"status": status, "tv_name": tv, "mirroring": mirroring, "can_mirror": can, "icon": icon}


class AirlinxApp(Gtk.Application):
    def __init__(self, debug: bool = False, no_window: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.debug = debug
        self.no_window = no_window
        self.window: Optional[Gtk.ApplicationWindow] = None
        self.webview: Optional[WebKit2.WebView] = None
        self.settings_store: Optional[Settings] = None
        self.controller: Optional[Controller] = None
        self.bridge: Optional[Bridge] = None
        self.tray: Optional[Tray] = None
        self._quitting = False
        self._finished = False
        self._first_activate = True
        self._dropping = False
        self._chooser: Any = None

    # ------------------------------------------------------------------ startup
    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        self.hold()  # keep running while hidden in the tray
        gs = Gtk.Settings.get_default()
        if gs is not None:
            gs.set_property("gtk-application-prefer-dark-theme", True)
        Gtk.IconTheme.get_default().append_search_path(str(ICON_DIR))
        Gtk.Window.set_default_icon_name("airlinx")
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        runtime.start()
        self.settings_store = Settings()
        hooks = UiHooks(pick_files=self._pick_files_async, hide_window=lambda: runtime.call_in_gtk(self.hide_window),
                        quit=lambda: runtime.call_in_gtk(self.request_quit),
                        set_autostart=lambda on: runtime.call_in_gtk(set_autostart, on))
        self.controller = Controller(self.settings_store, hooks=hooks)
        self.bridge = Bridge(self.controller, runtime.run_coro, runtime.call_in_gtk, debug=self.debug)
        self.bridge.local_handlers.update({
            "window.startDrag": self._win_start_drag,
            "window.minimize": lambda p: self.window and self.window.iconify(),
            "window.toggleMaximize": self._win_toggle_max,
            "window.close": lambda p: self.on_close_request(),
            "window.isNative": lambda p: True,
        })
        self.controller.subscribe(self._on_controller_event)

        self.tray = Tray(self._on_tray_action)
        if not self.tray.start():
            log.warning("tray unavailable; closing the window will quit")

        self._build_window()
        import signal

        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (self.request_quit(), True)[1])
        fut = runtime.run_coro(self.controller.start())
        fut.add_done_callback(lambda f: f.exception() and log.error("controller start failed", exc_info=f.exception()))

        for name, cb in (("quit", lambda *_: self.request_quit()), ("show", lambda *_: self.show_window())):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

    def do_activate(self) -> None:
        first, self._first_activate = self._first_activate, False
        if first and (self.no_window or (self.settings_store and self.settings_store.get("start_hidden"))):
            return
        self.show_window()

    # ------------------------------------------------------------------ window
    def _build_window(self) -> None:
        win = Gtk.ApplicationWindow(application=self, title="AirLinx")
        win.get_style_context().add_class("airlinx")
        win.set_default_size(1100, 720)
        win.set_size_request(860, 560)
        win.set_position(Gtk.WindowPosition.CENTER)
        titlebar = Gtk.Box()
        titlebar.get_style_context().add_class("airlinx-titlebar")
        titlebar.set_size_request(-1, 0)
        win.set_titlebar(titlebar)  # CSD (shadow + resize edges) without a native header
        win.connect("delete-event", lambda *_: self.on_close_request() or True)

        ucm = WebKit2.UserContentManager()
        wv = WebKit2.WebView.new_with_user_content_manager(ucm)
        wv.set_background_color(Gdk.RGBA(0, 0, 0, 1))
        s = wv.get_settings()
        s.set_allow_file_access_from_file_urls(True)
        s.set_allow_universal_access_from_file_urls(False)
        s.set_enable_developer_extras(self.debug)
        s.set_enable_write_console_messages_to_stdout(self.debug)
        s.set_javascript_can_open_windows_automatically(False)
        s.set_enable_back_forward_navigation_gestures(False)
        s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
        wv.connect("decide-policy", self._on_decide_policy)
        wv.connect("context-menu", lambda *_: not self.debug)
        wv.connect("drag-drop", self._on_drag_drop)
        wv.connect("drag-data-received", self._on_drag_data)
        wv.connect("web-process-terminated", lambda _w, r: log.error("web process terminated: %s", r))
        assert self.bridge is not None
        self.bridge.attach(wv)
        index = UI_DIR / "index.html"
        wv.load_uri(index.as_uri())
        win.add(wv)
        wv.show()
        titlebar.show()
        self.window, self.webview = win, wv

    def show_window(self) -> None:
        if self.window is None:
            return
        self.window.show()
        self.window.present_with_time(Gtk.get_current_event_time() or GLib.get_monotonic_time() // 1000)

    def hide_window(self) -> None:
        if self.window is not None:
            self.window.hide()

    def toggle_window(self) -> None:
        if self.window is None:
            return
        if self.window.get_visible() and self.window.is_active():
            self.hide_window()
        else:
            self.show_window()

    def on_close_request(self) -> None:
        if self.settings_store and self.settings_store.get("close_to_tray", True) and self.tray and self.tray.registered:
            self.hide_window()
        else:
            self.request_quit()

    def _win_start_drag(self, params: dict) -> bool:
        if self.window is None:
            return False
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display else None
        if seat is None:
            return False
        _scr, x, y = seat.get_pointer().get_position()
        self.window.begin_move_drag(int(params.get("button", 1) or 1), x, y, Gtk.get_current_event_time())
        return True

    def _win_toggle_max(self, _params: dict) -> bool:
        if self.window is None:
            return False
        if self.window.is_maximized():
            self.window.unmaximize()
        else:
            self.window.maximize()
        return True

    def _on_decide_policy(self, _wv: Any, decision: Any, dtype: Any) -> bool:
        if dtype in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if uri.startswith(UI_DIR.as_uri()) and dtype == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            decision.ignore()
            if uri.startswith(("http://", "https://")):
                try:
                    Gtk.show_uri_on_window(self.window, uri, Gtk.get_current_event_time())
                except Exception as e:
                    log.warning("cannot open %s: %s", uri, e)
            else:
                log.info("blocked navigation to %s", uri)
            return True
        return False

    # ------------------------------------------------------------------ drag & drop
    def _on_drag_drop(self, *_a: Any) -> bool:
        self._dropping = True
        return False  # let WebKit continue (JS still gets the drop event)

    def _on_drag_data(self, _wv: Any, _ctx: Any, _x: int, _y: int, data: Any, _info: int, _time: int) -> bool:
        if not self._dropping:
            return False
        self._dropping = False
        uris = data.get_uris() or []
        paths = [unquote(urlparse(u).path) if u.startswith("file://") else u for u in uris]
        if paths and self.controller is not None:
            log.info("dropped %d item(s)", len(paths))
            fut = runtime.run_coro(self.controller.media_add(paths=paths))
            fut.add_done_callback(lambda f: f.exception() and log.warning("drop add failed: %s", f.exception()))
        return False

    # ------------------------------------------------------------------ file chooser
    async def _pick_files_async(self) -> list[str]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def done(paths: list[str]) -> None:
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(paths))

        runtime.call_in_gtk(self._show_chooser, done)
        return await fut

    def _show_chooser(self, done: Any) -> None:
        chooser = Gtk.FileChooserNative.new("Dodaj multimedia", self.window, Gtk.FileChooserAction.OPEN,
                                            "Dodaj", "Anuluj")
        chooser.set_select_multiple(True)
        all_f = Gtk.FileFilter()
        all_f.set_name("Wszystkie multimedia")
        for pats in MEDIA_PATTERNS.values():
            for p in pats:
                all_f.add_pattern(p)
                all_f.add_pattern(p.upper())
        chooser.add_filter(all_f)
        for name, pats in MEDIA_PATTERNS.items():
            f = Gtk.FileFilter()
            f.set_name(name)
            for p in pats:
                f.add_pattern(p)
                f.add_pattern(p.upper())
            chooser.add_filter(f)
        last = self.settings_store.get("ui.last_dir") if self.settings_store else None
        if last and os.path.isdir(last):
            chooser.set_current_folder(last)

        def on_response(dlg: Any, resp: int) -> None:
            paths: list[str] = []
            if resp == Gtk.ResponseType.ACCEPT:
                paths = [f.get_path() for f in dlg.get_files() if f.get_path()]
                if paths and self.settings_store:
                    self.settings_store.set("ui.last_dir", os.path.dirname(paths[0]))
            dlg.destroy()
            self._chooser = None
            done(paths)

        chooser.connect("response", on_response)
        self._chooser = chooser  # keep a reference while shown
        chooser.show()

    # ------------------------------------------------------------------ controller → tray/notifications
    def _on_controller_event(self, event: str, payload: Any) -> None:
        if event == "state" and isinstance(payload, AppState):
            runtime.call_in_gtk(self._update_tray, payload)
        elif event == "error" and isinstance(payload, dict):
            runtime.call_in_gtk(self._notify_if_hidden, payload.get("message") or "Błąd")

    def _update_tray(self, st: AppState) -> None:
        if self.tray is not None:
            self.tray.update(**tray_summary(st))

    def _notify_if_hidden(self, message: str) -> None:
        if self.window is not None and self.window.get_visible():
            return
        if self.settings_store and not self.settings_store.get("notifications", True):
            return
        n = Gio.Notification.new("AirLinx")
        n.set_body(message)
        n.set_default_action("app.show")
        self.send_notification("airlinx-error", n)

    def _on_tray_action(self, action: str) -> None:
        if action == "toggle":
            self.toggle_window()
        elif action == "open":
            self.show_window()
        elif action == "quit":
            self.request_quit()
        elif action == "mirror" and self.controller is not None:
            fut = runtime.run_coro(self.controller.mirror_toggle())

            def report(f: Any) -> None:
                exc = f.exception()
                if exc is not None and self.controller is not None:
                    self.controller.error(getattr(exc, "code", "mirror_failed"),
                                          getattr(exc, "message_pl", "Nie udało się udostępnić ekranu."))

            fut.add_done_callback(report)

    # ------------------------------------------------------------------ quit
    def request_quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("quitting: stopping sessions")
        if self.window is not None:
            self.window.hide()
        if self.tray is not None:
            self.tray.stop()

        def finish(*_a: Any) -> bool:
            if self._finished:
                return False
            self._finished = True
            runtime.stop()
            self.release()
            self.quit()
            return False

        if self.controller is None:
            finish()
            return
        fut = runtime.run_coro(self.controller.shutdown())
        fut.add_done_callback(lambda _f: runtime.call_in_gtk(finish))
        # Hard fallback must outlast every timeout inside shutdown(), or it would cut a STOP short.
        GLib.timeout_add_seconds(quit_fallback_seconds(), finish)


def quit_fallback_seconds() -> int:
    """Hard quit timeout: longer than the sum of Controller.shutdown() timeouts."""
    import math

    return int(math.ceil(Controller.SHUTDOWN_TOTAL)) + 2


# --------------------------------------------------------------------------- autostart
def desktop_exec_quote(arg: str) -> str:
    """Quote one argument for a .desktop Exec= line (Desktop Entry Spec; same rules as install.sh).

    Double quotes only (the spec has no single-quote form), a backslash before " ` $ \\, then every
    backslash doubled again by the string-value escape rule, and % written as %%.
    """
    for ch in '\\"`$':
        arg = arg.replace(ch, "\\" + ch)
    return '"' + arg.replace("\\", "\\\\").replace("%", "%%") + '"'


def _launcher_cmd() -> str:
    local_bin = Path.home() / ".local" / "bin" / "airlinx"
    if local_bin.exists():
        return f"{desktop_exec_quote(str(local_bin))} --no-window"
    return (f"env {desktop_exec_quote('PYTHONPATH=' + str(ROOT))} {desktop_exec_quote(sys.executable)} "
            "-m airlinx --no-window")


def set_autostart(enabled: bool) -> None:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    target = base / "autostart" / f"{APP_ID}.desktop"
    if not enabled:
        target.unlink(missing_ok=True)
        log.info("autostart disabled")
        return
    tpl = (DATA_DIR / f"{APP_ID}-autostart.desktop").read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tpl.replace("@EXEC@", _launcher_cmd()), encoding="utf-8")
    log.info("autostart enabled: %s", target)


def configure_logging(debug: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("asyncio", "zeroconf", "pychromecast", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.INFO if debug else logging.WARNING)
    # Samsung has no :8008 eureka_info; pychromecast warns on every connect although all works.
    logging.getLogger("pychromecast.dial").setLevel(logging.ERROR)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="airlinx", description="AirLinx — ekran i multimedia na telewizor")
    ap.add_argument("--debug", action="store_true", help="logi DEBUG + inspektor WebKit")
    ap.add_argument("--no-window", action="store_true", help="start schowany w zasobniku")
    ap.add_argument("--version", action="version", version=f"AirLinx {__version__}")
    args = ap.parse_args(argv)
    configure_logging(args.debug)
    # Developer mode only (AIRLINX_DEV=1 + AIRLINX_DEV_CAST_ROOT=<pem>); off and silent otherwise. See castv2.py.
    from .core.cast.castv2 import configure_dev_trust

    configure_dev_trust()
    GLib.set_prgname("airlinx")
    GLib.set_application_name("AirLinx")
    config_dir().mkdir(parents=True, exist_ok=True)
    app = AirlinxApp(debug=args.debug, no_window=args.no_window)
    return app.run([sys.argv[0]])
