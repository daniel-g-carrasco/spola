"""Trash searches show progress and can be paused without authorizing a partial restore."""

import locale
import tempfile
import threading
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
    from gi.repository import Adw, Gdk, Gtk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola.account import Credentials  # noqa: E402
from spola.config import Config, Pair  # noqa: E402
from spola.engine import Engine  # noqa: E402
from spola import engine as engine_mod  # noqa: E402
from spola.paths import Paths  # noqa: E402
from spola.trash import Entry  # noqa: E402
from spola.window import TrashDialog, VersionsDialog  # noqa: E402
from test_trash_search import spin_until  # noqa: E402


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from descendants(child)
        child = child.get_next_sibling()


class TrashDialogTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        local = root / "local"
        local.mkdir()
        pair = Pair("p", str(local), "/work")
        self.engine = Engine(Paths(root / "config", root / "state", root / "data"),
                             Config(pairs=[pair], mount_id="m"))
        self.addCleanup(self.engine.shutdown, lambda: None)
        self.engine.creds = Credentials("test", "")
        self.window = SimpleNamespace(engine=self.engine, toast=lambda text: None)
        self.gate, self.waiting = threading.Event(), threading.Event()
        self.addCleanup(self.gate.set)
        self.first = Entry("m", "/work/old.txt", 10, 20_000)
        self.second = Entry("m", "/elsewhere/other.txt", 10, 10_000)
        self.calls = []

        def page(cursor):
            self.calls.append(cursor)
            if cursor is None:
                return [self.first], "next"
            self.waiting.set()
            self.gate.wait(3)
            return [self.second], None

        self.engine.koofr_trash = lambda creds: SimpleNamespace(page=page)

    def test_restore_shows_progress_before_the_last_page_and_waits_to_offer_groups(self):
        dialog = TrashDialog(self.window, "p")
        self.addCleanup(lambda: dialog.emit("closed"))
        self.assertTrue(spin_until(lambda: self.engine.trash_read.entries and self.waiting.is_set()))
        rows = [w for w in descendants(dialog.get_child()) if isinstance(w, Adw.ActionRow)]
        self.assertTrue(any("1 " in row.get_subtitle() for row in rows),
                        [row.get_subtitle() for row in rows])
        self.assertFalse(dialog.restore.get_sensitive())
        self.assertFalse(any(isinstance(w, Gtk.CheckButton) for w in descendants(dialog.get_child())))
        self.gate.set()
        self.assertTrue(spin_until(lambda: any(isinstance(w, Gtk.CheckButton) for w in descendants(dialog.get_child()))))
        check = next(w for w in descendants(dialog.get_child()) if isinstance(w, Gtk.CheckButton))
        check.set_active(True)
        self.assertTrue(dialog.restore.get_sensitive())

    def test_search_can_be_paused_and_continued_in_both_dialogs(self):
        for factory in (lambda: TrashDialog(self.window, "p"),
                        lambda: VersionsDialog(self.window, "p", "", "deleted")):
            with self.subTest(factory=factory):
                self.engine.invalidate_trash()
                self.gate.clear()
                dialog = factory()
                self.addCleanup(lambda d=dialog: d.emit("closed"))
                self.assertTrue(spin_until(lambda: self.engine.trash_read.entries and self.waiting.is_set()))
                self.assertTrue(hasattr(dialog, "trash_search"), "trash search has no pause control")
                dialog.trash_search.action.emit("clicked")
                self.assertFalse(dialog.trash_search.loading)
                self.assertFalse(self.engine.trash_read.loading)
                self.assertTrue(dialog.trash_search.action.get_sensitive())
                self.gate.set()
                dialog.trash_search.action.emit("clicked")
                self.assertTrue(spin_until(lambda d=dialog: d.trash_search.complete))
                self.assertEqual(dialog.trash_search.entries, [self.first, self.second])
                self.assertFalse(dialog.trash_search.get_visible())

    def test_closing_a_restore_dialog_stops_its_search(self):
        dialog = TrashDialog(self.window, "p")
        self.assertTrue(spin_until(lambda: self.engine.trash_read.entries and self.waiting.is_set()))
        dialog.emit("closed")
        self.assertFalse(self.engine.trash_read.loading)
        self.gate.set()

    def test_an_error_keeps_progress_and_retry_without_offering_a_partial_group(self):
        self.enterContext(mock.patch.object(engine_mod, "TRASH_RETRY_S", 0))

        def page(cursor):
            if cursor is None:
                return [self.first], "next"
            raise TimeoutError("slow page")

        self.engine.koofr_trash = lambda creds: SimpleNamespace(page=page)
        dialog = TrashDialog(self.window, "p")
        self.addCleanup(lambda: dialog.emit("closed"))
        self.assertTrue(spin_until(lambda: not self.engine.trash_read.loading))
        self.assertEqual(dialog.trash_search.entries, [self.first])
        self.assertFalse(dialog.trash_search.complete)
        self.assertIn("slow page", dialog.trash_search.get_subtitle())
        self.assertTrue(dialog.trash_search.action.get_sensitive())
        self.assertFalse(dialog.restore.get_sensitive())
        self.assertFalse(any(isinstance(w, Gtk.CheckButton) for w in descendants(dialog.get_child())))
        self.engine.koofr_trash = lambda creds: SimpleNamespace(page=lambda cursor: ([self.second], None))
        dialog.trash_search.action.emit("clicked")
        self.assertTrue(spin_until(lambda: dialog.trash_search.complete))
        self.assertEqual(sum(isinstance(w, Gtk.CheckButton) for w in descendants(dialog.get_child())), 1)

    def test_searching_names_during_a_read_keeps_the_pause_control_and_matching_files(self):
        dialog = VersionsDialog(self.window, "p", "", "deleted")
        self.addCleanup(lambda: dialog.emit("closed"))
        self.assertTrue(spin_until(lambda: dialog.trash_entries and self.waiting.is_set()))
        dialog._search("old")
        self.assertEqual(dialog._here, [self.first])
        self.assertIsNotNone(dialog.trash_search.get_parent())
        dialog._search("absent")
        self.assertEqual(dialog._here, [])
        self.assertTrue(dialog.trash_search.get_visible())
        dialog.trash_search.action.emit("clicked")
        self.assertFalse(self.engine.trash_read.loading)
