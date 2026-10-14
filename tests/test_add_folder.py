#!/usr/bin/env python3
"""The Add Folder dialog: Add stays off while the local folder cannot be
synced, whichever side was chosen first."""

import locale
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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

from spola.add_folder import AddFolderDialog  # noqa: E402
from spola.config import Config  # noqa: E402


class AddButtonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "remote" / "work").mkdir(parents=True)
        engine = SimpleNamespace(cfg=Config(), dev_remote=str(Path(self.tmp.name) / "remote"))
        self.dialog = AddFolderDialog(SimpleNamespace(engine=engine))

    def test_a_refused_local_folder_keeps_add_off(self):
        home = os.path.expanduser("~")
        # the Koofr folder first, from the browser, then a folder Spola refuses
        self.dialog.chosen("/work")
        self.dialog.set_local(home)                         # the whole home folder: too broad
        self.assertTrue(self.dialog.local_refused)
        self.assertFalse(self.dialog.add_button.get_sensitive())
        self.dialog.chosen("/work")                         # choosing again changes nothing
        self.assertFalse(self.dialog.add_button.get_sensitive())
        with tempfile.TemporaryDirectory(dir=home) as fine:
            self.dialog.set_local(fine)
            self.assertFalse(self.dialog.local_refused)
            self.assertTrue(self.dialog.add_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
