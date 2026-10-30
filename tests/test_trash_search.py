"""A shared trash search can be stopped and resumed without losing pages."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
from gi.repository import GLib

from spola import engine as engine_mod
from spola.account import Credentials
from spola.config import Config
from spola.engine import Engine
from spola.paths import Paths
from spola.trash import Entry


def spin_until(condition, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        GLib.MainContext.default().iteration(False)
        time.sleep(0.001)
    return bool(condition())


class TrashSearchTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.engine = Engine(Paths(root / "config", root / "state", root / "data"), Config())
        self.addCleanup(self.engine.shutdown, lambda: None)
        self.engine.creds = Credentials("test", "")
        self.enterContext(mock.patch.object(engine_mod, "TRASH_RETRY_S", 0))
        self.first = Entry("m", "/work/a.txt", 10, 20_000)
        self.second = Entry("m", "/work/b.txt", 10, 10_000)

    def read(self):
        seen = []
        stop = self.engine.trash_entries(lambda entries, done, error: seen.append((entries, done, error)))
        return seen, stop

    def test_a_failed_page_is_resumed_without_reading_the_first_pages_again(self):
        calls, broken = [], [True]

        def page(cursor):
            calls.append(cursor)
            if cursor is None:
                return [self.first], "next"
            if broken[0]:
                raise TimeoutError("slow page")
            return [self.second], None

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, _stop = self.read()
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.assertEqual(seen[-1], ([self.first], True, "slow page"))
        broken[0] = False
        calls.clear()
        again, _stop = self.read()
        self.assertTrue(spin_until(lambda: again and again[-1][1]))
        self.assertEqual(calls, ["next"])
        self.assertEqual(again[-1], ([self.first, self.second], True, ""))

    def test_closing_the_last_reader_stops_retries_and_keeps_the_resume_point(self):
        gate, waiting, returned = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        calls, slow = [], [True]

        def page(cursor):
            calls.append(cursor)
            if cursor is None:
                return [self.first], "next"
            if slow[0]:
                waiting.set()
                gate.wait(3)
                returned.set()
                raise TimeoutError("slow page")
            return [self.second], None

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, stop = self.read()
        self.assertTrue(spin_until(lambda: seen and seen[-1][0] == [self.first] and waiting.is_set()))
        stop()
        self.assertFalse(self.engine.trash_read.loading)
        before = list(seen)
        gate.set()
        self.assertTrue(returned.wait(3))
        # A queued page/failure from the cancelled worker cannot reach a new reader.
        slow[0] = False
        again, _stop = self.read()
        self.assertTrue(spin_until(lambda: again and again[-1][1]))
        self.assertEqual(calls, [None, "next", "next"])
        self.assertEqual(seen, before)
        self.assertEqual(again[-1], ([self.first, self.second], True, ""))

    def test_closing_one_reader_leaves_the_other_reading(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def page(cursor):
            if cursor is None:
                return [self.first], "next"
            gate.wait(3)
            return [self.second], None

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        one, stop = self.read()
        two, _stop = self.read()
        self.assertTrue(spin_until(lambda: one and two))
        stop()
        self.assertTrue(self.engine.trash_read.loading)
        before = list(one)
        gate.set()
        self.assertTrue(spin_until(lambda: two and two[-1][1]))
        self.assertEqual(two[-1], ([self.first, self.second], True, ""))
        self.assertEqual(one, before)

    def test_a_cached_delivery_can_be_cancelled_before_it_reaches_a_closed_dialog(self):
        self.engine.trash_read.entries = [self.first]
        self.engine.trash_read.read_at = time.time()
        seen, stop = self.read()
        stop()
        drained = []
        GLib.idle_add(lambda: drained.append(True) and False)
        self.assertTrue(spin_until(lambda: drained))
        self.assertEqual(seen, [])

    def test_invalidating_a_partial_read_starts_from_the_new_first_page(self):
        calls = []

        def page(cursor):
            calls.append(cursor)
            if cursor is None:
                return [self.first], "next"
            raise TimeoutError("slow page")

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, _stop = self.read()
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.engine.invalidate_trash()
        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(
            lambda cursor: (calls.append(cursor) or [self.second], None))})()
        calls.clear()
        again, _stop = self.read()
        self.assertTrue(spin_until(lambda: again and again[-1][1]))
        self.assertEqual(calls, [None])
        self.assertEqual(again[-1], ([self.second], True, ""))

    def test_a_pagination_limit_is_reported_as_an_incomplete_read(self):
        self.enterContext(mock.patch.object(engine_mod.trash_mod, "MAX_PAGES", 2))
        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(
            lambda cursor: ([self.first], str(int(cursor or 0) + 1)))})()
        seen, _stop = self.read()
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.assertTrue(seen[-1][2])
        self.assertEqual(self.engine.trash_read.read_at, 0)

    def test_a_cache_invalidated_before_delivery_is_read_again(self):
        self.engine.trash_read.entries = [self.first]
        self.engine.trash_read.read_at = time.time()
        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(
            lambda cursor: ([self.second], None))})()
        seen, _stop = self.read()
        self.engine.invalidate_trash()
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.assertEqual(seen[-1], ([self.second], True, ""))

    def test_a_resumed_read_shows_its_kept_entries_while_the_next_page_waits(self):
        self.engine.trash_read.entries = [self.first]
        self.engine.trash_read.cursor = "next"
        self.engine.trash_read.resumable = True
        self.engine.trash_read.paused_at = time.time()
        gate = threading.Event()
        self.addCleanup(gate.set)

        def page(cursor):
            gate.wait(3)
            return [self.second], None

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, stop = self.read()
        self.addCleanup(stop)
        self.assertTrue(spin_until(lambda: seen, timeout=0.5))
        self.assertEqual(seen[0], ([self.first], False, ""))

    def test_pausing_as_the_last_page_arrives_cannot_repeat_its_entries(self):
        calls, seen = [], []
        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(
            lambda cursor: (calls.append(cursor) or [self.first], None))})()

        def arrived(entries, done, error):
            seen.append((entries, done, error))
            if entries and not done:
                stop()

        stop = self.engine.trash_entries(arrived)
        self.assertTrue(spin_until(lambda: any(entries for entries, _done, _error in seen)))
        again, _stop = self.read()
        self.assertTrue(spin_until(lambda: again and again[-1][1]))
        self.assertEqual(again[-1], ([self.first], True, ""))
        self.assertEqual(calls, [None])

    def test_an_empty_page_with_a_cursor_is_the_end_of_the_list(self):
        # Koofr ends with a full page and no cursor (88 pages on 2026-10-30); an empty
        # page, or a cursor that does not move, must be the end too, never an error
        def page(cursor):
            if cursor is None:
                return [self.first], "next"
            return [], "more"

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, stop = self.read()
        self.addCleanup(stop)
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.assertEqual(seen[-1], ([self.first], True, ""))
        self.assertTrue(self.engine.trash_read.read_at)

    def test_a_stopped_read_older_than_the_cache_starts_from_the_first_page(self):
        calls, broken = [], [True]

        def page(cursor):
            calls.append(cursor)
            if cursor is None:
                return [self.first], "next"
            if broken[0]:
                raise TimeoutError("slow page")
            return [self.second], None

        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(page)})()
        seen, _stop = self.read()
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        broken[0] = False
        self.engine.trash_read.paused_at -= engine_mod.TRASH_TTL_S + 1
        calls.clear()
        again, _stop = self.read()
        self.assertTrue(spin_until(lambda: again and again[-1][1]))
        self.assertEqual(calls, [None, "next"])
        self.assertEqual(again[-1], ([self.first, self.second], True, ""))

    def test_an_empty_cursor_finishes_the_read(self):
        self.engine.koofr_trash = lambda creds: type("Api", (), {"page": staticmethod(
            lambda cursor: ([self.first], ""))})()
        seen, stop = self.read()
        self.addCleanup(stop)
        self.assertTrue(spin_until(lambda: seen and seen[-1][1]))
        self.assertEqual(seen[-1], ([self.first], True, ""))
