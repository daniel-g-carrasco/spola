#!/usr/bin/env python3
"""GTK 4.24's quirks on Windows, worked around per window (winquirks.py): a
maximized window that the menu shrinks, a popover that comes back from a
minimize, a maximize at the start that must be Windows' own. Real windows,
on Windows only (nearly transparent, a few seconds). Nothing is typed or
clicked: Windows' own maximize and restore are asked with ShowWindow."""

import ctypes
import os
import time
import unittest
from ctypes import wintypes
from unittest import mock

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


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class DialogWindowTest(unittest.TestCase):
    """A dialog's window, where the parent is GTK's own (as Spola's main
    window is on Windows): Windows' frame and title bar, no window button in
    the header bar, in the middle of its parent. An alert is only centred."""

    WS_CAPTION, WS_THICKFRAME, WS_MINIMIZEBOX = 0x00C00000, 0x00040000, 0x00020000

    def setUp(self):
        Adw.init()
        patch = mock.patch.dict(os.environ, {"GTK_CSD": "0"})       # as the app sets it at its start
        patch.start()
        self.addCleanup(patch.stop)
        self.user32 = ctypes.WinDLL("user32")
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = ctypes.c_long
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, wintypes.UINT]
        toplevels = Gtk.Window.get_toplevels()
        handler = toplevels.connect("items-changed", lambda model, position, _removed, count: [
            winquirks.watch_dialog(model.get_item(i)) for i in range(position, position + count)])
        self.addCleanup(toplevels.disconnect, handler)
        self.parent = Gtk.Window(title=TITLE + "-parent", default_width=500, default_height=400, opacity=0.05)
        self.parent.set_child(Gtk.Label(label="parent"))
        self.parent.present()
        self.addCleanup(self.parent.destroy)
        self.assertTrue(spin_until(lambda: self.parent.get_mapped()))
        spin(0.3)
        # away from the corner a window opens at when nothing places it
        self.user32.SetWindowPos(winquirks.hwnd_of(self.parent), None, 300, 200, 0, 0, 0x0001 | 0x0004 | 0x0010)
        spin(0.3)

    def shown(self, dialog):
        dialog.present(self.parent)
        self.addCleanup(dialog.force_close)
        self.assertTrue(spin_until(lambda: dialog.get_mapped()))
        spin(0.5)
        root = dialog.get_root()
        self.assertIsNot(root, self.parent)         # a window of its own
        return root, winquirks.hwnd_of(root)

    def rect(self, hwnd):
        r = wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def assert_centred(self, hwnd):
        left, top, right, bottom = self.rect(hwnd)
        p_left, p_top, p_right, p_bottom = self.rect(winquirks.hwnd_of(self.parent))
        self.assertLessEqual(abs((left + right) - (p_left + p_right)), 2)
        self.assertLessEqual(abs((top + bottom) - (p_top + p_bottom)), 2)

    def test_a_dialog_with_a_header_bar_has_windows_frame(self):
        dialog = Adw.Dialog(title="A dialog", content_width=300, content_height=200)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        view.set_content(Gtk.Label(label="content"))
        dialog.set_child(view)
        dialog.connect("realize", lambda d: d.get_root().set_resizable(True))      # as Spola's resizable dialogs do
        root, hwnd = self.shown(dialog)
        style = self.user32.GetWindowLongW(hwnd, -16)
        self.assertIsNone(root.get_titlebar())
        self.assertEqual(style & self.WS_CAPTION, self.WS_CAPTION)          # the system's title bar
        self.assertTrue(style & self.WS_THICKFRAME)                         # resized by Windows
        self.assertFalse(style & self.WS_MINIMIZEBOX)
        self.assertFalse(header.get_show_end_title_buttons())               # the title bar has the close button
        self.assertFalse(header.get_show_start_title_buttons())
        self.assertFalse(header.get_show_title())                           # and the title
        self.assert_centred(hwnd)

    def test_the_title_is_said_once(self):
        named = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Share", subtitle="report.pdf"))
        winquirks.title_once(named, "Share")
        self.assertEqual((named.get_title_widget().get_title(), named.get_title_widget().get_subtitle()),
                         ("report.pdf", ""))
        other = Adw.HeaderBar(title_widget=Adw.WindowTitle(title="Projects", subtitle="Documents"))
        winquirks.title_once(other, "Share")
        self.assertEqual(other.get_title_widget().get_title(), "Projects")
        page = Adw.NavigationPage(title="Advanced")
        inside = Adw.HeaderBar()
        page.set_child(inside)
        winquirks.title_once(inside, "Preferences")
        self.assertTrue(inside.get_show_title())                # a page's own title stays

    def test_an_alert_keeps_its_frame_and_is_centred(self):
        alert = Adw.AlertDialog(heading="A question", body="Made up.")
        alert.add_response("ok", "OK")
        root, hwnd = self.shown(alert)
        self.assertIsNotNone(root.get_titlebar())
        self.assertFalse(self.user32.GetWindowLongW(hwnd, -16) & self.WS_CAPTION == self.WS_CAPTION)
        self.assert_centred(hwnd)

    def test_a_dialog_is_put_in_front_as_it_shows(self):
        # whether Windows grants it depends on who is in front (not this test): the request is made, on the right window
        dialog = Adw.Dialog(title="A dialog", content_width=300, content_height=200)
        dialog.set_child(Gtk.Label(label="content"))
        asked = []
        with mock.patch.object(winquirks._user32, "SetForegroundWindow", side_effect=asked.append):
            dialog.present(self.parent)
            self.addCleanup(dialog.force_close)
            winquirks.to_front(dialog)          # right after present(), as the app does
            self.assertTrue(spin_until(lambda: bool(asked)))
            self.assertEqual(asked, [winquirks.hwnd_of(dialog.get_root())])

    def test_the_middle_stays_on_the_screen(self):
        area = (0, 0, 1920, 1040)
        self.assertEqual(winquirks.centred((400, 300), (100, 100, 1100, 900), area), (400, 350))
        self.assertEqual(winquirks.centred((400, 300), (-300, -200, 100, 100), area), (0, 0))        # parent off the edge
        self.assertEqual(winquirks.centred((400, 300), (1800, 900, 2400, 1300), area), (1520, 740))
        self.assertEqual(winquirks.centred((400, 1200), (100, 100, 1100, 900), area), (400, 0))      # taller than the screen: its top


if __name__ == "__main__":
    unittest.main()
