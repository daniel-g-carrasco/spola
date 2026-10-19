#!/usr/bin/env python3
"""The system font a size larger on Windows: 9 points become 10.5, Spola's
own value is left alone, a new size from Windows is scaled again."""

import locale
import unittest

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gdk, Gtk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
initialised = Gtk.init_check()
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)
if not initialised or Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola.app import larger_system_font  # noqa: E402


class FontTest(unittest.TestCase):
    def test_a_size_larger_and_following_windows(self):
        settings = Gtk.Settings.get_default()
        before = settings.props.gtk_font_name
        self.addCleanup(settings.reset_property, "gtk-font-name")
        settings.props.gtk_font_name = "Segoe UI 9"
        self.addCleanup(settings.disconnect, larger_system_font())
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI 10.5")
        settings.props.gtk_font_name = "Segoe UI 11"            # a larger text size chosen in Windows
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI 13")
        settings.props.gtk_font_name = "Segoe UI Bold 9"
        self.assertEqual(settings.props.gtk_font_name, "Segoe UI Bold 10.5")
        self.assertTrue(before)


if __name__ == "__main__":
    unittest.main()
