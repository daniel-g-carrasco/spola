#!/usr/bin/env python3
"""GTK 4.24's quirks on Windows, worked around per window (winquirks.py): a
maximized window that the menu shrinks, a popover that comes back from a
minimize, a maximize at the start that must be Windows' own. Real windows,
on Windows only (nearly transparent, a few seconds). Nothing is typed or
clicked: Windows' own maximize and restore are asked with ShowWindow."""

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
TITLE = "spola-winquirks-test"


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
    def window_with_a_menu(self, watched: bool = True, maximized_at_start: bool = False):
        Adw.init()
        self.user32 = ctypes.windll.user32
        self.window = Gtk.Window(title=TITLE, default_width=300, default_height=200, opacity=0.05)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_title=False)
        menu = Gio.Menu()
        menu.append("Item", "app.none")
        self.button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(self.button)
        view.add_top_bar(header)
        view.set_content(Gtk.Label(label="test"))
        self.window.set_child(view)
        if watched:
            winquirks.watch(self.window)
        if maximized_at_start:
            winquirks.maximize_at_start(self.window)
        self.window.present()
        self.assertTrue(spin_until(lambda: self.window.get_surface() is not None and self.hwnd()))
        spin(0.5)
        self.addCleanup(self.window.destroy)
        # the size GTK remembers once the window is laid out (its content may ask for more than was given)
        self.normal = tuple(self.window.get_default_size())

    def hwnd(self):
        return self.user32.FindWindowW(None, TITLE)

    def surface_size(self):
        s = self.window.get_surface()
        return s.get_width(), s.get_height()

    def by_windows(self, command: int, maximized: bool) -> None:
        self.user32.ShowWindow(self.hwnd(), command)             # as the title bar's button
        self.assertTrue(spin_until(lambda: self.window.is_maximized() == maximized))
        spin(0.6)

    def open_the_menu(self) -> None:
        self.button.activate()                                   # the main menu, as a click opens it
        spin(0.9)

    def test_a_window_maximized_by_windows_keeps_its_size_when_the_menu_opens(self):
        self.window_with_a_menu()
        small = self.surface_size()
        self.by_windows(SW_MAXIMIZE, True)
        big = self.surface_size()
        self.assertGreater(big[0], small[0] + 100)
        self.assertEqual(winquirks.normal_size(self.window), self.normal)    # what a saved state must hold
        for _again in range(2):
            self.open_the_menu()
            self.assertEqual(self.surface_size(), big)
            self.assertTrue(self.window.is_maximized())
            self.button.get_popover().popdown()
            spin(0.3)
        # and the restore, left to Windows, gives the normal size back
        self.by_windows(SW_RESTORE, False)
        self.assertEqual(self.surface_size(), small)
        self.assertEqual(tuple(self.window.get_default_size()), self.normal)
        self.assertEqual(winquirks.normal_size(self.window), self.normal)
        self.assertFalse(self.user32.IsZoomed(self.hwnd()))

    def test_the_quirk_is_still_there_without_the_workaround(self):
        # when this fails GTK has fixed it: the workaround can go
        self.window_with_a_menu(watched=False)
        small = self.surface_size()
        self.by_windows(SW_MAXIMIZE, True)
        big = self.surface_size()
        self.assertGreater(big[0], small[0] + 100)
        self.open_the_menu()
        self.assertLess(self.surface_size()[0], big[0] - 100)       # fallen to GTK's default size
        self.assertTrue(self.window.is_maximized())                 # and still "maximized"

    def test_maximized_at_the_start_by_windows_it_restores_right(self):
        self.window_with_a_menu(maximized_at_start=True)
        self.assertTrue(spin_until(lambda: self.window.is_maximized()))
        spin(0.6)
        big = self.surface_size()
        self.assertTrue(self.user32.IsZoomed(self.hwnd()))
        self.assertEqual(winquirks.normal_size(self.window), (300, 200))     # the size it was given, not the screen's
        self.open_the_menu()
        self.assertEqual(self.surface_size(), big)
        self.button.get_popover().popdown()
        self.by_windows(SW_RESTORE, False)
        # (maximized by GTK at the start, this is where the window stays as big as the screen)
        self.assertLess(self.surface_size()[0], big[0] - 100)
        self.assertLess(self.window.get_default_size()[0], big[0] - 100)
        self.assertFalse(self.user32.IsZoomed(self.hwnd()))

    def test_a_popover_open_at_a_minimize_is_closed_when_the_window_comes_back(self):
        self.window_with_a_menu()
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
        spin(0.7)
        self.assertFalse(popover.get_visible())

    def test_the_popovers_of_a_tree_are_found(self):
        self.window_with_a_menu()
        found = list(winquirks.popovers_below(self.window))
        self.assertEqual(found, [self.button.get_popover()])
        self.assertEqual(winquirks.close_open_popovers(self.window), 0)


if __name__ == "__main__":
    unittest.main()
