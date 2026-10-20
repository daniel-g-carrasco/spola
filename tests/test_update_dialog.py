#!/usr/bin/env python3
"""The update dialog opens with the focus on its action, not on the notes
(a selectable label given the focus has all its text selected)."""

import locale
import unittest

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola import updates  # noqa: E402
from spola.update_dialog import UpdateDialog  # noqa: E402


class FocusTest(unittest.TestCase):
    def test_the_focus_on_the_action(self):
        release = updates.Release("99.0.0", "https://example/release", "Notes\n\n- one line", [])
        dialog = UpdateDialog(release)
        self.assertIs(dialog.get_focus(), dialog.action)
        self.assertIs(dialog.get_default_widget(), dialog.action)

    def test_later_as_the_download_ends_starts_nothing(self):
        from pathlib import Path
        from unittest import mock
        dialog = UpdateDialog(updates.Release("99.0.0", "https://example/release", "", []))
        dialog._cancel.set()            # "Later" (the dialog closed) once the last chunk was in
        with mock.patch("spola.update_dialog.subprocess.Popen") as popen:
            dialog._downloaded(Path("spola-setup-v99.0.0-windows-x64.exe"))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
