#!/usr/bin/env python3
"""Preferences in Windows' separate window: a bounded opening size and
room to resize, with the settings still available by scrolling."""

import locale
import unittest
from types import SimpleNamespace
from unittest import mock

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola import window  # noqa: E402
from spola.config import Config  # noqa: E402


class PreferencesDialogTest(unittest.TestCase):
    def dialog(self, windows):
        parent = (Gtk.Window if windows else Adw.Window)(default_width=480, default_height=640)
        self.addCleanup(parent.destroy)
        parent.engine = SimpleNamespace(cfg=Config(), creds=None)
        with mock.patch.object(window, "WINDOWS", windows):
            dialog = window.PreferencesDialog(parent)
            dialog.present(parent)
        self.addCleanup(dialog.force_close)
        return dialog, parent

    def test_windows_preferences_can_be_resized(self):
        dialog, parent = self.dialog(windows=True)
        root = dialog.get_root()
        self.assertIsInstance(root, Gtk.Window)
        self.assertIsNot(root, parent)
        self.assertTrue(root.get_resizable())
        self.assertIs(root.get_transient_for(), parent)
        self.assertTrue(root.get_modal())

    def test_windows_preferences_open_below_a_full_screen_of_settings(self):
        dialog, _parent = self.dialog(windows=True)
        root = dialog.get_root()
        width, height = root.get_default_size()
        self.assertGreater(height, 300)
        self.assertLessEqual(height, 700)
        minimum, _natural, _min_baseline, _nat_baseline = root.measure(Gtk.Orientation.VERTICAL, width)
        self.assertLess(minimum, height)     # the settings can scroll when the window shrinks

    def test_linux_preferences_remain_inside_the_main_window(self):
        dialog, parent = self.dialog(windows=False)
        self.assertIs(dialog.get_root(), parent)
        self.assertEqual(parent.get_default_size(), (480, 640))


if __name__ == "__main__":
    unittest.main()
