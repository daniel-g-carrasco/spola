#!/usr/bin/env python3
"""Importing from Koofr Desktop in the window: the folders only once Koofr
Desktop is stopped and kept from starting, then the chosen ones added,
paused if they were paused there."""

import locale
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

from spola import import_dialog, migrate, preflight  # noqa: E402
from spola.config import Config  # noqa: E402

State = preflight.KoofrDesktopState


class ImportDialogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.added, self.toasts = [], []
        engine = SimpleNamespace(cfg=Config(), add_pair=lambda local, remote, paused=False:
                                 self.added.append((local, remote, paused)))
        self.window = SimpleNamespace(engine=engine, toast=self.toasts.append)
        self.state = State(running=True, at_login=True)
        for target, value in ((preflight, "koofr_desktop_state"), (preflight, "koofr_desktop_active")):
            patcher = mock.patch.object(target, value, side_effect=(
                (lambda *a: self.state) if value == "koofr_desktop_state" else (lambda *a: self.state.active)))
            patcher.start()
            self.addCleanup(patcher.stop)
        t = Path(self.tmp.name)
        self.candidates = [migrate.Candidate(str(t / "work"), "/work", False),
                           migrate.Candidate(str(t / "photos"), "/photos", True),
                           migrate.Candidate(str(t / "shared"), "/shared", False, "not in your own Koofr storage")]
        patcher = mock.patch.object(migrate, "candidates", return_value=self.candidates)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(migrate, "problem_of", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def dialog(self):
        dialog = import_dialog.ImportDialog(self.window)
        self.addCleanup(dialog._stop_polling)
        return dialog

    def test_the_folders_only_once_koofr_desktop_is_stopped(self):
        dialog = self.dialog()
        self.assertFalse(dialog.next_button.get_sensitive())
        self.state = State(running=False, at_login=True)        # quit, still starting at login
        dialog._refresh()
        self.assertFalse(dialog.next_button.get_sensitive())
        self.state = State(running=False, at_login=False)
        dialog._refresh()
        self.assertTrue(dialog.next_button.get_sensitive())

    def test_the_chosen_folders_are_added(self):
        self.state = State(running=False, at_login=False)
        dialog = self.dialog()
        dialog._to_folders()
        checks = {Path(c.local).name: check for check, c in dialog.checks}
        self.assertTrue(checks["work"].get_active())
        self.assertFalse(checks["shared"].get_sensitive())       # cannot be taken
        checks["work"].set_active(False)
        self.assertTrue(dialog.import_button.get_sensitive())
        dialog._import()
        self.assertEqual([(Path(local).name, remote, paused) for local, remote, paused in self.added],
                         [("photos", "/photos", True)])
        self.assertEqual(len(self.toasts), 1)

    def test_koofr_desktop_started_again_sends_back(self):
        self.state = State(running=False, at_login=False)
        dialog = self.dialog()
        dialog._to_folders()
        self.state = State(running=True, at_login=False)
        dialog._import()
        self.assertEqual(self.added, [])
        self.assertFalse(dialog.next_button.get_sensitive())


if __name__ == "__main__":
    unittest.main()
