#!/usr/bin/env python3
"""The tray's recently synced files: newest first, five at most, kept
across restarts, forgotten with their pair."""

import os
import tempfile
import time
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.recent import Recent, transferring


class RecentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.file = Path(self.root) / "state" / "recent.json"
        now = time.time()
        for i, name in enumerate(["a.txt", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"]):
            path = os.path.join(self.root, name)
            open(path, "w").close()
            os.utime(path, (now - 100 + i, now - 100 + i))      # f.txt is the newest

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_first_limited_and_kept(self):
        recent = Recent(self.file)
        self.assertTrue(recent.record("p1", self.root, ["a.txt", "b.txt"], ["c.txt", "d.txt", "e.txt", "f.txt",
                                                                            "gone.txt"]))
        self.assertEqual([f.path for f in recent.items], ["f.txt", "e.txt", "d.txt", "c.txt", "b.txt"])
        self.assertEqual([f.direction for f in recent.items], ["down", "down", "down", "down", "up"])
        again = Recent(self.file)
        self.assertEqual(again.items, recent.items)

    def test_a_file_synced_again_moves_up(self):
        recent = Recent(self.file)
        recent.record("p1", self.root, ["a.txt", "b.txt"], [])
        recent.record("p1", self.root, ["a.txt"], [])
        self.assertEqual([f.path for f in recent.items], ["a.txt", "b.txt"])

    def test_nothing_to_record_and_forget(self):
        recent = Recent(self.file)
        self.assertFalse(recent.record("p1", self.root, [], []))
        recent.record("p1", self.root, ["a.txt"], [])
        recent.record("p2", self.root, ["b.txt"], [])
        recent.forget_pair("p1")
        self.assertEqual([(f.pair_id, f.path) for f in Recent(self.file).items], [("p2", "b.txt")])

    def test_broken_file_is_an_empty_list(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{not json")
        self.assertEqual(Recent(self.file).items, [])

    def test_transferring(self):
        stats = [{"name": "Foto/big.bin", "srcFs": "local{HK82T}:/home/u/t", "dstFs": "koofr{5E}:t"},
                 {"name": "doc.odt", "srcFs": "koofr{5E}:t", "dstFs": "local{HK82T}:/home/u/t"},
                 {"name": ""}]
        self.assertEqual(transferring(stats), [("Foto/big.bin", "up"), ("doc.odt", "down")])


if __name__ == "__main__":
    unittest.main()
