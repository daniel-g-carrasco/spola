#!/usr/bin/env python3
"""Conflict copies paired up by original name, and resolved with local file
operations only (the discarded copy goes to a wastebasket, never deleted)."""

import os
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.app.conflicts import ResolveError, decide, difference, group, resolve
from spola.app.config import Pair
from spola.app.preflight import claim_root


class ConflictsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trashed = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return rel

    def trash(self, path):
        self.trashed.append(Path(os.path.relpath(path, self.root)).as_posix())
        os.unlink(path)

    def listing(self):
        # with "/", as rclone's listings name the files on every system
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def test_grouping(self):
        paths = [
            self.write("a/doc.conflict-local-2026-10-10-1.odt", "L"),
            self.write("a/doc.conflict-koofr-2026-10-10-1.odt", "K"),
            self.write("a/doc.conflitto-locale-2026-10-11-1.odt", "L2"),      # Italian, another day
            self.write("a/doc.conflitto-koofr-2026-10-11-1.odt", "K2"),
            self.write("old.conflict1.txt", "L"), self.write("old.conflict2.txt", "K"),  # first versions
            self.write("lonely.conflict-koofr-2026-10-10-1", "K"),            # no extension, one copy left
            "a/missing.conflict-local-2026-10-10-1.odt",                       # in the listing, not on disk
        ]
        items = group(paths, str(self.root))
        self.assertEqual([(c.original, c.date) for c in items],
                         [("a/doc.odt", "2026-10-10"), ("a/doc.odt", "2026-10-11"), ("lonely", "2026-10-10"), ("old.txt", "")])
        first = items[0]
        self.assertEqual(first.local.path, "a/doc.conflict-local-2026-10-10-1.odt")
        self.assertEqual(first.koofr.path, "a/doc.conflict-koofr-2026-10-10-1.odt")
        self.assertEqual(first.local.size, 1)
        self.assertEqual(items[1].local.path, "a/doc.conflitto-locale-2026-10-11-1.odt")
        self.assertIsNone(items[2].local)
        self.assertEqual(items[3].local.path, "old.conflict1.txt")

    def conflict(self):
        self.write("a/doc.conflict-local-2026-10-10-1.odt", "local")
        self.write("a/doc.conflict-koofr-2026-10-10-1.odt", "koofr")
        return group(self.listing(), str(self.root))[0]

    def test_keep_local(self):
        renames = resolve(str(self.root), self.conflict(), "local", trash=self.trash)
        self.assertEqual(renames, [("a/doc.conflict-local-2026-10-10-1.odt", "a/doc.odt")])
        self.assertEqual(self.listing(), ["a/doc.odt"])
        self.assertEqual((self.root / "a/doc.odt").read_text(), "local")
        self.assertEqual(self.trashed, ["a/doc.conflict-koofr-2026-10-10-1.odt"])

    def test_keep_koofr(self):
        resolve(str(self.root), self.conflict(), "koofr", trash=self.trash)
        self.assertEqual((self.root / "a/doc.odt").read_text(), "koofr")
        self.assertEqual(self.trashed, ["a/doc.conflict-local-2026-10-10-1.odt"])

    def test_keep_both(self):
        resolve(str(self.root), self.conflict(), "both", trash=self.trash)
        self.assertEqual(self.listing(), ["a/doc (Koofr 2026-10-10).odt", "a/doc (this computer 2026-10-10).odt"])
        self.assertEqual(self.trashed, [])

    def test_never_overwrites(self):
        conflict = self.conflict()
        self.write("a/doc.odt", "made since")
        with self.assertRaises(ResolveError):
            resolve(str(self.root), conflict, "local", trash=self.trash)
        self.assertEqual((self.root / "a/doc.odt").read_text(), "made since")
        self.assertEqual(len(self.listing()), 3)          # nothing moved, nothing trashed
        self.assertEqual(self.trashed, [])
        # "both" steps aside instead
        self.write("a/doc (Koofr 2026-10-10).odt", "taken")
        resolve(str(self.root), conflict, "both", trash=self.trash)
        self.assertIn("a/doc (Koofr 2026-10-10) (2).odt", self.listing())

    def test_rules(self):
        conflict = self.conflict()
        local, koofr = self.root / conflict.local.path, self.root / conflict.koofr.path
        os.utime(local, (0, 1_000_000))
        os.utime(koofr, (0, 1_000_000 - 7200))
        conflict = group(self.listing(), str(self.root))[0]
        newer = decide(conflict, "newer")
        self.assertEqual(newer.choice, "local")
        self.assertIn("2 hours", newer.reason)
        os.utime(koofr, (0, 1_000_000 + 300))
        conflict = group(self.listing(), str(self.root))[0]
        self.assertEqual(decide(conflict, "newer").choice, "koofr")
        self.assertEqual(decide(conflict, "local").choice, "local")
        self.assertEqual(decide(conflict, "koofr").choice, "koofr")
        self.assertEqual(decide(conflict, "both").choice, "both")
        self.assertIsNone(decide(conflict, "ask").choice)
        # what a rule cannot decide is left to the user, with the reason
        os.utime(koofr, (0, 1_000_000 + 1))
        conflict = group(self.listing(), str(self.root))[0]
        self.assertIsNone(decide(conflict, "newer").choice)
        koofr.unlink()
        conflict = group(self.listing(), str(self.root))[0]
        self.assertIsNone(decide(conflict, "newer").choice)
        self.assertIsNone(decide(conflict, "koofr").choice)
        self.assertEqual(decide(conflict, "local").choice, "local")

    def test_difference_in_words(self):
        self.assertEqual(difference(5), "5 seconds")
        self.assertEqual(difference(-300), "5 minutes")
        self.assertEqual(difference(3 * 3600), "3 hours")
        self.assertEqual(difference(86400 * 2), "2 days")
        self.assertEqual(difference(60), "60 seconds")

    def test_missing_copy(self):
        conflict = self.conflict()
        (self.root / conflict.koofr.path).unlink()
        with self.assertRaises(ResolveError):
            resolve(str(self.root), conflict, "koofr", trash=self.trash)
        resolve(str(self.root), conflict, "local", trash=self.trash)
        self.assertEqual(self.listing(), ["a/doc.odt"])


class ResolveChecksTheFolderTest(unittest.TestCase):
    """Given the pair (as the app does), a resolution changes nothing in a
    folder that is no longer the paired one."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "local"
        self.root.mkdir()
        st = os.stat(self.root)
        self.pair = Pair("p1", str(self.root), "/d", root_dev=st.st_dev, root_ino=st.st_ino)
        claim_root(self.pair)
        self.trashed = []

    def fill(self, root):
        for rel, text in (("a.conflict-local-2026-10-10-1.odt", "mine"),
                          ("a.conflict-koofr-2026-10-10-1.odt", "theirs")):
            (root / rel).write_text(text)
        return group(["a.conflict-local-2026-10-10-1.odt", "a.conflict-koofr-2026-10-10-1.odt"], str(root))[0]

    def test_the_paired_folder_is_resolved_as_before(self):
        conflict = self.fill(self.root)
        resolve(str(self.root), conflict, "local", trash=self.trashed.append, pair=self.pair)
        self.assertEqual((self.root / "a.odt").read_text(), "mine")

    def test_a_replaced_folder_is_left_as_it_is(self):
        conflict = self.fill(self.root)
        os.rename(self.root, self.tmp / "moved")
        self.root.mkdir()
        self.fill(self.root)
        before = sorted(p.name for p in self.root.iterdir())
        for choice in ("local", "koofr", "both"):
            with self.assertRaises(ResolveError):
                resolve(str(self.root), conflict, choice, trash=self.trashed.append, pair=self.pair)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)
        self.assertEqual(self.trashed, [])


if __name__ == "__main__":
    unittest.main()
