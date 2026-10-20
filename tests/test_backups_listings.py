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


class PartialsTest(unittest.TestCase):
    def test_rclone_leftovers_a_day_old_go(self):
        # a download cut short leaves "<name>.<8 hex>.partial" next to the
        # file; nothing else removes it, and nothing else is removed
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = time.time() - 2 * 86400
            for folder in ("d", "node_modules", "mine"):
                (root / folder).mkdir()
            files = {"d/f05.bin.af1b584d.partial": old,      # rclone's, two days old: goes
                     "g.pdf.085c08fe.partial": time.time(),   # rclone's, a download in progress: stays
                     "notes.partial": old,                    # not rclone's form: stays
                     "x.ABCDEF12.partial": old,               # nor this
                     "f05.bin": old,
                     # in folders rclone never writes to: excluded by default, by the pair
                     "node_modules/a.js.0123abcd.partial": old,
                     "mine/b.txt.0123abcd.partial": old}
            for rel, when in files.items():
                (root / rel).write_text("x")
                os.utime(root / rel, (when, when))
            self.assertEqual(jobs.remove_stale_partials(str(root), ["mine/**"]), 1)
            left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
            self.assertEqual(left, sorted(set(files) - {"d/f05.bin.af1b584d.partial"}))
