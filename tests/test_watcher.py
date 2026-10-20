"""The tree watcher of Linux (Gio, a monitor per folder): a small tree is
watched at once, a big one found in a thread and watched a batch at a time,
and a stop in between adds nothing more. Gio's monitors work on Windows
too, so this runs there as well (Spola itself uses winwatch.py there)."""

import os
import tempfile
import time
import unittest
from unittest import mock

import _setup  # noqa: F401
from gi.repository import GLib

from spola import watcher


def spin(cond, timeout: float = 10.0) -> bool:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        ctx.iteration(False)
        time.sleep(0.01)
    return cond()


class GioTreeWatcherTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        for i in range(30):
            os.makedirs(os.path.join(self.root, f"d{i:02}"))
        os.makedirs(os.path.join(self.root, "skip", "inner"))       # excluded, with what is inside

    def watcher(self) -> watcher.GioTreeWatcher:
        w = watcher.GioTreeWatcher(self.root, lambda rel: None, ignore=lambda rel: rel.startswith("skip"))
        self.addCleanup(w.stop)
        return w

    def test_a_small_tree_is_watched_at_once(self):
        w = self.watcher()
        w.start()
        self.assertEqual(w.count, 31)                   # the root and its 30 folders, no main loop needed

    def test_a_big_tree_is_watched_a_batch_at_a_time(self):
        with mock.patch.object(watcher, "WALK_BUDGET_S", -1), mock.patch.object(watcher, "BATCH", 7):
            w = self.watcher()
            w.start()
            self.assertEqual(w.count, 1)                # the root at once, the rest from the thread
            self.assertTrue(spin(lambda: w.count == 31), w.count)
        w.stop()
        self.assertEqual(w.count, 0)

    def test_a_stop_during_the_walk_adds_nothing_after(self):
        with mock.patch.object(watcher, "WALK_BUDGET_S", -1), mock.patch.object(watcher, "BATCH", 7):
            w = self.watcher()
            w.start()
            w.stop()
            spin(lambda: False, 1)
        self.assertEqual(w.count, 0)


if __name__ == "__main__":
    unittest.main()
