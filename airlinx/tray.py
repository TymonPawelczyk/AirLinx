"""System tray: StatusNotifierItem + com.canonical.dbusmenu over Gio D-Bus (pure Python).

No libappindicator: we export both interfaces ourselves and register with
org.kde.StatusNotifierWatcher (provided on GNOME by the appindicator extension).
The pure-Python menu model (`MenuModel`) is GLib-free except for variant building,
so the layout serialization is unit-testable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewIconThemePath"><arg name="icon_theme_path" type="s"/></signal>
    <signal name="NewMenu"/>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""


# --------------------------------------------------------------------------- menu model
@dataclass
class MenuItem:
    id: int
    label: str = ""
    enabled: bool = True
    visible: bool = True
    separator: bool = False
    action: Optional[str] = None
    icon_name: Optional[str] = None
    children: list["MenuItem"] = field(default_factory=list)

    def props(self) -> dict[str, tuple[str, Any]]:
        """Properties as {name: (gvariant_type, value)}; defaults are omitted per spec."""
        p: dict[str, tuple[str, Any]] = {}
        if self.separator:
            p["type"] = ("s", "separator")
        elif self.label:
            p["label"] = ("s", self.label)
        if not self.enabled:
            p["enabled"] = ("b", False)
        if not self.visible:
            p["visible"] = ("b", False)
        if self.icon_name:
            p["icon-name"] = ("s", self.icon_name)
        if self.children:
            p["children-display"] = ("s", "submenu")
        return p


class MenuModel:
    """dbusmenu tree; root id 0. Item ids are stable across rebuilds."""

    ID_STATUS, ID_MIRROR, ID_OPEN, ID_SEP, ID_QUIT = 1, 2, 3, 4, 5

    def __init__(self) -> None:
        self.revision = 1
        self.root = MenuItem(0)
        self.build("AirLinx — szukam telewizora…", None, False, False)

    def build(self, status: str, tv_name: Optional[str], mirroring: bool, can_mirror: bool) -> None:
        if mirroring:
            mirror = MenuItem(self.ID_MIRROR, "Zatrzymaj udostępnianie", action="mirror")
        elif tv_name:
            mirror = MenuItem(self.ID_MIRROR, f"Udostępnij ekran → {tv_name}", enabled=can_mirror, action="mirror")
        else:
            mirror = MenuItem(self.ID_MIRROR, "Udostępnij ekran", enabled=False, action="mirror")
        self.root.children = [
            MenuItem(self.ID_STATUS, status, enabled=False),
            mirror,
            MenuItem(self.ID_OPEN, "Otwórz AirLinx", action="open"),
            MenuItem(self.ID_SEP, separator=True),
            MenuItem(self.ID_QUIT, "Zakończ", action="quit"),
        ]
        self.revision += 1

    def find(self, item_id: int, node: Optional[MenuItem] = None) -> Optional[MenuItem]:
        node = node or self.root
        if node.id == item_id:
            return node
        for c in node.children:
            hit = self.find(item_id, c)
            if hit:
                return hit
        return None

    def layout(self, parent_id: int = 0, depth: int = -1,
               names: Optional[list[str]] = None) -> tuple[int, Any]:
        """Plain-python layout: (revision, (id, {prop: value}, [children...]))."""
        node = self.find(parent_id) or self.root

        def ser(n: MenuItem, d: int) -> tuple:
            props = {k: v for k, (_t, v) in n.props().items() if not names or k in names}
            kids = [ser(c, d - 1) for c in n.children] if d != 0 else []
            return (n.id, props, kids)

        return self.revision, ser(node, depth)

    def layout_variant(self, parent_id: int = 0, depth: int = -1, names: Optional[list[str]] = None) -> Any:
        """GLib.Variant '(u(ia{sv}av))' for GetLayout."""
        from gi.repository import GLib

        node = self.find(parent_id) or self.root

        def ser(n: MenuItem, d: int) -> Any:
            props = {k: GLib.Variant(t, v) for k, (t, v) in n.props().items() if not names or k in names}
            kids = [GLib.Variant("(ia{sv}av)", ser(c, d - 1)) for c in n.children] if d != 0 else []
            return (n.id, props, kids)

        return GLib.Variant("(u(ia{sv}av))", (self.revision, ser(node, depth)))

    def group_props_variant(self, ids: list[int], names: list[str]) -> Any:
        from gi.repository import GLib

        out = []
        for i in ids or [it.id for it in self._all()]:
            n = self.find(i)
            if n is None:
                continue
            out.append((i, {k: GLib.Variant(t, v) for k, (t, v) in n.props().items() if not names or k in names}))
        return GLib.Variant("(a(ia{sv}))", (out,))

    def _all(self, node: Optional[MenuItem] = None) -> list[MenuItem]:
        node = node or self.root
        res = [node]
        for c in node.children:
            res += self._all(c)
        return res


# --------------------------------------------------------------------------- tray
def default_icon_theme_path() -> str:
    """Directory holding hicolor/… with our status icons (repo data/icons or installed copy)."""
    here = Path(__file__).resolve().parent.parent / "data" / "icons"
    if here.is_dir():
        return str(here)
    installed = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "airlinx" / "icons"
    return str(installed)


class Tray:
    ICONS = {"idle": "airlinx-idle-symbolic", "streaming": "airlinx-streaming-symbolic",
             "error": "airlinx-error-symbolic"}

    def __init__(self, on_action: Callable[[str], None], icon_theme_path: Optional[str] = None):
        self.on_action = on_action
        self.icon_theme_path = icon_theme_path or default_icon_theme_path()
        self.menu = MenuModel()
        self.icon_state = "idle"
        self.tooltip = "AirLinx"
        self.bus: Any = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._reg_ids: list[int] = []
        self._own_id = 0
        self._watch_id = 0
        self.registered = False
        self._registering = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        from gi.repository import Gio

        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception as e:
            log.warning("tray: no session bus: %s", e)
            return False
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._reg_ids.append(self.bus.register_object(SNI_PATH, sni, self._sni_call, self._sni_get, None))
        self._reg_ids.append(self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_get, None))
        self._own_id = Gio.bus_own_name_on_connection(
            self.bus, self.bus_name, Gio.BusNameOwnerFlags.NONE, self._on_name_acquired, None)
        self._watch_id = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE, self._on_watcher_appeared, None)
        return True

    def stop(self) -> None:
        from gi.repository import Gio

        if self.bus is None:
            return
        for rid in self._reg_ids:
            self.bus.unregister_object(rid)
        self._reg_ids.clear()
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
        if self._own_id:
            Gio.bus_unown_name(self._own_id)
        self._own_id = self._watch_id = 0
        self.registered = False

    def _on_name_acquired(self, *_a: Any) -> None:
        self._register()

    def _on_watcher_appeared(self, *_a: Any) -> None:
        self._register()

    def _register(self) -> None:
        from gi.repository import Gio, GLib

        if self._registering:
            return
        self._registering = True

        def done(bus: Any, res: Any) -> None:
            self._registering = False
            try:
                bus.call_finish(res)
                self.registered = True
                log.info("tray registered with %s as %s", WATCHER_NAME, self.bus_name)
            except Exception as e:
                log.warning("tray registration failed: %s", e)

        self.bus.call(WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, 3000, None, done)

    # ------------------------------------------------------------------ public update API
    def update(self, *, status: str, tv_name: Optional[str], mirroring: bool, can_mirror: bool,
               icon: str = "idle") -> None:
        changed_icon = icon != self.icon_state
        self.icon_state = icon
        self.tooltip = status
        self.menu.build(status, tv_name, mirroring, can_mirror)
        if self.bus is None:
            return
        self._signal(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        if changed_icon:
            self._signal(SNI_PATH, "org.kde.StatusNotifierItem", "NewIcon", None)
            self._signal(SNI_PATH, "org.kde.StatusNotifierItem", "NewStatus",
                         ("(s)", ("NeedsAttention" if icon == "error" else "Active",)))
        self._signal(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", ("(ui)", (self.menu.revision, 0)))

    def _signal(self, path: str, iface: str, name: str, args: Optional[tuple[str, tuple]]) -> None:
        from gi.repository import GLib

        try:
            self.bus.emit_signal(None, path, iface, name, GLib.Variant(*args) if args else None)
        except Exception as e:
            log.debug("signal %s failed: %s", name, e)

    # ------------------------------------------------------------------ SNI
    def _sni_props(self) -> dict[str, Any]:
        from gi.repository import GLib

        icon = self.ICONS.get(self.icon_state, self.ICONS["idle"])
        return {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "airlinx"),
            "Title": GLib.Variant("s", "AirLinx"),
            "Status": GLib.Variant("s", "NeedsAttention" if self.icon_state == "error" else "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", icon),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", self.ICONS["error"]),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (icon, [], "AirLinx", self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }

    def _sni_get(self, _conn: Any, _sender: str, _path: str, _iface: str, prop: str) -> Any:
        return self._sni_props().get(prop)

    def _sni_call(self, _conn: Any, _sender: str, _path: str, _iface: str, method: str,
                  _params: Any, invocation: Any) -> None:
        if method == "Activate":
            self._action("toggle")
        elif method == "SecondaryActivate":
            self._action("mirror")
        elif method == "ContextMenu":
            pass  # host draws the dbusmenu itself
        invocation.return_value(None)

    # ------------------------------------------------------------------ dbusmenu
    def _menu_get(self, _conn: Any, _sender: str, _path: str, _iface: str, prop: str) -> Any:
        from gi.repository import GLib

        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path]),
        }.get(prop)

    def _menu_call(self, _conn: Any, _sender: str, _path: str, _iface: str, method: str,
                   params: Any, invocation: Any) -> None:
        from gi.repository import GLib

        try:
            args = params.unpack()
            if method == "GetLayout":
                parent, depth, names = args
                invocation.return_value(self.menu.layout_variant(parent, depth, list(names)))
            elif method == "GetGroupProperties":
                ids, names = args
                invocation.return_value(self.menu.group_props_variant(list(ids), list(names)))
            elif method == "GetProperty":
                item_id, name = args
                item = self.menu.find(item_id)
                props = item.props() if item else {}
                t, v = props.get(name, ("s", ""))
                invocation.return_value(GLib.Variant("(v)", (GLib.Variant(t, v),)))
            elif method == "Event":
                item_id, event_id, _data, _ts = args
                if event_id == "clicked":
                    self._click(item_id)
                invocation.return_value(None)
            elif method == "EventGroup":
                (events,) = args
                errors = []
                for item_id, event_id, _d, _ts in events:
                    if self.menu.find(item_id) is None:
                        errors.append(item_id)
                    elif event_id == "clicked":
                        self._click(item_id)
                invocation.return_value(GLib.Variant("(ai)", (errors,)))
            elif method == "AboutToShow":
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as e:
            log.exception("dbusmenu %s failed", method)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))

    def _click(self, item_id: int) -> None:
        item = self.menu.find(item_id)
        if item and item.action and item.enabled:
            from gi.repository import GLib

            GLib.idle_add(lambda: (self._action(item.action), False)[1])

    def _action(self, name: str) -> None:
        try:
            self.on_action(name)
        except Exception:
            log.exception("tray action %s failed", name)
