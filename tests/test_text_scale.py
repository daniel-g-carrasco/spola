#!/usr/bin/env python3
"""Windows' fractional scales: GTK scales by whole numbers, the text makes
up for the rest (gtk-xft-dpi)."""

import locale
import unittest

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw  # noqa: F401
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)

from spola.window import text_dpi  # noqa: E402


class TextDpiTest(unittest.TestCase):
    def test_the_fraction_gtk_leaves_out(self):
        self.assertEqual(text_dpi(240, 2), 122880)      # 250% at GTK's 2: text 1.25 larger
        self.assertEqual(text_dpi(144, 1), 147456)      # 150% at GTK's 1: 1.5
        self.assertEqual(text_dpi(120, 1), 122880)      # 125%
        self.assertEqual(text_dpi(192, 2), -1)          # whole scales: GTK's own
        self.assertEqual(text_dpi(96, 1), -1)
        self.assertEqual(text_dpi(288, 3), -1)
        self.assertEqual(text_dpi(240, 0), 245760)      # no scale yet: taken as 1


if __name__ == "__main__":
    unittest.main()
