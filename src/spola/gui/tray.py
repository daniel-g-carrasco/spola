"""Tray icon: a StatusNotifierItem with a com.canonical.dbusmenu menu, in
plain Gio (there is no libappindicator for GTK 4, and the GLib Ayatana
library exports menus in a format Noctalia does not read).

The lesson from Koofr Desktop, which re-announces the same icon through a new
temporary file every second and makes Noctalia rebuild its whole tray:
  * the icon is a themed icon NAME (exported by the Flatpak, recoloured by
    the shell), never a pixmap or a file;
  * NewIcon is emitted only when the name really changes, at most every
    MIN_ICON_INTERVAL_MS; tooltip updates at most every MIN_TOOLTIP_INTERVAL_MS.
tests/test_tray.py counts the signals on a private bus.

The item is registered by object path on our unique bus name, so no
org.kde.StatusNotifierItem-* name has to be owned (none is allowed in the
Flatpak); both GNOME's AppIndicator extension and Noctalia accept that.

The menu changes while files are transferring (a submenu lists them, the
progress line moves), so its signals are throttled too, and items are
numbered depth-first. When only texts change (the progress), the host gets
ItemsPropertiesUpdated for those items: an open menu updates in place
instead of being rebuilt.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"
MIN_ICON_INTERVAL_MS = 500
MIN_TOOLTIP_INTERVAL_MS = 2000
MIN_MENU_INTERVAL_MS = 2000

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
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


@dataclass
class MenuItem:
    label: str = ""
    action: Callable[[], None] | None = None
    enabled: bool = True
    separator: bool = False
    children: list[MenuItem] = field(default_factory=list)
    icon: str = ""
    # what keeps the item's number while the menu changes (see Ids); the
    # label when empty
    name: str = ""

    def props(self) -> dict:
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        props = {
            # "_" marks a mnemonic in dbusmenu: file names need it doubled
            "label": GLib.Variant("s", self.label.replace("_", "__")),
            "enabled": GLib.Variant("b", self.enabled and (self.action is not None or bool(self.children))),
            "visible": GLib.Variant("b", True)}
        if self.children:
            props["children-display"] = GLib.Variant("s", "submenu")
        if self.icon:
            props["icon-name"] = GLib.Variant("s", self.icon)
        return props

    def key(self) -> tuple:
        return (self.label, self.enabled and (self.action is not None or bool(self.children)),
                self.separator, self.icon, tuple(c.key() for c in self.children))

    def shape(self) -> tuple:
        """The structure only: what changes it needs a new layout."""
        return (self.separator, tuple(c.shape() for c in self.children))


class Ids:
    """The numbers of the menu items, kept across menus. A tray host may
    hold a menu a moment old (a click while a file finishes, or while the
    progress line goes away), and its click carries the old number: if the
    numbers followed the positions, "Open Spola" would land on "Quit". An item
    that does something keeps its number for its name, wherever it moves, and
    a number once given to it is never given to another; a line of text (no
    action) keeps the number of its place, so that a text changing every
    second is updated in place."""

    MAX_NAMES = 2000          # beyond that, the names no longer in the menu are forgotten

    def __init__(self):
        self._ids: dict[str, int] = {}
        self._next = 1

    def number(self, items: list[MenuItem]) -> tuple[dict[int, MenuItem], dict[int, list[int]]]:
        """Ids for the items (0 is the root), and the children of each id."""
        by_id: dict[int, MenuItem] = {}
        children: dict[int, list[int]] = {0: []}
        used: set[str] = set()

        def walk(parent: int, parent_key: str, lst: list[MenuItem]) -> None:
            for position, item in enumerate(lst):
                if item.action is not None or item.children:
                    key = f"{parent_key}/{item.name or item.label}"
                    n = 2
                    while key in used:          # two items alike: told apart by order
                        key, n = f"{parent_key}/{item.name or item.label}#{n}", n + 1
                else:
                    key = f"{parent_key}/@{position}"
                used.add(key)
                if key not in self._ids:
                    self._ids[key] = self._next
                    self._next += 1
                item_id = self._ids[key]
                by_id[item_id] = item
                children[parent].append(item_id)
                children[item_id] = []
                walk(item_id, key, item.children)
        walk(0, "", items)
        if len(self._ids) > self.MAX_NAMES:
            self._ids = {k: v for k, v in self._ids.items() if k in used}
        return by_id, children


def number(items: list[MenuItem]) -> tuple[dict[int, MenuItem], dict[int, list[int]]]:
    """Ids for one menu on its own (see Ids)."""
    return Ids().number(items)


class Throttle:
    """Call `emit` for a new value at most every `interval_ms`; values equal
    to the last emitted one are dropped. Separate from D-Bus so the rule can
    be tested on its own."""

    def __init__(self, interval_ms: int, emit: Callable[[], None]):
        self.interval_ms = interval_ms
        self.emit = emit
        self.emitted = None
        self.pending = None
        self._source = 0
        self._last = 0

    def set(self, value) -> None:
        self.pending = value
        if self._source:
            return
        wait = self.interval_ms - (GLib.get_monotonic_time() // 1000 - self._last)
        if wait > 0:
            self._source = GLib.timeout_add(wait, self._flush)
        else:
            self._flush()

    def _flush(self) -> bool:
        self._source = 0
        if self.pending != self.emitted:
            self.emitted = self.pending
            self._last = GLib.get_monotonic_time() // 1000
            self.emit()
        return False

    def cancel(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0


class TrayIcon:
    def __init__(self, app_id: str, title: str, on_activate: Callable[[], None],
                 icon_theme_path: str = ""):
        self.app_id = app_id
        self.title = title
        self.on_activate = on_activate
        self.icon_theme_path = icon_theme_path
        self.icon_name = f"{app_id}-symbolic"
        self.tooltip_title = title
        self.tooltip_body = ""
        self.items: list[MenuItem] = []
        self._by_id: dict[int, MenuItem] = {}
        self._children: dict[int, list[int]] = {0: []}
        self._ids = Ids()
        self.revision = 1
        self.connection: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._watch_id = 0
        self._icon = Throttle(MIN_ICON_INTERVAL_MS, lambda: self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon"))
        self._tooltip = Throttle(MIN_TOOLTIP_INTERVAL_MS, lambda: self._signal(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip"))
        self._layout_signal = Throttle(MIN_MENU_INTERVAL_MS, self._layout_updated)
        self._props_signal = Throttle(MIN_MENU_INTERVAL_MS, self._props_updated)
        self._dirty: set[int] = set()
        self._props_serial = 0
        self._icon.emitted = self.icon_name
        self._tooltip.emitted = (self.tooltip_title, self.tooltip_body)

    # -- public ------------------------------------------------------------------

    def start(self, connection: Gio.DBusConnection) -> None:
        self.connection = connection
        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._reg_ids = [
            connection.register_object(ITEM_PATH, item_info, self._item_call, self._item_get, None),
            connection.register_object(MENU_PATH, menu_info, self._menu_call, self._menu_get, None),
        ]
        # (re)register whenever a tray host appears: GNOME Shell or Noctalia restarts
        self._watch_id = Gio.bus_watch_name_on_connection(
            connection, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_appeared, None)

    def stop(self) -> None:
        self._icon.cancel()
        self._tooltip.cancel()
        self._layout_signal.cancel()
        self._props_signal.cancel()
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self.connection:
            for reg in self._reg_ids:
                self.connection.unregister_object(reg)
        self._reg_ids = []

    def set_icon(self, name: str) -> None:
        self.icon_name = name
        self._icon.set(name)

    def set_tooltip(self, title: str, body: str) -> None:
        self.tooltip_title, self.tooltip_body = title, body
        self._tooltip.set((title, body))

    def set_menu(self, items: list[MenuItem]) -> None:
        if [i.key() for i in items] == [i.key() for i in self.items]:
            self.items = items                  # same texts: new actions only
            self._by_id, self._children = self._ids.number(items)
            return
        same_shape = [i.shape() for i in items] == [i.shape() for i in self.items]
        old = self._by_id
        self.items = items
        self._by_id, self._children = self._ids.number(items)
        if same_shape:
            # only texts or states changed (the progress line): update those items
            self._dirty |= {i for i, item in self._by_id.items() if old.get(i) and old[i].key() != item.key()}
            self._props_serial += 1
            self._props_signal.set(self._props_serial)
        else:
            self._dirty.clear()
            self.revision += 1
            self._layout_signal.set(self.revision)

    def _props_updated(self) -> None:
        dirty, self._dirty = self._dirty, set()
        updated = [(i, self._by_id[i].props()) for i in sorted(dirty) if i in self._by_id]
        if self.connection and updated:
            self.connection.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                                        GLib.Variant("(a(ia{sv})a(ias))", (updated, [])))

    def _layout_updated(self) -> None:
        if self.connection:
            self.connection.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                        GLib.Variant("(ui)", (self.revision, 0)))

    # -- D-Bus: item -----------------------------------------------------------------

    def _watcher_appeared(self, connection, _name, _owner) -> None:
        connection.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE, 5000, None,
                        lambda conn, res: self._finish(conn, res))

    @staticmethod
    def _finish(connection, result) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error:
            pass   # no tray host right now: we stay registered for the next one

    def _signal(self, path: str, iface: str, name: str) -> None:
        if self.connection:
            self.connection.emit_signal(None, path, iface, name, None)

    def _item_get(self, _conn, _sender, _path, _iface, prop):
        tooltip = (self.icon_name, [], self.tooltip_title, self.tooltip_body)
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            # never "Passive": GNOME's AppIndicator extension hides passive items
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", tooltip),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _item_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: self.on_activate() and False)
        invocation.return_value(None)

    # -- D-Bus: menu -----------------------------------------------------------------

    def _layout(self, item_id: int = 0, depth: int = -1) -> tuple:
        children = []
        if depth != 0:
            children = [GLib.Variant("(ia{sv}av)", self._layout(child, depth - 1))
                        for child in self._children.get(item_id, [])]
        return (item_id, self._props_of(item_id), children)

    def _props_of(self, item_id: int) -> dict:
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        item = self._by_id.get(item_id)
        return item.props() if item else {}

    def _menu_get(self, _conn, _sender, _path, _iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [self.icon_theme_path] if self.icon_theme_path else []),
        }.get(prop)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            parent, depth, _names = params.unpack()
            if parent != 0 and parent not in self._by_id:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.UnknownId", str(parent))
                return
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth))))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            if not ids:
                ids = [0] + list(self._by_id)
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props_of(i)) for i in ids],)))
        elif method == "GetProperty":
            item_id, name = params.unpack()
            value = self._props_of(item_id).get(name)
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.UnknownProperty", name)
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event_id, _data, _ts = params.unpack()
            self._activate(item_id, event_id)
            invocation.return_value(None)
        elif method == "EventGroup":
            (events,) = params.unpack()
            for item_id, event_id, _data, _ts in events:
                self._activate(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _activate(self, item_id: int, event_id: str) -> None:
        item = self._by_id.get(item_id)
        if event_id != "clicked" or item is None:
            return
        action = item.action
        if action is not None:
            GLib.idle_add(lambda: action() and False)
