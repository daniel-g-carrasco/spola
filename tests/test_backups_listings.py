"""The listings kept after a run (the kept copies of files: test_versions.py)."""

import os
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401
from spola import jobs
from spola.paths import Paths


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
