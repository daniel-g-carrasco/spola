#!/usr/bin/env python3
"""The update dialog opens with the focus on its action and nothing selected
(a selectable label given the focus has all its text selected); what is new
comes as rows under the changelog's headings; a failure is said in a banner."""

import locale
import unittest

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
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola.app import updates  # noqa: E402
from spola.gui.update_dialog import UpdateDialog  # noqa: E402


NOTES = """### Fixed
- Renaming a whole folder no longer destroys
  the public links that point to it.
- A second thing.

### Added
- A sync that would remove a link stops first.

**Full Changelog**: https://github.com/x/y/compare/v1...v2
"""


def widgets(root):
    child = root.get_first_child()
    while child is not None:
        yield child
        yield from widgets(child)
        child = child.get_next_sibling()


class LayoutTest(unittest.TestCase):
    def test_what_is_new_as_rows_and_no_text_to_select(self):
        dialog = UpdateDialog(updates.Release("99.0.0", "https://example/release", NOTES, []))
        self.assertEqual([row.get_title() for row in dialog.note_rows],
                         ["Renaming a whole folder no longer destroys the public links that point to it.",
                          "A second thing.", "A sync that would remove a link stops first."])
        selectable = [w for w in widgets(dialog) if isinstance(w, Gtk.Label) and w.get_selectable()]
        self.assertEqual(selectable, [])
        self.assertFalse(dialog.progress_group.get_visible())
        self.assertFalse(dialog.banner.get_revealed())

    def test_a_failed_download_is_said_and_can_be_tried_again(self):
        dialog = UpdateDialog(updates.Release("99.0.0", "https://example/release", "", []))
        dialog.action.set_sensitive(False)
        dialog.progress_group.set_visible(True)
        dialog._failed("Download failed: no network")
        self.assertTrue(dialog.banner.get_revealed())
        self.assertEqual(dialog.banner.get_title(), "Download failed: no network")
        self.assertTrue(dialog.action.get_sensitive())
        self.assertFalse(dialog.progress_group.get_visible())


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
        with mock.patch("spola.gui.update_dialog.subprocess.Popen") as popen:
            dialog._downloaded(Path("spola-setup-v99.0.0-windows-x64.exe"))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
