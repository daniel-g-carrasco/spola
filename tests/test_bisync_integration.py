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
from spola.rclone import Mode, Result, bisync_args, clean_env
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

    def start(self, mode=Mode.SYNC, dry_run=False, force=False, extra=()):
        job = jobs.prepare(self.cfg, self.pair, self.paths, mode, dry_run, force,
                           path2_override=str(self.remote))
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
        out = self.bisync(Mode.RESYNC)
        self.assertIs(out.result, Result.OK, out.message)
        self.pair.initialized = True

    def write(self, side: Path, rel: str, text: str, age: float = 0):
        path = side / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))

    def files(self, side: Path):
        return sorted(str(p.relative_to(side)) for p in side.rglob("*") if p.is_file())

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
        self.assertEqual([f for f in self.files(self.local) if not f.startswith("keep")],
                         ["doc.conflict1.odt", "doc.conflict2.odt", "notes.txt"])
        self.assertEqual(self.files(self.local), self.files(self.remote))
        self.assertEqual((self.remote / "notes.txt").read_text(), "only here changed")

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
