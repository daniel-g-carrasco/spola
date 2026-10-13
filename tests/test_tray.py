#!/usr/bin/env python3
"""The tray icon on a private D-Bus, with a fake tray host. The point is the
Koofr Desktop lesson: no NewIcon while nothing changes, no bursts while the
state flips quickly, and the menu works."""

import time
import unittest

import _setup  # noqa: F401
import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from spola.tray import ITEM_PATH, WATCHER, MenuItem, TrayIcon  # noqa: E402

WATCHER_XML = """
<node><interface name="org.kde.StatusNotifierWatcher">
  <method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>
"""


def spin_until(cond, timeout: float = 3.0) -> bool:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        ctx.iteration(False)
        time.sleep(0.002)
    return cond()


def spin(seconds: float) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


class TrayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.bus.up()

    @classmethod
    def tearDownClass(cls):
        cls.bus.down()

    def connect(self):
        return Gio.DBusConnection.new_for_address_sync(
            self.bus.get_bus_address(),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)

    def setUp(self):
        self.registered = []
        self.new_icons = 0
        self.host = self.connect()
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]

        def call(conn, sender, path, iface, method, params, inv):
            self.registered.append((sender, params.unpack()[0]))
            inv.return_value(None)

        self.host.register_object("/StatusNotifierWatcher", info, call, None, None)
        self.owner = Gio.bus_own_name_on_connection(self.host, WATCHER, Gio.BusNameOwnerFlags.NONE, None, None)

        def on_signal(*args):
            self.new_icons += 1

        self.host.signal_subscribe(None, "org.kde.StatusNotifierItem", "NewIcon", ITEM_PATH, None,
                                   Gio.DBusSignalFlags.NONE, on_signal)
        self.app_conn = self.connect()
        self.clicked = []
        self.tray = TrayIcon("io.example.App", "App", on_activate=lambda: self.clicked.append("activate"))
        self.tray.set_menu([MenuItem("Sync now", lambda: self.clicked.append("sync")),
                            MenuItem(separator=True),
                            MenuItem("Status: ok"),
                            MenuItem("Recent", children=[
                                MenuItem("my_report.odt", lambda: self.clicked.append("recent"),
                                         icon="go-up-symbolic")])])
        self.tray.start(self.app_conn)
        spin_until(lambda: self.registered)

    def tearDown(self):
        self.tray.stop()
        Gio.bus_unown_name(self.owner)
        self.app_conn.close_sync(None)
        self.host.close_sync(None)
        spin(0.1)

    def call(self, path, iface, method, params):
        """Async call from the fake host, spinning the loop the app answers on."""
        box = []
        self.host.call(self.app_conn.get_unique_name(), path, iface, method, params, None,
                       Gio.DBusCallFlags.NONE, 2000, None,
                       lambda conn, res: box.append(conn.call_finish(res)))
        self.assertTrue(spin_until(lambda: box), f"{method}: no answer")
        return box[0]

    def get(self, iface, prop, path=ITEM_PATH):
        return self.call(path, "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (iface, prop))).unpack()[0]

    def test_registers_by_path_on_unique_name(self):
        self.assertEqual(self.registered, [(self.app_conn.get_unique_name(), ITEM_PATH)])
        self.assertEqual(self.get("org.kde.StatusNotifierItem", "Status"), "Active")
        self.assertEqual(self.get("org.kde.StatusNotifierItem", "IconName"), "io.example.App-symbolic")

    def test_no_new_icon_when_nothing_changes(self):
        spin(1.5)
        for _ in range(50):
            self.tray.set_icon("io.example.App-symbolic")
        spin(1.2)
        self.assertEqual(self.new_icons, 0)

    def test_quick_flips_are_coalesced(self):
        for i in range(40):
            self.tray.set_icon("io.example.App-syncing-symbolic" if i % 2 == 0 else "io.example.App-error-symbolic")
            spin(0.02)
        spin(1.2)
        self.assertGreaterEqual(self.new_icons, 1)
        self.assertLessEqual(self.new_icons, 3)
        self.assertEqual(self.get("org.kde.StatusNotifierItem", "IconName"), "io.example.App-error-symbolic")

    def test_menu_click_and_activate(self):
        reply = self.call("/StatusNotifierItem/Menu", "com.canonical.dbusmenu", "GetLayout",
                          GLib.Variant("(iias)", (0, -1, [])))
        _rev, (_id, _props, children) = reply.unpack()
        labels = [c[1].get("label") for c in children]
        self.assertEqual(labels, ["Sync now", None, "Status: ok", "Recent"])
        self.assertFalse(children[2][1]["enabled"])       # no action: shown as text
        self.call("/StatusNotifierItem/Menu", "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("s", ""), 0)))
        self.call(ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (0, 0)))
        spin(0.3)
        self.assertEqual(self.clicked, ["sync", "activate"])

    def test_submenu(self):
        menu = "/StatusNotifierItem/Menu"
        _rev, (_id, _props, children) = self.call(menu, "com.canonical.dbusmenu", "GetLayout",
                                                  GLib.Variant("(iias)", (0, -1, []))).unpack()
        recent_id, recent_props, recent_children = children[3]
        self.assertEqual(recent_props["children-display"], "submenu")
        self.assertTrue(recent_props["enabled"])
        (file_id, file_props, _c), = recent_children
        self.assertEqual(file_props["label"], "my__report.odt")     # "_" is a mnemonic in dbusmenu
        self.assertEqual(file_props["icon-name"], "go-up-symbolic")
        # a host may ask for one submenu only
        _rev, (sub_id, _p, sub_children) = self.call(menu, "com.canonical.dbusmenu", "GetLayout",
                                                     GLib.Variant("(iias)", (recent_id, 1, []))).unpack()
        self.assertEqual((sub_id, [c[0] for c in sub_children]), (recent_id, [file_id]))
        (props,) = self.call(menu, "com.canonical.dbusmenu", "GetGroupProperties",
                             GLib.Variant("(aias)", ([file_id], []))).unpack()
        self.assertEqual(props[0][1]["label"], "my__report.odt")
        self.call(menu, "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (file_id, "clicked", GLib.Variant("s", ""), 0)))
        spin(0.3)
        self.assertEqual(self.clicked, ["recent"])

    def test_new_texts_update_items_in_place(self):
        # the progress line changes every second: an open menu must update
        # in place, not be rebuilt (LayoutUpdated) at every change
        layouts, props = [], []
        menu = "/StatusNotifierItem/Menu"
        self.host.signal_subscribe(None, "com.canonical.dbusmenu", "LayoutUpdated", menu, None,
                                   Gio.DBusSignalFlags.NONE, lambda *a: layouts.append(1))
        self.host.signal_subscribe(None, "com.canonical.dbusmenu", "ItemsPropertiesUpdated", menu, None,
                                   Gio.DBusSignalFlags.NONE, lambda *a: props.append(a[5].unpack()))
        self.tray.set_menu([MenuItem("Syncing work"), MenuItem("10% · 1 of 9 files"), MenuItem("Quit", lambda: None)])
        spin(2.3)
        layouts.clear()
        self.tray.set_menu([MenuItem("Syncing work"), MenuItem("40% · 4 of 9 files"), MenuItem("Quit", lambda: None)])
        spin(2.3)
        self.assertEqual(layouts, [])
        self.assertEqual(len(props), 1)
        (updated, removed), = props
        self.assertEqual([(i, p["label"]) for i, p in updated], [(2, "40% · 4 of 9 files")])
        self.assertEqual(removed, [])
        # a line more or less is a new layout
        self.tray.set_menu([MenuItem("Up to date"), MenuItem("Quit", lambda: None)])
        spin(2.3)
        self.assertEqual(len(layouts), 1)

    def test_a_click_on_a_menu_a_moment_old_never_lands_elsewhere(self):
        # the host opened the menu while a file was still transferring; the
        # file is done, the menu is shorter: "Open" must stay "Open" (with
        # numbers by position it became "Quit", and Spola quit)
        def menu(files):
            return [MenuItem("Syncing work"),
                    MenuItem("Transferring Now", children=[MenuItem(f"f{i}.jpg", lambda: None, name=f"f{i}")
                                                           for i in range(files)], name="transferring"),
                    MenuItem("Open", lambda: self.clicked.append("open"), name="open"),
                    MenuItem("Quit", lambda: self.clicked.append("quit"), name="quit")]
        menu_path = "/StatusNotifierItem/Menu"
        self.tray.set_menu(menu(2))
        _rev, (_id, _props, children) = self.call(menu_path, "com.canonical.dbusmenu", "GetLayout",
                                                  GLib.Variant("(iias)", (0, -1, []))).unpack()
        seen = {c[1].get("label"): c[0] for c in children}
        self.tray.set_menu(menu(1))           # what the host has not fetched yet
        self.call(menu_path, "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (seen["Open"], "clicked", GLib.Variant("s", ""), 0)))
        spin(0.3)
        self.assertEqual(self.clicked, ["open"])
        # a number that belonged to an item no longer there does nothing
        self.tray.set_menu([MenuItem("Up to date"), MenuItem("Quit", lambda: self.clicked.append("quit"), name="quit")])
        self.call(menu_path, "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (seen["Open"], "clicked", GLib.Variant("s", ""), 0)))
        spin(0.3)
        self.assertEqual(self.clicked, ["open"])

    def test_menu_updates_are_throttled(self):
        updates = []
        self.host.signal_subscribe(None, "com.canonical.dbusmenu", "LayoutUpdated", "/StatusNotifierItem/Menu",
                                   None, Gio.DBusSignalFlags.NONE, lambda *a: updates.append(1))
        spin(2.2)
        for i in range(20):
            self.tray.set_menu([MenuItem(f"file-{i}.txt")])
            spin(0.05)
        spin(2.5)
        self.assertGreaterEqual(len(updates), 1)
        self.assertLessEqual(len(updates), 2)
        _rev, (_id, _props, children) = self.call("/StatusNotifierItem/Menu", "com.canonical.dbusmenu", "GetLayout",
                                                  GLib.Variant("(iias)", (0, -1, []))).unpack()
        self.assertEqual([c[1]["label"] for c in children], ["file-19.txt"])


if __name__ == "__main__":
    unittest.main()
