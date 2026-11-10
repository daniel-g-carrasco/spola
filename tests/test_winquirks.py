#!/usr/bin/env python3
"""GTK 4.24's quirks on Windows, worked around per window (winquirks.py): a
restore made by Windows that leaves the maximized size, a popover that comes
back from a minimize. Real windows, on Windows only (a hidden-ish one: tiny
and nearly transparent, off to the side, a few seconds)."""

import ctypes
import time
import unittest

import _setup

if _setup.WINDOWS:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    from spola.gui import winquirks

SW_MAXIMIZE, SW_RESTORE = 3, 9


def spin(seconds: float) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.01)


def spin_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    return cond()


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class QuirksTest(unittest.TestCase):
    def setUp(self):
        Adw.init()
        self.user32 = ctypes.windll.user32
        self.window = Gtk.Window(title="spola-winquirks-test", default_width=300, default_height=200, opacity=0.05)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        menu = Gio.Menu()
        menu.append("Item", "app.none")
        self.button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(self.button)
        view.add_top_bar(header)
        view.set_content(Gtk.Label(label="test"))
        self.window.set_child(view)
        winquirks.watch(self.window)
        self.window.present()
        self.assertTrue(spin_until(lambda: self.window.get_surface() is not None and self.hwnd()))
        spin(0.4)
        self.addCleanup(self.window.destroy)

    def hwnd(self):
        return self.user32.FindWindowW(None, "spola-winquirks-test")

    def surface_size(self):
        s = self.window.get_surface()
        return s.get_width(), s.get_height()

    def test_a_restore_made_by_windows_gets_the_remembered_size_back(self):
        small = self.surface_size()
        self.window.maximize()
        self.assertTrue(spin_until(lambda: self.window.is_maximized()))
        spin(0.5)
        big = self.surface_size()
        self.assertGreater(big[0], small[0] + 100)
        self.user32.ShowWindow(self.hwnd(), SW_RESTORE)          # the title bar's button
        self.assertTrue(spin_until(lambda: not self.window.is_maximized()))
        # without the workaround the surface stays at `big` (GTK 4.24.1); with it, back to `small`
        self.assertTrue(spin_until(lambda: self.surface_size()[0] <= small[0] + winquirks.SLACK, 5), self.surface_size())
        self.assertFalse(self.window.is_maximized())
        self.assertFalse(self.user32.IsZoomed(self.hwnd()))

    def test_a_maximize_made_by_windows_survives_the_first_popover(self):
        small = self.surface_size()
        self.user32.ShowWindow(self.hwnd(), SW_MAXIMIZE)         # the title bar's button
        self.assertTrue(spin_until(lambda: self.window.is_maximized()))
        spin(0.6)
        big = self.surface_size()
        self.assertGreater(big[0], small[0] + 100)
        self.button.activate()                                   # the main menu, as a click opens it
        spin(0.9)
        # without the workaround the surface is back to `small` here, the window still "maximized"
        self.assertGreater(self.surface_size()[0], small[0] + 100, self.surface_size())
        self.assertTrue(self.window.is_maximized())
        self.button.get_popover().popdown()

    def test_a_popover_open_at_a_minimize_is_closed_when_the_window_comes_back(self):
        popover = self.button.get_popover()
        popover.popup()
        self.assertTrue(spin_until(popover.get_visible))
        self.window.minimize()
        self.assertTrue(spin_until(lambda: self.window.get_property("suspended"), 5))
        spin(0.3)
        self.window.unminimize()
        self.window.present()
        self.assertTrue(spin_until(lambda: not self.window.get_property("suspended"), 5))
        # GTK shows it again on its own, without the grab; the workaround closes it
        self.assertTrue(spin_until(lambda: not popover.get_visible(), 3))
        spin(0.3)
        self.assertFalse(popover.get_visible())

    def test_the_popovers_of_a_tree_are_found(self):
        found = list(winquirks.popovers_below(self.window))
        self.assertEqual(found, [self.button.get_popover()])
        self.assertEqual(winquirks.close_open_popovers(self.window), 0)


if __name__ == "__main__":
    unittest.main()
