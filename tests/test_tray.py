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
                            MenuItem("Status: ok")])
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
        self.assertEqual(labels, ["Sync now", None, "Status: ok"])
        self.assertFalse(children[2][1]["enabled"])       # no action: shown as text
        self.call("/StatusNotifierItem/Menu", "com.canonical.dbusmenu", "Event",
                  GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("s", ""), 0)))
        self.call(ITEM_PATH, "org.kde.StatusNotifierItem", "Activate", GLib.Variant("(ii)", (0, 0)))
        spin(0.3)
        self.assertEqual(self.clicked, ["sync", "activate"])


if __name__ == "__main__":
    unittest.main()
