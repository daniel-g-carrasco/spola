#!/usr/bin/env python3
"""End-to-end scenarios with the real rclone, between two local folders
(the "Koofr" side is a local folder here): what Spola does to files when
things go right and when they go wrong. Skipped if rclone is not installed.

    python tests/test_bisync_integration.py
"""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import _setup
from _setup import rclone_binary

from spola import jobs
from spola.config import Config, Pair
from spola.paths import Paths
from spola.preflight import root_identity
from spola.rclone import Kind, Mode, Result, bisync_args, clean_env
from spola.runner import BisyncRun, PairBusy

RCLONE = rclone_binary()


@unittest.skipUnless(RCLONE, "rclone not installed")
class BisyncScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="spola-test-"))
        self.local = self.tmp / "local"
        self.remote = self.tmp / "remote"
        self.local.mkdir()
        self.remote.mkdir()
        self.paths = Paths(self.tmp / "config", self.tmp / "state", self.tmp / "data")
        dev, ino = root_identity(str(self.local))
        self.pair = Pair("p1", str(self.local), "/test", root_dev=dev, root_ino=ino)
        self.cfg = Config(user="test", pairs=[self.pair])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def start(self, mode=Mode.SYNC, dry_run=False, force=False, extra=(), wait_for_writes=False):
        job = jobs.prepare(self.cfg, self.pair, self.paths, mode, dry_run, force,
                           path2_override=str(self.remote), wait_for_writes=wait_for_writes)
        job.request.extra_test_flags = list(extra)
        run = BisyncRun(bisync_args(RCLONE, job.request), clean_env(),
                        self.paths.lock_file(self.pair.id), self.paths.log_dir(self.pair.id),
                        job.path1, job.path2)
        run.start()
        return run

    def bisync(self, *a, **kw):
        outcome = self.start(*a, **kw).wait(120)
        self.assertIsNotNone(outcome, "rclone did not finish")
        return outcome

    def align(self):
        """First alignment as the app does it: resync, then a sealing run."""
        out = self.bisync(Mode.RESYNC)
        self.assertIs(out.result, Result.OK, out.message)
        self.pair.initialized = True
        self.assertIs(self.bisync().result, Result.OK)
        workdir = self.paths.workdir(self.pair.id)
        self.assertEqual(len(list(workdir.glob("*.lst-old"))), 2, "no backup listing for --recover")

    def write(self, side: Path, rel: str, text: str, age: float = 0):
        path = side / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))

    def files(self, side: Path):
        # the root marker Spola writes is excluded from the sync, like any .spola-* file
        return sorted(str(p.relative_to(side)) for p in side.rglob("*") if p.is_file() and p.name != ".spola-root")

    def backups(self):
        root = self.paths.backup_dir(self.pair.id)
        return sorted(str(p.relative_to(root)).split("/", 1)[1] for p in root.rglob("*") if p.is_file()) if root.exists() else []

    # -- scenarios ---------------------------------------------------------------

    def test_no_normal_run_before_first_alignment(self):
        with self.assertRaises(jobs.NotReady) as ctx:
            self.start(Mode.SYNC)
        self.assertEqual(ctx.exception.problems[0].code, "not-initialized")

    def test_first_alignment_preview_then_real(self):
        self.write(self.local, "only-local.txt", "L")
        self.write(self.local, "differ.txt", "old local", age=3600)
        self.write(self.remote, "differ.txt", "new remote")
        self.write(self.remote, "sub/only-remote.txt", "R")
        preview = self.bisync(Mode.RESYNC, dry_run=True)
        self.assertIs(preview.result, Result.OK)
        self.assertEqual(preview.plan.upload, ["only-local.txt"])
        self.assertEqual(sorted(preview.plan.download), ["differ.txt", "sub/only-remote.txt"])
        self.assertEqual(preview.plan.replace_local, ["differ.txt"])
        self.assertEqual(self.files(self.local), ["differ.txt", "only-local.txt"])   # dry run: untouched
        self.align()
        self.assertEqual(self.files(self.local), self.files(self.remote))
        self.assertEqual((self.local / "differ.txt").read_text(), "new remote")
        self.assertEqual(self.backups(), ["differ.txt"])       # the old local version is kept

    def test_changes_and_conflicts_keep_every_version(self):
        self.write(self.local, "doc.odt", "base", age=600)
        self.write(self.local, "notes.txt", "base", age=600)
        for i in range(5):    # if every file changed, bisync stops on its own ("all files changed")
            self.write(self.local, f"keep{i}.txt", "same", age=600)
        self.align()
        self.write(self.local, "doc.odt", "edited here")
        self.write(self.remote, "doc.odt", "edited on Koofr!")
        self.write(self.local, "notes.txt", "only here changed")
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual(out.plan.conflicts, ["doc.odt"])
        today = time.strftime("%Y-%m-%d")
        self.assertEqual([f for f in self.files(self.local) if not f.startswith("keep")],
                         [f"doc.conflict-koofr-{today}-1.odt", f"doc.conflict-local-{today}-1.odt", "notes.txt"])
        self.assertEqual((self.local / f"doc.conflict-local-{today}-1.odt").read_text(), "edited here")
        # a second conflict on the same file, the same day, keeps the first copies
        self.write(self.local, "doc.odt", "second edit here")
        self.write(self.remote, "doc.odt", "second edit on Koofr!")
        self.assertIs(self.bisync().result, Result.OK)
        conflicts = [f for f in self.files(self.local) if f.startswith("doc.conflict")]
        self.assertEqual(len(conflicts), 4, conflicts)
        self.assertEqual(self.files(self.local), self.files(self.remote))
        self.assertEqual((self.remote / "notes.txt").read_text(), "only here changed")

    def test_resolving_a_conflict_reaches_the_other_side(self):
        from spola import conflicts
        self.write(self.local, "doc.odt", "base", age=600)
        for i in range(5):
            self.write(self.local, f"keep{i}.txt", "same", age=600)
        self.align()
        self.write(self.local, "doc.odt", "mine")
        self.write(self.remote, "doc.odt", "theirs")
        self.assertIs(self.bisync().result, Result.OK)
        items = conflicts.group([f for f in self.files(self.local) if "conflict" in f], str(self.local))
        self.assertEqual(len(items), 1)
        trashed = []
        conflicts.resolve(str(self.local), items[0], "local", trash=lambda p: trashed.append(p) or os.unlink(p))
        self.assertEqual(len(trashed), 1)
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual((self.remote / "doc.odt").read_text(), "mine")
        self.assertEqual([f for f in self.files(self.remote) if "conflict" in f], [])

    def test_remote_delete_goes_to_local_backup(self):
        for i in range(30):
            self.write(self.local, f"f{i:02}.txt", str(i), age=600)
        self.align()
        (self.remote / "f07.txt").unlink()
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertFalse((self.local / "f07.txt").exists())
        self.assertEqual(self.backups(), ["f07.txt"])

    def test_mass_delete_stops_and_needs_confirmation(self):
        for i in range(40):
            self.write(self.local, f"f{i:02}.txt", str(i), age=600)
        self.align()
        for i in range(10):                      # 25% of the files
            (self.local / f"f{i:02}.txt").unlink()
        out = self.bisync()
        self.assertIs(out.result, Result.NEEDS_CONFIRMATION)
        self.assertEqual(len(self.files(self.remote)), 40)       # nothing deleted on Koofr
        preview = self.bisync(dry_run=True, force=True)
        self.assertEqual(len(preview.plan.delete_remote), 10)
        self.assertEqual(len(self.files(self.remote)), 40)
        out = self.bisync(force=True)                               # the user confirmed
        self.assertIs(out.result, Result.OK)
        self.assertEqual(len(self.files(self.remote)), 30)

    def test_renames_are_moves_on_the_other_side(self):
        for i in range(40):
            self.write(self.local, f"PXL_{i:02}.jpg", f"photo {i}", age=600)
        self.align()
        moved_inode = (self.remote / "PXL_00.jpg").stat().st_ino
        (self.local / "2026").mkdir()
        for i in range(10):                      # 25% renamed: a move counts as a delete, so it asks first
            (self.local / f"PXL_{i:02}.jpg").rename(self.local / "2026" / f"2026-05-{i + 1:02}.jpg")
        out = self.bisync()
        self.assertIs(out.result, Result.NEEDS_CONFIRMATION)
        self.assertEqual(len(self.files(self.remote)), 40)
        # what the user confirms: ten moves and a new folder, nothing deleted
        preview = self.bisync(dry_run=True, force=True)
        self.assertEqual(len(preview.plan.moved_remote), 10)
        self.assertIn(("PXL_00.jpg", "2026/2026-05-01.jpg"), preview.plan.moved_remote)
        self.assertEqual((preview.plan.upload, preview.plan.delete_remote), (["2026"], []))
        out = self.bisync(force=True)
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual(sorted(out.plan.moved_remote), sorted(preview.plan.moved_remote))
        self.assertEqual(self.files(self.remote), self.files(self.local))
        # the same file, moved: not deleted and copied again
        self.assertEqual((self.remote / "2026" / "2026-05-01.jpg").stat().st_ino, moved_inode)
        self.assertEqual((self.remote / "2026" / "2026-05-01.jpg").read_text(), "photo 0")
        self.assertEqual(self.backups(), [])    # nothing replaced, nothing lost
        self.assertTrue(self.bisync().plan.empty)

    def test_renames_on_koofr_are_moves_here(self):
        for i in range(20):
            self.write(self.local, f"PXL_{i:02}.jpg", f"photo {i}", age=600)
        self.align()
        moved_inode = (self.local / "PXL_03.jpg").stat().st_ino
        (self.remote / "PXL_03.jpg").rename(self.remote / "from-the-other-laptop.jpg")
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual(out.plan.moved_local, [("PXL_03.jpg", "from-the-other-laptop.jpg")])
        self.assertEqual(out.plan.download + out.plan.delete_local, [])
        self.assertEqual((self.local / "from-the-other-laptop.jpg").stat().st_ino, moved_inode)
        self.assertEqual(self.backups(), [])    # moved, so nothing to keep in the backup
        self.assertTrue(self.bisync().plan.empty)

    def test_replaced_root_never_reaches_rclone(self):
        self.write(self.local, "a.txt", "x")
        self.align()
        shutil.rmtree(self.local)               # e.g. the drive is unmounted...
        self.local.mkdir()                      # ...and an empty folder is in its place
        with self.assertRaises(jobs.NotReady) as ctx:
            self.start()
        self.assertEqual(ctx.exception.problems[0].code, "root-replaced")
        self.assertEqual(self.files(self.remote), ["a.txt"])

    def test_empty_side_is_a_repair_not_a_wipe(self):
        for i in range(3):
            self.write(self.local, f"f{i}.txt", str(i), age=600)
        self.align()
        for p in self.remote.iterdir():
            p.unlink()
        out = self.bisync()
        self.assertIn(out.result, (Result.NEEDS_REPAIR, Result.NEEDS_CONFIRMATION))
        self.assertEqual(self.files(self.local), ["f0.txt", "f1.txt", "f2.txt"])

    def test_default_exclusions(self):
        self.write(self.local, "kept.txt", "x")
        self.write(self.local, "app/node_modules/pkg/index.js", "x")
        self.write(self.local, ".cache/koofr-staging", "x")
        self.write(self.local, "sub/.cache/kept-too", "x")
        self.write(self.local, "~$offerta.docx", "x")
        self.write(self.local, "Thumbs.db", "x")
        self.align()
        self.assertEqual(self.files(self.remote), ["kept.txt", "sub/.cache/kept-too"])

    def test_names_koofr_refuses_are_left_out_not_blocking(self):
        for i in range(5):
            self.write(self.local, f"f{i}.txt", str(i), age=600)
        self.align()
        # glob characters too, where the file system allows them ("*" is not a name on Windows)
        odd = "foto \U0001F600 [1].jpg" if _setup.WINDOWS else "foto \U0001F600 [1]*.jpg"
        self.write(self.local, odd, "emoji and glob characters")
        self.write(self.local, "Vacanze \U0001F334/a.jpg", "in a folder with an emoji")
        self.write(self.local, "normale.txt", "this one must still go")
        (self.local / "f0.txt").unlink()                  # and deletions still apply
        job = jobs.prepare(self.cfg, self.pair, self.paths, Mode.SYNC, path2_override=str(self.remote))
        self.assertEqual(sorted(r.path for r in job.refused), ["Vacanze \U0001F334", odd])
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual(self.files(self.remote), ["f1.txt", "f2.txt", "f3.txt", "f4.txt", "normale.txt"])
        self.assertTrue((self.local / odd).exists())     # never touched
        # renamed: synced at the next run
        (self.local / odd).rename(self.local / "foto.jpg")
        out = self.bisync()
        self.assertIs(out.result, Result.OK, out.message)
        self.assertIn("foto.jpg", self.files(self.remote))

    def test_files_being_written_wait_or_are_left_out(self):
        # a transcription written while it was synced: Koofr got half of it,
        # the next run took it for changed on both sides (a conflict), and
        # renaming it, held open, stopped bisync for a repair
        for i in range(30):
            self.write(self.local, f"f{i:02}.txt", str(i), age=600)
        self.align()
        self.write(self.local, "f01.txt", "being written")         # already in sync
        self.write(self.local, "notes.srt", "half a transcription")   # new
        with self.assertRaises(jobs.Busy) as ctx:                  # an automatic run waits
            jobs.prepare(self.cfg, self.pair, self.paths, Mode.SYNC, path2_override=str(self.remote),
                         wait_for_writes=True)
        self.assertEqual(ctx.exception.paths, ["f01.txt"])
        settled = time.time() - 60
        os.utime(self.local / "f01.txt", (settled, settled))
        out = self.bisync(wait_for_writes=True)                    # the new one still written: left out
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual((self.remote / "f01.txt").read_text(), "being written")
        self.assertFalse((self.remote / "notes.srt").exists())
        self.write(self.local, "notes.srt", "the whole transcription", age=60)
        out = self.bisync(wait_for_writes=True)
        self.assertIs(out.result, Result.OK, out.message)
        self.assertEqual((self.remote / "notes.srt").read_text(), "the whole transcription")
        self.assertEqual(out.plan.conflicts, [])
        self.assertEqual(self.files(self.local), self.files(self.remote))
        # what the user asks for never waits
        self.write(self.local, "now.txt", "saved, then Sync Now")
        self.assertIs(self.bisync().result, Result.OK)
        self.assertTrue((self.remote / "now.txt").exists())

    def test_same_pair_cannot_run_twice(self):
        self.write(self.local, "big.bin", "x" * 2_000_000)
        first = self.start(Mode.RESYNC, extra=["--bwlimit", "400k"])
        try:
            with self.assertRaises(PairBusy):
                self.start(Mode.RESYNC)
        finally:
            first.cancel()
            first.cancel()
            first.wait(60)

    def test_a_long_run_keeps_one_stats_line_at_a_time(self):
        self.write(self.local, "big.bin", "x" * 1_500_000)
        run = self.start(Mode.RESYNC, extra=["--bwlimit", "300k"])
        out = run.wait(60)
        self.assertIs(out.result, Result.OK)
        stats = [e for e in run.events if e.kind is Kind.STATS]
        self.assertGreater(len(run.events), 0)
        self.assertLessEqual(len(stats), 3)               # one a second, folded into the latest
        self.assertIsNotNone(out.stats)

    def test_interrupted_run_recovers(self):
        self.write(self.local, "small.txt", "s", age=600)
        self.align()
        self.write(self.local, "big.bin", "x" * 3_000_000)
        run = self.start(extra=["--bwlimit", "500k"])
        time.sleep(2)
        run.cancel()                             # graceful: bisync saves its state
        out = run.wait(90)
        self.assertIsNotNone(out)
        self.assertIn(out.result, (Result.OK, Result.CANCELLED, Result.RETRY))
        again = self.bisync()
        self.assertIs(again.result, Result.OK, again.message)
        self.assertEqual(self.files(self.local), self.files(self.remote))


if __name__ == "__main__":
    unittest.main()
