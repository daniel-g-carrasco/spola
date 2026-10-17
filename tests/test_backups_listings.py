"""The local backups seen and used, and the listings kept after a run."""

import os
import tempfile
import time
import unittest
from pathlib import Path

import _setup  # noqa: F401
from spola import jobs
from spola.config import Pair
from spola.paths import Paths


class BackupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = Paths(self.tmp / "config", self.tmp / "state", self.tmp / "data")
        self.local = self.tmp / "local"
        (self.local / "docs").mkdir(parents=True)
        self.pair = Pair("p1", str(self.local), "/d")
        root = self.paths.backup_dir("p1")
        for run, files in (("20261013-175420", {"docs/a.txt": "old a", "b.txt": "old b"}),
                           ("20261015-090000", {"docs/a.txt": "older a"})):
            for rel, text in files.items():
                (root / run / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / run / rel).write_text(text)
        (root / "not-ours").mkdir()
        (root / "not-ours" / "x").write_text("x")

    def test_runs_and_their_files(self):
        runs = jobs.backup_runs(self.paths, "p1")
        self.assertEqual([r.name for r in runs], ["20261015-090000", "20261013-175420"])
        self.assertEqual((runs[1].files, runs[1].size), (2, 10))
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(runs[0].time)), "2026-10-15 09:00")
        self.assertEqual(jobs.backup_files(self.paths, "p1", "20261013-175420"), [("b.txt", 5), ("docs/a.txt", 5)])
        self.assertEqual(jobs.backup_runs(self.paths, "none"), [])
        with self.assertRaises(ValueError):
            jobs.backup_files(self.paths, "p1", "not-ours")
        with self.assertRaises(ValueError):
            jobs.backup_files(self.paths, "p1", "../p2")

    def test_restore_keeps_the_current_file_first(self):
        (self.local / "docs" / "a.txt").write_text("current a")
        kept = jobs.restore_backup(self.paths, self.pair, "20261013-175420", "docs/a.txt", now=1_800_000_000)
        self.assertEqual((self.local / "docs" / "a.txt").read_text(), "old a")
        self.assertEqual(kept, time.strftime("%Y%m%d-%H%M%S", time.localtime(1_800_000_000)))
        self.assertEqual((self.paths.backup_dir("p1") / kept / "docs" / "a.txt").read_text(), "current a")
        # the backup itself stays; a file with nothing in its place needs no run of its own
        self.assertTrue((self.paths.backup_dir("p1") / "20261013-175420" / "docs" / "a.txt").exists())
        self.assertEqual(jobs.restore_backup(self.paths, self.pair, "20261013-175420", "b.txt"), "")
        self.assertEqual((self.local / "b.txt").read_text(), "old b")
        for bad in ("../b.txt", "/b.txt", "", "docs/../../b.txt"):
            with self.assertRaises(ValueError):
                jobs.restore_backup(self.paths, self.pair, "20261013-175420", bad)
        with self.assertRaises(FileNotFoundError):
            jobs.restore_backup(self.paths, self.pair, "20261013-175420", "missing.txt")

    def test_delete_a_run(self):
        jobs.delete_backup_run(self.paths, "p1", "20261015-090000")
        self.assertEqual([r.name for r in jobs.backup_runs(self.paths, "p1")], ["20261013-175420"])
        with self.assertRaises(ValueError):
            jobs.delete_backup_run(self.paths, "p1", "not-ours")
        self.assertTrue((self.paths.backup_dir("p1") / "not-ours").exists())


class KeptListingsTest(unittest.TestCase):
    def test_kept_after_a_run_and_put_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "config", Path(tmp) / "state", Path(tmp) / "data")
            wd = paths.workdir("p1")
            wd.mkdir(parents=True)
            self.assertIsNone(jobs.keep_listings(paths, "p1"))             # nothing to keep yet
            (wd / "a..b.path1.lst").write_text("# listing\nhere 1\n")
            (wd / "a..b.path2.lst").write_text("# listing\nthere 1\n")
            filters = paths.filters_file("p1")
            filters.parent.mkdir(parents=True)
            filters.write_text("- x\n")
            filters.with_name(filters.name + ".md5").write_text("abc\n")
            first = jobs.keep_listings(paths, "p1", now=1_800_000_000)
            self.assertEqual(sorted(f.name for f in first.iterdir()), ["a..b.path1.lst", "a..b.path2.lst", "p1.txt.md5"])
            for i in range(1, 5):
                (wd / "a..b.path1.lst").write_text(f"# listing\nhere {i + 1}\n")
                jobs.keep_listings(paths, "p1", now=1_800_000_000 + i * 60)
            kept = jobs.kept_listings(paths, "p1")
            self.assertEqual(len(kept), jobs.KEPT_LISTINGS)                 # the oldest two gone
            self.assertEqual(kept[0][1], 1_800_000_000 + 4 * 60)            # newest first
            # the current listings damaged: the last kept copy put back, the damage set aside
            (wd / "a..b.path1.lst").write_text("garbage")
            (wd / "a..b.path2.lst-err").write_text("x")
            (wd / "a..b.path1.lst-old").write_text("x")
            filters.with_name(filters.name + ".md5").write_text("changed\n")
            jobs.restore_listings(paths, "p1", kept[1][0])
            self.assertEqual((wd / "a..b.path1.lst").read_text(), "# listing\nhere 4\n")
            self.assertEqual((wd / "a..b.path2.lst").read_text(), "# listing\nthere 1\n")
            self.assertFalse((wd / "a..b.path2.lst-err").exists())
            self.assertFalse((wd / "a..b.path1.lst-old").exists())
            self.assertEqual(filters.with_name(filters.name + ".md5").read_text(), "abc\n")
            aside = [d for d in (wd / "kept").iterdir() if d.name.startswith("replaced-")]
            self.assertEqual(len(aside), 1)
            self.assertEqual((aside[0] / "a..b.path1.lst").read_text(), "garbage")
            self.assertEqual(sorted(os.listdir(aside[0])), ["a..b.path1.lst", "a..b.path1.lst-old", "a..b.path2.lst",
                                                            "a..b.path2.lst-err"])
            for bad in ("../x", "replaced-20261017-000000", "nope"):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    jobs.restore_listings(paths, "p1", bad)
