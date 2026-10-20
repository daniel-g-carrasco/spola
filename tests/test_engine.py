#!/usr/bin/env python3
"""The scheduler end to end, with the real rclone and a local folder as the
"Koofr" side (SPOLA_DEV_REMOTE): first alignment, a local change picked up by
the monitor, a mass delete that stops and waits, pause, shutdown."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _setup
from _setup import rclone_binary

from gi.repository import GLib

from spola import engine as engine_mod
from spola import trash as trash_mod
from spola import koofr_api
from spola.account import Credentials, Quota
from spola.config import Config, Pair
from spola.conflicts import decide
from spola.engine import Engine, PairRuntime, Status, conflict_name_pattern, slept
from spola.paths import Paths
from spola import preflight
from spola.preflight import rename_plan, root_identity
from spola import jobs, listings
from spola import versions as versions_mod
from spola.rclone import Mode, Result

RCLONE = rclone_binary()


def text_of(path) -> str | None:
    """The file's text, None while it is not there."""
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


def spin_until(cond, timeout=60.0) -> bool:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        ctx.iteration(False)
        time.sleep(0.01)
    return cond()


@unittest.skipUnless(RCLONE, "rclone not installed")
class EngineTest(unittest.TestCase):
    def setUp(self):
        engine_mod.DEBOUNCE_S = 1
        # the files the tests write are not being written by anyone
        self.addCleanup(setattr, engine_mod.jobs, "SETTLE_S", engine_mod.jobs.SETTLE_S)
        engine_mod.jobs.SETTLE_S = 0
        engine_mod.PREVIEW_AHEAD_DELAY_S = 3600      # only the test about it prepares previews ahead
        self.tmp = Path(tempfile.mkdtemp(prefix="spola-engine-"))
        self.local = self.tmp / "local"
        self.remote_root = self.tmp / "remote"
        self.remote = self.remote_root / "test"
        self.local.mkdir()
        self.remote.mkdir(parents=True)
        for i in range(30):
            (self.local / f"f{i:02}.txt").write_text(str(i))
        os.environ["SPOLA_DEV_REMOTE"] = str(self.remote_root)
        os.environ["SPOLA_RCLONE"] = RCLONE
        paths = Paths(self.tmp / "config", self.tmp / "state", self.tmp / "data")
        dev, ino = root_identity(str(self.local))
        self.pair = Pair("p1", str(self.local), "/test", root_dev=dev, root_ino=ino)
        self.engine = Engine(paths, Config(user="dev", pairs=[self.pair]))
        self.engine.cfg.pause_on_metered = False
        self.engine.cfg.pause_on_power_saver = False
        self.engine.set_credentials(Credentials("dev", ""))
        self.engine.start()

    def tearDown(self):
        done = []
        self.engine.shutdown(lambda: done.append(True))
        spin_until(lambda: done, 100)
        os.environ.pop("SPOLA_DEV_REMOTE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def status(self):
        return self.engine.runtime["p1"].status

    def remote_files(self):
        return sorted(p.name for p in self.remote.iterdir() if p.is_file())

    def align(self):
        previews = []
        self.engine.preview("p1", Mode.RESYNC, False, lambda out, err: previews.append((out, err)))
        self.assertTrue(spin_until(lambda: previews))
        outcome, error = previews[0]
        self.assertEqual(error, "")
        self.assertEqual(len(outcome.plan.upload), 30)
        self.assertEqual(self.remote_files(), [])            # a preview changes nothing
        self.engine.confirm("p1", Mode.RESYNC, False)
        self.assertTrue(spin_until(lambda: self.pair.initialized and self.status() is Status.OK))
        self.assertEqual(len(self.remote_files()), 30)

    def test_an_automatic_run_waits_for_a_file_being_written(self):
        self.align()
        engine_mod.jobs.SETTLE_S = 3
        rt = self.engine.runtime["p1"]
        (self.local / "f01.txt").write_text("being written")
        self.engine.request_sync("p1", "poll")
        self.assertTrue(spin_until(lambda: rt.status is Status.PENDING and "f01.txt" in rt.message, 20),
                        (rt.status, rt.message))
        self.assertEqual((self.remote / "f01.txt").read_text(), "1")         # not yet
        # settled: the run goes on its own
        self.assertTrue(spin_until(lambda: (self.remote / "f01.txt").read_text() == "being written"
                                   and rt.status is Status.OK, 60), (rt.status, rt.message))
        self.assertEqual(rt.busy_since, 0.0)

    def test_every_file_copied_is_told_as_it_happens(self):
        # what File Explorer marks in sync during the run, not at its end
        copied = []
        self.engine.connect("path-copied", lambda _e, pid, rel: copied.append((pid, rel)))
        self.align()
        self.assertEqual(sorted(rel for _pid, rel in copied), [f"f{i:02}.txt" for i in range(30)])
        self.assertEqual({pid for pid, _rel in copied}, {"p1"})

    def test_the_backups_of_removed_pairs_expire_too(self):
        paths = self.engine.paths
        old = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 40 * 86400))
        new = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 86400))
        for pair_id, runs in (("gone1", (old, new)), ("gone2", (old,))):
            for run in runs:
                (paths.backup_dir(pair_id) / run).mkdir(parents=True)
                (paths.backup_dir(pair_id) / run / "f.txt").write_text("x")
        self.engine._prune_backups()
        self.assertEqual(sorted(p.name for p in paths.backup_dir("gone1").iterdir()), [new])
        self.assertFalse(paths.backup_dir("gone2").exists())        # nothing left: its folder too

    def test_a_pair_removed_leaves_its_backups_only(self):
        self.align()
        paths = self.engine.paths
        state = [paths.workdir("p1"), paths.filters_file("p1"), paths.lock_file("p1"), paths.log_dir("p1")]
        self.assertTrue(all(p.exists() for p in state), [p for p in state if not p.exists()])
        paths.backup_dir("p1").mkdir(parents=True)
        (paths.backup_dir("p1") / "kept.txt").write_text("x")
        self.engine.remove_pair("p1")
        # at once, or once a run still going is over
        self.assertTrue(spin_until(lambda: not any(p.exists() for p in state), 60),
                        [p for p in state if p.exists()])
        self.assertTrue((paths.backup_dir("p1") / "kept.txt").exists())
        self.assertEqual(len(list(self.local.glob("f*.txt"))), 30)         # files stay
        self.assertEqual(len(self.remote_files()), 30)

    def test_sign_in_and_out_change_the_overall_status_without_pairs(self):
        # with no pair, nothing else would tell the tray that the account changed
        self.engine.remove_pair("p1")
        seen = []
        self.engine.connect("status-changed", lambda _e: seen.append(self.engine.creds))
        self.engine.set_credentials(None)
        self.engine.set_credentials(Credentials("dev", ""))
        self.engine.set_paused_all(True)
        self.assertEqual(len(seen), 3)
        self.assertIsNone(seen[0])
        self.engine.set_paused_all(False)

    def test_activity_feed_rings_the_pair_and_falls_back_when_it_fails(self):
        self.align()
        calls = []
        feed = {"items": [], "fail": False}

        def fake_activity(user, password, mount, since):
            calls.append(since)
            if feed["fail"]:
                raise koofr_api.ApiError("HTTP 503")
            return feed["items"]

        orig = koofr_api.activity, koofr_api.primary_mount_id
        koofr_api.activity, koofr_api.primary_mount_id = fake_activity, lambda u, p: "m-1"
        try:
            self.engine.feed_enabled = True
            self.engine.creds = Credentials("dev", "", "app-password")
            # someone uploads to Koofr: the feed reports it, the pair runs
            (self.remote / "from-phone.txt").write_text("hi")
            feed["items"] = [koofr_api.Activity("1", 2_000_000_000_000, "file_uploaded", "m-1",
                                                "/test/from-phone.txt", "", "Dalvik/2.1.0")]
            self.engine._poll_activity()
            self.assertTrue(spin_until(lambda: (self.local / "from-phone.txt").exists(), 30))
            self.assertTrue(self.engine.feed_ok)
            self.assertEqual(self.engine.cfg.mount_id, "m-1")
            # the same item again is not a new change
            self.assertTrue(spin_until(lambda: self.status() is Status.OK, 30))
            self.engine._poll_activity()
            spin_until(lambda: False, 1)
            self.assertEqual(calls[-1], 2_000_000_000_000)
            self.assertIs(self.status(), Status.OK)
            # the feed breaks: back to the short periodic runs
            feed["fail"] = True
            self.engine._poll_activity()
            self.assertTrue(spin_until(lambda: self.engine.feed_ok is False, 10))
        finally:
            koofr_api.activity, koofr_api.primary_mount_id = orig

    def test_changed_exclusions_need_a_repair_shown_as_a_preview(self):
        from spola.jobs import read_filters
        self.align()
        (self.local / "secret.tmp").write_text("x")
        (self.local / "shown.txt").write_text("y")
        text = read_filters(self.engine.paths, self.pair) + "- *.tmp\n"
        self.assertFalse(self.engine.set_filters("p1", read_filters(self.engine.paths, self.pair)))   # unchanged
        self.assertTrue(self.engine.set_filters("p1", text))
        self.assertIs(self.status(), Status.NEEDS_REPAIR)
        self.engine.sync_all("poll")                       # automatic runs stay stopped
        spin_until(lambda: False, 1.5)
        self.assertNotIn("shown.txt", self.remote_files())
        results = []
        self.engine.preview("p1", Mode.RESYNC, False, lambda out, err: results.append((out, err)))
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].task is not None, 10))
        # a local change while the preview runs must not queue a normal run behind it
        self.engine.request_sync("p1", "local")
        self.assertFalse([t for t in self.engine.queue if t.pair_id == "p1"])
        self.assertTrue(spin_until(lambda: results, 30))
        self.assertEqual(results[0][0].plan.upload, ["shown.txt"])
        spin_until(lambda: False, 0.5)
        self.assertIs(self.status(), Status.NEEDS_REPAIR)      # a preview decides nothing
        self.engine.confirm("p1", Mode.RESYNC, False)
        self.assertTrue(spin_until(lambda: self.status() is Status.OK, 30))
        self.assertIn("shown.txt", self.remote_files())
        self.assertNotIn("secret.tmp", self.remote_files())

    def slow_prepare(self, seconds):
        """jobs.prepare taking its time, as on a folder with many files."""
        real = engine_mod.jobs.prepare

        def slow(*args, **kwargs):
            time.sleep(seconds)
            return real(*args, **kwargs)
        engine_mod.jobs.prepare = slow
        self.addCleanup(setattr, engine_mod.jobs, "prepare", real)

    def test_preparing_a_run_leaves_the_main_loop_free(self):
        self.align()
        self.slow_prepare(1.5)
        ticks = []
        GLib.timeout_add(100, lambda: ticks.append(1) or len(ticks) < 50)
        results = []
        self.engine.preview("p1", Mode.SYNC, False, lambda out, err: results.append(out))
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].task is not None, 5))
        self.assertIs(self.status(), Status.SYNCING)          # "Preparing…" at once
        before = len(ticks)
        spin_until(lambda: False, 1.0)
        self.assertIsNone(self.engine.runtime["p1"].run)       # still preparing...
        self.assertGreaterEqual(len(ticks) - before, 5)        # ...and the loop went on meanwhile
        self.assertTrue(spin_until(lambda: results, 30))

    def test_stop_while_preparing(self):
        self.align()
        self.slow_prepare(1.5)
        logs_before = len(list(self.engine.paths.log_dir("p1").iterdir()))
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].task is not None, 5))
        self.engine.stop("p1")
        self.assertTrue(spin_until(lambda: self.engine.active is None and self.status() is not Status.SYNCING, 10))
        spin_until(lambda: False, 0.5)
        self.assertEqual(len(list(self.engine.paths.log_dir("p1").iterdir())), logs_before)   # never started
        self.assertNotIn("new.txt", self.remote_files())

    def test_removed_while_preparing(self):
        # the preparation went on: it put the state and the marker back, and
        # could still rename on Koofr
        self.align()
        os.rename(self.local / "f00.txt", self.local / "F00.txt")       # a rename by case alone, for Koofr
        self.slow_prepare(1.5)
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: rt.task is not None, 5))
        self.engine.remove_pair("p1")
        paths = self.engine.paths
        state = [paths.workdir("p1"), paths.filters_file("p1"), paths.log_dir("p1"),
                 Path(preflight.marker_path(self.pair))]
        self.assertTrue(spin_until(lambda: self.engine.active is None and not any(p.exists() for p in state), 10),
                        [p for p in state if p.exists()])
        spin_until(lambda: False, 0.5)
        self.assertFalse(any(p.exists() for p in state))
        self.assertIn("f00.txt", self.remote_files())           # not renamed on Koofr

    def test_stop_clicked_twice_stays_graceful(self):
        # bisync takes a while to stop; a second click in the meantime must
        # not end it at once: that left its lock and the next run refused
        self.align()
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: rt.run is not None and rt.transferring, 30))
        run = rt.run
        changed = []
        self.engine.connect("pair-changed", lambda _e, pid: changed.append(pid))
        self.engine.stop("p1")
        self.assertTrue(run.stopping)
        self.assertIn("p1", changed)                    # the window shows "Stopping…" at once
        self.engine.stop("p1")
        self.assertEqual(run._interrupts, 1)
        self.assertTrue(spin_until(lambda: not run.running, 120))
        self.assertEqual(run.outcome.message, "stopped")
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.assertTrue(spin_until(lambda: self.engine.active is None, 10))
        # the next run starts clean: no "prior lock file", nothing to repair
        self.engine.request_sync("p1", "manual")
        ok = spin_until(lambda: self.status() is Status.OK and "big.bin" in self.remote_files(), 60)
        self.assertTrue(ok, (self.status(), rt.message, rt.last_outcome and rt.last_outcome.message))

    def test_stop_then_pause_or_quit_stays_graceful(self):
        # Pause, Pause All and Quit asked for the stop again, which ended
        # rclone at once: the next run then needed a repair
        self.align()
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: rt.run is not None and rt.transferring, 30))
        run = rt.run
        self.engine.stop("p1")
        self.engine.set_paused("p1", True)
        self.engine.set_paused_all(True)
        done = []
        self.engine.shutdown(lambda: done.append(True))
        self.assertEqual(run._interrupts, 1)
        self.assertTrue(spin_until(lambda: done, 120))
        self.assertEqual(run.outcome.message, "stopped")

    def test_a_metered_connection_stops_an_automatic_run(self):
        self.align()
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "poll")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        spin_until(lambda: False, 1)
        self.engine.blocked_reason = lambda: "Paused on a metered connection"
        self.engine._on_conditions()
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is None, 120))
        self.assertIs(self.status(), Status.PAUSED)
        self.assertNotIn("big.bin", self.remote_files())
        # back on a normal network: the sync starts again on its own and finishes
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.engine.blocked_reason = lambda: ""
        self.engine._on_conditions()
        self.assertTrue(spin_until(lambda: "big.bin" in self.remote_files(), 120))
        self.assertTrue(spin_until(lambda: self.status() is Status.OK, 30))

    def changed_on_both_sides(self, name, here, there, here_newer):
        """A conflict for the next run: `name` rewritten on both sides, one an
        hour older than the other."""
        now = time.time()
        (self.remote / name).write_text(there)
        os.utime(self.remote / name, (now, now - (3600 if here_newer else 0)))
        (self.local / name).write_text(here)
        os.utime(self.local / name, (now, now - (0 if here_newer else 3600)))

    def trash_here(self):
        trash = self.tmp / "trash"
        trash.mkdir()
        self.engine.trash = lambda path: shutil.move(path, trash / os.path.basename(path))
        return trash

    def no_conflict_copies(self):
        return not [n for n in self.remote_files() + os.listdir(self.local) if n.count(".") > 1]

    def test_the_folder_rule_resolves_new_conflicts(self):
        self.align()
        trash = self.trash_here()
        self.engine.set_conflict_rule("p1", "newer")
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        self.changed_on_both_sides("f00.txt", "new here", "old on Koofr", here_newer=True)
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: "resolved" in told, 60))
        # the next run takes the outcome to Koofr: one file, the newer version
        self.assertTrue(spin_until(lambda: (self.remote / "f00.txt").exists() and self.no_conflict_copies()
                                   and (self.remote / "f00.txt").read_text() == "new here", 60))
        self.assertEqual((self.local / "f00.txt").read_text(), "new here")
        (gone,) = os.listdir(trash)                     # Koofr's version, in the wastebasket
        self.assertEqual((trash / gone).read_text(), "old on Koofr")
        self.assertNotIn("conflicts", told)
        self.assertEqual(self.engine.conflicts("p1"), [])

    def test_conflicts_wait_for_the_user_then_resolve_all(self):
        self.align()
        self.trash_here()
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        self.changed_on_both_sides("f00.txt", "new here", "old on Koofr", here_newer=True)
        self.changed_on_both_sides("f01.txt", "old here", "new on Koofr", here_newer=False)
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: "conflicts" in told, 60))
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is None, 30))
        items = self.engine.conflicts("p1")
        self.assertEqual([c.original for c in items], ["f00.txt", "f01.txt"])      # the rule is "ask"
        done, errors = self.engine.resolve_conflicts("p1", [decide(c, "newer") for c in items])
        self.assertEqual((done, errors), (2, []))
        self.assertTrue(spin_until(lambda: self.no_conflict_copies()
                                   and (self.remote / "f00.txt").exists() and (self.remote / "f01.txt").exists(), 60))
        self.assertEqual((self.remote / "f00.txt").read_text(), "new here")
        self.assertEqual((self.remote / "f01.txt").read_text(), "new on Koofr")
        self.assertEqual((self.local / "f01.txt").read_text(), "new on Koofr")
        self.assertNotIn("resolved", told)

    def test_left_out_names_renamed_then_synced(self):
        if not _setup.case_sensitive(self.local):
            self.skipTest("A.txt and a.txt are one file here (NTFS)")
        self.align()
        (self.local / "A.txt").write_text("upper")
        (self.local / "a.txt").write_text("lower")
        (self.local / "foto \U0001F600.txt").write_text("smile")
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: len(rt.refused) == 2 and rt.run is None, 60))
        self.assertTrue(spin_until(lambda: "A.txt" in self.remote_files(), 30))
        plan = rename_plan(str(self.local), rt.refused, "/test")
        done, errors = self.engine.rename_refused("p1", [(item.path, new) for item, new in plan if new])
        self.assertEqual((done, errors), (2, []))
        self.assertEqual(rt.refused, [])
        self.assertTrue(spin_until(lambda: {"a (2).txt", "foto.txt"} <= set(self.remote_files()), 60))
        self.assertEqual((self.remote / "a (2).txt").read_text(), "lower")

    def test_a_move_from_koofr_is_not_taken_for_a_change_here(self):
        self.align()
        # the alignment's sealing run first
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        runs = []
        self.engine.connect("run-finished", lambda _e, pid, reason: runs.append(reason))
        inode = (self.local / "f05.txt").stat().st_ino
        (self.remote / "f05.txt").rename(self.remote / "renamed-there.txt")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: runs, 60))
        self.assertEqual((self.local / "renamed-there.txt").stat().st_ino, inode)      # moved, not downloaded
        self.assertEqual(self.engine.runtime["p1"].last_outcome.plan.moved_local, [("f05.txt", "renamed-there.txt")])
        # the watcher saw rclone move the file here: that is no change to sync back
        spin_until(lambda: False, engine_mod.DEBOUNCE_S + 3)
        self.assertEqual(runs, ["manual"])
        self.assertIn("renamed-there.txt", [f.path for f in self.engine.recent.items])    # in the tray's list

    def other_pair(self, name="other", files=5):
        local = self.tmp / name
        local.mkdir()
        for i in range(files):
            (local / f"{name}-{i}.txt").write_text(str(i))
        (self.remote_root / name).mkdir()
        return self.engine.add_pair(str(local), f"/{name}")

    def test_two_empty_folders_align_and_sync(self):
        # empty but for what Spola leaves out: bisync's listings are empty,
        # and it refuses a run from them ("Empty prior Path1 listing")
        local = self.tmp / "empty"
        local.mkdir()
        (local / ".cache").mkdir()              # Koofr Desktop's, excluded
        (self.remote_root / "empty").mkdir()
        pair = self.engine.add_pair(str(local), "/empty")
        rt = self.engine.runtime[pair.id]
        _kept, seen = self.preview_of(pair.id)                 # the first alignment: its preview, then the confirmation
        self.assertTrue(spin_until(lambda: seen, 60))
        self.assertTrue(self.engine.confirm(pair.id, Mode.RESYNC, False))
        self.assertTrue(spin_until(lambda: pair.initialized and self.engine.active is None
                                   and not self.engine.queue, 60))          # the alignment and its seal
        self.assertIsNot(rt.status, Status.NEEDS_REPAIR, rt.message)
        (local / "first.txt").write_text("x")
        self.engine.request_sync(pair.id, "manual")
        self.assertTrue(spin_until(lambda: (self.remote_root / "empty" / "first.txt").exists()
                                   and rt.status is Status.OK, 60), (rt.status, rt.message))
        # the sync that became a resync was sealed too: --recover has its .lst-old
        self.assertTrue(spin_until(lambda: not self.engine.queue and self.engine.active is None, 30))
        self.assertTrue(list(self.engine.paths.workdir(pair.id).glob("*.lst-old")))

    def test_two_empty_folders_with_the_same_new_file_ask_first(self):
        # the run of two empty folders is a resync, which settles a file new
        # on both sides by date: the older copy would be lost, unasked
        local = self.tmp / "empty"
        local.mkdir()
        remote = self.remote_root / "empty"
        remote.mkdir()
        pair = self.engine.add_pair(str(local), "/empty")
        rt = self.engine.runtime[pair.id]
        _kept, seen = self.preview_of(pair.id)                 # the first alignment: its preview, then the confirmation
        self.assertTrue(spin_until(lambda: seen, 60))
        self.assertTrue(self.engine.confirm(pair.id, Mode.RESYNC, False))
        self.assertTrue(spin_until(lambda: pair.initialized and self.engine.active is None
                                   and not self.engine.queue, 60))
        (remote / "same.txt").write_text("from Koofr")
        (local / "same.txt").write_text("from here, later")
        self.engine.request_sync(pair.id, "manual")
        self.assertTrue(spin_until(lambda: rt.status is Status.NEEDS_REPAIR and self.engine.active is None, 60),
                        (rt.status, rt.message))
        self.assertEqual((remote / "same.txt").read_text(), "from Koofr")
        self.assertEqual((local / "same.txt").read_text(), "from here, later")

    def test_a_folder_removed_and_added_again_is_not_replaced(self):
        # the pair removed left its marker; the new pair, another id, took it
        # for the sign of a folder replaced and refused its preview
        pair = self.other_pair()
        self.assertTrue(spin_until(lambda: os.path.exists(preflight.marker_path(pair)), 5))
        self.engine.remove_pair(pair.id)
        self.assertFalse(os.path.exists(preflight.marker_path(pair)))
        # a marker of a pair removed by an older Spola, which left it there
        Path(preflight.marker_path(pair)).write_text("removed00000\n")
        again = self.engine.add_pair(pair.local, pair.remote)
        self.assertEqual(preflight.check_root_identity(again), [])
        _kept, got = self.preview_of(again.id)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(got[0][1], "")

    def preview_of(self, pair_id, mode=Mode.RESYNC, force=False, **kw):
        got = []
        kept = self.engine.preview(pair_id, mode, force, lambda out, err: got.append((out, err)), **kw)
        return kept, got

    def logs(self, pair_id):
        folder = self.engine.paths.log_dir(pair_id)
        return len(list(folder.glob("*.jsonl*"))) if folder.exists() else 0

    def test_a_preview_runs_next_to_another_folders_sync(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        other = self.other_pair()
        _kept, got = self.preview_of(other.id)
        self.assertTrue(spin_until(lambda: got, 60))
        # done while the other folder still uploads, and it decided nothing
        self.assertIsNotNone(self.engine.runtime["p1"].run)
        self.assertEqual(self.engine.active.pair_id, "p1")
        outcome, error = got[0]
        self.assertEqual(error, "")
        self.assertEqual(sorted(outcome.plan.upload), [f"other-{i}.txt" for i in range(5)])
        self.assertIs(self.engine.runtime[other.id].status, Status.NEEDS_SETUP)
        self.engine.stop("p1")

    def test_a_confirmed_run_waits_its_turn_and_says_so(self):
        # another folder syncs for a while: what the user confirms is not
        # lost, it says it is confirmed, and goes in the order given
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        first, second = self.other_pair("first"), self.other_pair("second")
        seen = {pair.id: self.preview_of(pair.id)[1] for pair in (first, second)}
        self.assertTrue(spin_until(lambda: all(seen.values()), 60))
        changed = []
        self.engine.connect("pair-changed", lambda _e, pid: changed.append(pid))
        self.assertTrue(self.engine.confirm(second.id, Mode.RESYNC, False))
        self.assertTrue(self.engine.confirm(first.id, Mode.RESYNC, False))
        for pair in (first, second):
            self.assertIs(self.engine.runtime[pair.id].status, Status.PENDING)
            self.assertTrue(self.engine.confirmed_waiting(pair.id))
            self.assertIn(pair.id, changed)                     # the window hears it at once
        self.assertEqual([t.pair_id for t in self.engine.queue if t.reason == "confirmed"], [second.id, first.id])
        # an automatic run neither queues behind it nor changes what it says
        queued = list(self.engine.queue)
        self.engine.sync_all("poll")
        self.engine._on_conditions()
        self.assertEqual(self.engine.queue, queued)
        self.assertIs(self.engine.runtime[first.id].status, Status.PENDING)
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.engine.stop("p1")
        for pair in (first, second):
            self.assertTrue(spin_until(lambda pair=pair: pair.initialized
                                       and self.engine.runtime[pair.id].status is Status.OK, 60))
        self.assertFalse(self.engine.confirmed_waiting(first.id))

    def test_a_rename_by_case_alone_is_done_on_koofr_first(self):
        if not _setup.case_sensitive(self.remote):
            # the "Koofr" of the tests is a local folder: on NTFS it does not
            # tell the two names apart either, and the test reads its folders
            # by name; the rename on Koofr itself was tried there (appendix V)
            self.skipTest("a local folder as Koofr, on NTFS")
        self.align()
        (self.local / "Caps").mkdir()
        for i in range(3):
            (self.local / "Caps" / f"clip-{i}.txt").write_text(str(i))
        self.assertTrue(spin_until(lambda: (self.remote / "Caps" / "clip-2.txt").exists()
                                   and self.engine.active is None and not self.engine.queue, 30))
        os.rename(self.local / "Caps", self.local / "caps")
        finished = []
        self.engine.connect("run-finished", lambda _e, pid, reason: finished.append(reason))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: "manual" in finished, 30))
        self.assertEqual(sorted(p.name for p in self.remote.iterdir() if p.is_dir()), ["caps"])
        self.assertEqual(len(list((self.remote / "caps").iterdir())), 3)
        plan = self.engine.runtime["p1"].last_outcome.plan
        self.assertEqual(plan.moved_remote, [("Caps", "caps")])
        self.assertEqual(plan.upload + plan.delete_remote + plan.download + plan.delete_local, [])
        # the rename seen by the folder monitor: one more run, with nothing to do
        self.assertTrue(spin_until(lambda: self.status() is Status.OK and not self.engine.queue, 30))
        self.assertEqual(sorted(p.name for p in self.local.iterdir() if p.is_dir()), ["caps"])

    def test_confirming_needs_the_finished_preview(self):
        # nothing to confirm without the preview, nor while it still runs, nor
        # for another question: the preview goes on, and once it is done the
        # confirmation is taken
        refused = []
        self.assertFalse(self.engine.confirm("p1", Mode.RESYNC, False, lambda out, err: refused.append(err)))
        kept, got = self.preview_of("p1")
        self.assertFalse(self.engine.confirm("p1", Mode.RESYNC, False))
        self.assertFalse(self.engine.confirm("p1", Mode.SYNC, True))
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(got[0][1], "")                                   # not stopped
        self.assertIs(self.status(), Status.NEEDS_SETUP)
        self.assertTrue(self.engine.confirm("p1", Mode.RESYNC, False))
        self.assertIs(self.status(), Status.PENDING)
        self.assertTrue(spin_until(lambda: self.pair.initialized and self.status() is Status.OK, 60))
        self.assertEqual(len(self.remote_files()), 30)
        self.assertTrue(spin_until(lambda: refused, 5))
        self.assertEqual(refused, ["The preview is no longer current."])

    def test_a_preview_overtaken_by_a_change_cannot_be_confirmed(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        for i in range(10):
            (self.local / f"f{i:02}.txt").unlink()
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30))
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(len(got[0][0].plan.delete_remote), 10)
        (self.local / "later.txt").write_text("x")                       # the folder monitor sees it
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].preview is None, 10))
        self.assertFalse(self.engine.confirm("p1", Mode.SYNC, True))
        self.assertIs(self.status(), Status.NEEDS_CONFIRMATION)
        self.assertEqual(len(self.remote_files()), 30)

    def test_a_confirmation_is_bound_to_the_deletions_it_was_given_for(self):
        # confirmed for 10 deletions; before its turn, 15 more files vanish on
        # Koofr (unseen: no feed in the tests): the run stops again instead of
        # deleting them here on the strength of that confirmation
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        for i in range(10):
            (self.local / f"f{i:02}.txt").unlink()
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30))
        self.assertEqual(self.engine.runtime["p1"].safety, "deletes")
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(kept.counts, (30, 30))                            # the entries of the last run's listings
        for name in self.remote_files()[:15]:
            (self.remote / name).unlink()
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out)))
        self.assertTrue(spin_until(lambda: results, 60))
        self.assertIs(results[0].result, Result.NEEDS_CONFIRMATION)
        self.assertEqual(results[0].safety, "deletes")
        self.assertIs(self.status(), Status.NEEDS_CONFIRMATION)
        self.assertEqual(len(list(self.local.glob("f*.txt"))), 20)       # nothing deleted here
        self.assertEqual(len(self.remote_files()), 15)                     # nor there

    def test_files_gone_from_both_sides_are_confirmed_once(self):
        # the same files deleted here and on Koofr before the run (a deletion
        # on the web and here, two computers): bisync counts them for its
        # deletion check, the preview has nothing to delete for them, and
        # the confirmed run must go through, not stop again for them
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        self.engine.set_paused("p1", True)
        for i in range(20):
            (self.local / f"f{i:02}.txt").unlink()
            (self.remote / f"f{i:02}.txt").unlink()
        self.engine.set_paused("p1", False)
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 60))
        rt = self.engine.runtime["p1"]
        self.assertEqual(rt.safety, "deletes")
        self.assertEqual(rt.last_outcome.excess, {"Path1": (20, 30)})      # what bisync counted
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertTrue(got[0][0].plan.empty)                              # nothing to do for them
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out)))
        self.assertTrue(spin_until(lambda: results, 60))
        self.assertIs(results[0].result, Result.OK, results[0].message)
        self.assertTrue(spin_until(lambda: self.status() is Status.OK and not self.engine.queue, 30))
        self.assertEqual(len(self.remote_files()), 10)
        self.assertEqual(len(list(self.local.glob("f*.txt"))), 10)

    def test_files_changed_here_and_some_on_koofr_too_are_confirmed_once(self):
        # 60 of 90 files changed here (over the 50 that go unasked), 20 of
        # them changed on Koofr as well: conflicts, which the preview lists
        # apart from the uploads; the confirmed run must not stop again
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        for i in range(30, 90):
            (self.local / f"f{i:02}.txt").write_text(str(i))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: len(self.remote_files()) == 90 and self.engine.active is None
                                   and not self.engine.queue, 60))
        self.engine.set_paused("p1", True)
        for i in range(60):
            (self.local / f"f{i:02}.txt").write_text(f"changed here {i}")
        for i in range(20):
            (self.remote / f"f{i:02}.txt").write_text(f"changed on Koofr {i}")
        self.engine.set_paused("p1", False)
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 60))
        rt = self.engine.runtime["p1"]
        self.assertEqual((rt.safety, rt.safety_changed), ("changes", 60))
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        plan = got[0][0].plan
        self.assertEqual(len(plan.conflicts), 20)
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out)))
        self.assertTrue(spin_until(lambda: results, 120))
        self.assertIs(results[0].result, Result.OK, results[0].message)
        self.assertTrue(spin_until(lambda: self.status() is Status.CONFLICTS and not self.engine.queue, 60),
                        (self.status(), rt.message, [t.reason for t in self.engine.queue], self.engine.active))
        self.assertEqual(len(self.engine.conflicts("p1")), 20)
        self.assertEqual((self.remote / "f30.txt").read_text(), "changed here 30")

    def test_every_file_changed_is_confirmed_in_two_steps(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        self.engine.set_paused("p1", True)                                 # the monitor must not sync each one
        for i in range(30):
            (self.local / f"f{i:02}.txt").write_text(f"changed {i}")
        self.engine.set_paused("p1", False)
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30))
        self.assertEqual(self.engine.runtime["p1"].safety, "all-changed")
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(len(got[0][0].plan.upload), 30)
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out)))
        self.assertTrue(spin_until(lambda: results, 60))
        self.assertIs(results[0].result, Result.OK)                        # the bounded run, then --force
        self.assertEqual((self.remote / "f07.txt").read_text(), "changed 7")
        self.assertTrue(spin_until(lambda: self.status() is Status.OK and not self.engine.queue, 30))

    def test_restore_from_trash_puts_files_folders_and_dates_back(self):
        # a folder deleted on Koofr by mistake, Spola not run since: back from
        # the trash with today's dates (as Koofr does), its empty folder made
        # again, the files uploaded once more with their own dates
        self.align()
        (self.local / "sub" / "empty").mkdir(parents=True)
        for i in range(5):
            (self.local / "sub" / f"s{i}.txt").write_text(f"s{i}")
            os.utime(self.local / "sub" / f"s{i}.txt", (1_600_000_000 + i, 1_600_000_000 + i))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: (self.remote / "sub" / "empty").is_dir()
                                   and self.engine.active is None and not self.engine.queue, 30))
        bin_ = self.tmp / "koofr-trash"
        bin_.mkdir()
        mount = self.engine.cfg.mount_id
        deleted = int(time.time() * 1000)
        entries = []
        for f in sorted((self.remote / "sub").iterdir()):
            if f.is_file():
                shutil.move(str(f), bin_ / f.name)
                entries.append(trash_mod.Entry(mount, f"/test/sub/{f.name}", 2, deleted))
        shutil.rmtree(self.remote / "sub")

        class Bin:
            def page(self, cursor):
                return list(entries), None

            def undelete(self, chosen):
                assert chosen
                for e in chosen:
                    name = e.path.rsplit("/", 1)[1]
                    target = Path(str(bin_.parent / "remote") + "/test/sub") / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(bin_ / name), target)
                    os.utime(target)                    # Koofr: today's date
                    entries.remove(e)
                return "job"

            def job_done(self, job_id):
                return True
        self.engine.koofr_trash = lambda creds: Bin()
        found = []
        self.engine.trash_deletions("p1", lambda d, err: found.append((d, err)))
        self.assertTrue(spin_until(lambda: found, 10))
        (deletion,), error = found[0]
        self.assertEqual((len(deletion.entries), error), (5, ""))
        finished, outcome = [], []
        self.engine.connect("run-finished", lambda _e, pid, reason: finished.append(reason))
        (self.local / "sub" / "s4.txt").write_text("s4")     # changed here since: the same size, a new date
        self.assertTrue(self.engine.restore_from_trash("p1", deletion.entries, lambda n, t: None,
                                                       lambda r, up, err: outcome.append((r, up, err))))
        self.assertIs(self.status(), Status.SYNCING)
        self.engine.request_sync("p1", "manual")               # waits: no run while restoring
        self.assertTrue(spin_until(lambda: outcome, 30))
        result, reuploads, error = outcome[0]
        self.assertEqual((result.restored, result.left, reuploads, error), (5, [], 4, ""))
        self.assertTrue((self.remote / "sub" / "empty").is_dir())
        self.assertTrue(spin_until(lambda: "manual" in finished and self.engine.active is None
                                   and not self.engine.queue, 60))
        last = self.engine.runtime["p1"].last_outcome
        self.assertIs(last.result, Result.OK, last.message)
        for i in range(4):
            self.assertAlmostEqual((self.remote / "sub" / f"s{i}.txt").stat().st_mtime, 1_600_000_000 + i, delta=0.01)
        self.assertEqual((self.local / "sub" / "s4.txt").read_text(), "s4")       # this computer's version kept
        self.assertIn(self.status(), (Status.OK, Status.CONFLICTS))

    def test_a_preview_knows_the_files_of_the_last_run(self):
        # before a first alignment there is no last run; after it, both sides' files
        kept, got = self.preview_of("p1")
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertIsNone(kept.bases)
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        kept, got = self.preview_of("p1", Mode.SYNC)
        self.assertTrue(spin_until(lambda: got, 60))
        here, there = kept.bases
        self.assertEqual(len(here), 30)
        self.assertEqual(here, there)
        self.assertEqual(here["f07.txt"], 1)

    def test_the_folder_gone_from_koofr_is_said_and_made_again(self):
        # deleted on Koofr (from another computer): Spola stops, says so,
        # and this computer's files go up again once the folder is back
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        # the real question to rclone, on a folder there and one gone
        env = engine_mod.koofr_env("dev", "")
        self.assertTrue(jobs.koofr_folder_exists(RCLONE, env, self.engine.paths, self.pair, str(self.remote)))
        self.assertIs(jobs.koofr_folder_exists(RCLONE, env, self.engine.paths, self.pair,
                                               str(self.remote_root / "gone")), False)
        # (the "Koofr" of the tests is made again before every run: the answer is given)
        real = engine_mod.jobs.koofr_folder_exists
        engine_mod.jobs.koofr_folder_exists = lambda *a, **kw: False
        self.addCleanup(setattr, engine_mod.jobs, "koofr_folder_exists", real)
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        shutil.rmtree(self.remote)
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: rt.gone == "koofr-gone", 30), rt.gone)
        self.assertIs(self.status(), Status.NEEDS_REPAIR)
        self.assertIn("gone", told)
        self.assertEqual(len(list(self.local.glob("f*.txt"))), 30)          # nothing touched here
        self.engine.request_sync("p1", "poll")                              # no run of its own meanwhile
        spin_until(lambda: False, 1)
        self.assertEqual(rt.gone, "koofr-gone")
        engine_mod.jobs.koofr_folder_exists = real
        shutil.rmtree(self.remote, ignore_errors=True)
        made = []
        self.engine.make_koofr_folder("p1", made.append)
        self.assertTrue(spin_until(lambda: made, 30))
        self.assertEqual((made[0], rt.gone), ("", "koofr-empty"))
        self.assertTrue(self.remote.is_dir())
        _kept, seen = self.preview_of("p1")                                 # the repair, after its preview
        self.assertTrue(spin_until(lambda: seen, 60))
        self.assertTrue(self.engine.confirm("p1", Mode.RESYNC, False))
        self.assertTrue(spin_until(lambda: self.status() is Status.OK and len(self.remote_files()) == 30, 60))
        self.assertEqual(rt.gone, "")

    def test_a_folder_emptied_here_or_on_koofr_is_said(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        rt = self.engine.runtime["p1"]
        for f in self.remote.iterdir():
            f.unlink()
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: rt.gone == "koofr-empty" and self.status() is Status.NEEDS_REPAIR, 30))
        spin_until(lambda: False, 2)                                        # the folder is there: not "gone"
        self.assertEqual(rt.gone, "koofr-empty")
        self.assertEqual(len(list(self.local.glob("f*.txt"))), 30)

    def test_a_folder_emptied_here_is_said(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        rt = self.engine.runtime["p1"]
        for f in self.local.glob("f*.txt"):
            f.unlink()
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: rt.gone == "here-empty", 30), rt.gone)
        self.assertEqual(len(self.remote_files()), 30)                     # nothing touched on Koofr

    def test_a_corrupt_listing_is_a_repair_not_a_lane_taken_for_good(self):
        # the listing cannot be parsed (a crash with the disk full, a format
        # change): a repair in plain words (the kept copy, or a new
        # alignment), never a retry; the lane is free, another folder still syncs
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        lst = listings.find(self.engine.paths.workdir("p1"), 1)
        lst.write_text("# bisync listing v1 from x\nweird line\n", encoding="utf-8")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_REPAIR, 20))
        self.assertIn("damaged", self.engine.runtime["p1"].message)
        self.assertTrue(self.engine.kept_listings("p1"))
        self.assertIsNone(self.engine.active)
        other = self.other_pair()
        _kept, got = self.preview_of(other.id)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(len(got[0][0].plan.upload), 5)

    def test_an_outcome_that_cannot_be_read_frees_the_lane(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))

        def broken(pair, rt, then=None):
            raise RuntimeError("listing exploded")
        self.engine._refresh_listing = broken
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.ERROR, 30))
        self.assertIn("listing exploded", self.engine.runtime["p1"].message)
        self.assertIsNone(self.engine.active)
        self.assertIn("new.txt", self.remote_files())             # the run itself was done
        del self.engine._refresh_listing
        other = self.other_pair()
        _kept, got = self.preview_of(other.id)
        self.assertTrue(spin_until(lambda: got, 60))              # the pump still runs

    def test_a_log_that_cannot_be_written_does_not_stop_the_run(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        log_dir = self.engine.paths.log_dir("p1")
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o500)
        self.addCleanup(os.chmod, log_dir, 0o700)
        (self.local / "quiet.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: "quiet.txt" in self.remote_files() and self.engine.active is None
                                   and not self.engine.queue and self.status() is Status.OK, 30))

    def test_the_machine_about_to_sleep_stops_the_run_and_goes_on_after(self):
        # logind's PrepareForSleep: the run stops gracefully (bisync saves its
        # state), and goes again once the machine is awake
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(2_000_000))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        self.engine.before_sleep()
        self.assertTrue(spin_until(lambda: self.engine.active is None, 60))
        self.assertNotIn("big.bin", self.remote_files())
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.engine.after_sleep()
        self.assertTrue(spin_until(lambda: "big.bin" in self.remote_files() and self.status() is Status.OK, 60))

    def test_a_run_asked_for_during_a_run_goes_afterwards(self):
        # the run downloads a file into the folder's root; a request made
        # meanwhile (the feed, a retry) is kept for afterwards
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.remote / "from-koofr.txt").write_text("there")
        (self.local / "big.bin").write_bytes(os.urandom(1_500_000))
        before = self.logs("p1")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        self.engine.request_sync("p1", "poll")
        self.assertTrue(self.engine.runtime["p1"].rerun_requested)
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.assertTrue(spin_until(lambda: self.logs("p1") >= before + 2 and self.engine.active is None, 120))

    def test_a_lock_left_by_a_crash_is_said_and_tried_again(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(1_500_000))
        self.engine.request_sync("p1", "manual")
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: rt.run is not None and rt.stats is not None, 20))
        rt.run._proc.kill()                                   # as a crash or a power-off would
        self.assertTrue(spin_until(lambda: self.engine.active is None, 30))
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.ERROR and "interrupted" in rt.message, 30),
                        rt.message)
        self.assertTrue(rt.retry_source)

    def test_pausing_cancels_a_run_being_prepared(self):
        self.align()
        self.slow_prepare(1.5)
        before = self.logs("p1")
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].task is not None, 5))
        self.engine.set_paused("p1", True)
        self.assertTrue(spin_until(lambda: self.engine.active is None, 10))
        spin_until(lambda: False, 0.5)
        self.assertEqual(self.logs("p1"), before)             # never started
        self.assertIs(self.status(), Status.PAUSED)

    def test_stop_while_a_confirmation_is_prepared_keeps_the_question(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        for i in range(10):
            (self.local / f"f{i:02}.txt").unlink()
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30))
        _kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.slow_prepare(1.5)
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True))
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].task is not None, 5))
        self.engine.stop("p1")
        self.assertTrue(spin_until(lambda: self.engine.active is None and self.status() is not Status.SYNCING, 10))
        self.assertIs(self.status(), Status.NEEDS_CONFIRMATION)   # not "up to date"
        self.assertEqual(len(self.remote_files()), 30)

    def test_exclusions_changed_during_a_run_ask_for_a_repair_afterwards(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        os.environ["SPOLA_DEV_BWLIMIT"] = "20k"
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (self.local / "big.bin").write_bytes(os.urandom(1_500_000))
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].run is not None, 20))
        self.engine.set_filters("p1", jobs.read_filters(self.engine.paths, self.pair) + "- *.tmp\n")
        os.environ.pop("SPOLA_DEV_BWLIMIT")
        self.assertTrue(spin_until(lambda: self.engine.active is None, 120))
        self.assertIs(self.status(), Status.NEEDS_REPAIR)
        self.assertIn("exclusions", self.engine.runtime["p1"].message)

    def test_the_activity_feed_falls_back_on_any_failure(self):
        self.engine.feed_enabled = True
        self.engine.cfg.mount_id = ""
        real = engine_mod.koofr_api.primary_mount_id
        engine_mod.koofr_api.primary_mount_id = lambda *a: {}["mounts"]      # a KeyError, not an ApiError
        self.addCleanup(setattr, engine_mod.koofr_api, "primary_mount_id", real)
        self.engine.set_credentials(Credentials("dev", "", "secret"))
        self.pair.initialized = True
        self.engine._poll_activity()
        self.assertTrue(spin_until(lambda: not self.engine._activity_busy, 10))
        self.assertIs(self.engine.feed_ok, False)

    def test_a_damaged_listing_is_repaired_from_the_kept_copy(self):
        rt = self.engine.runtime["p1"]
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        kept = self.engine.kept_listings("p1")
        self.assertTrue(kept, "a successful run keeps its listings")
        workdir = self.engine.paths.workdir("p1")
        listing = listings.find(workdir, 1)
        listing.write_text("# bisync listing v1 from garbage\n@@@ not a line\n")   # a bad day: damaged
        (self.local / "f05.txt").write_text("changed here")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_REPAIR, 60), rt.message)
        self.assertEqual((self.remote / "f05.txt").read_text(), "5")
        # the other way round than a new alignment: the state known is put back, the change synced from it
        self.assertTrue(self.engine.restore_listings("p1", kept[0][0]))
        self.assertTrue(spin_until(lambda: self.status() is Status.OK and (self.remote / "f05.txt").read_text()
                                   == "changed here", 60), rt.message)
        self.assertNotIn(kept[0][0], [k for k, _t in self.engine.kept_listings("p1")])       # tried: not offered again

    def test_previous_versions_kept_found_and_restored(self):
        # the acceptance criteria: a file overwritten by a sync is restored
        # next to the current one and goes up at the next run; a file deleted
        # (on Koofr, so here by the sync) is found from its folder; nothing
        # but the retention deletes a copy
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        changed = []
        self.engine.connect("versions-changed", lambda _e, pid: changed.append(pid))
        time.sleep(1.1)
        (self.remote / "f01.txt").write_text("from another computer")
        (self.remote / "f02.txt").unlink()
        self.engine.request_sync("p1", "manual")
        # (while the run replaces it the file is briefly away: moved among the copies, then written)
        self.assertTrue(spin_until(lambda: text_of(self.local / "f01.txt") == "from another computer"
                                   and not (self.local / "f02.txt").exists() and self.status() is Status.OK, 60))
        idx = lambda: self.engine.versions_index("p1")      # noqa: E731
        self.assertTrue(spin_until(lambda: idx().of("f01.txt") and idx().of("f02.txt"), 30), changed)
        (old,) = idx().of("f01.txt")
        self.assertEqual(old.reason, "replaced")
        self.assertEqual([(v.rel, v.reason) for v in idx().deleted_under("")], [("f02.txt", "deleted")])
        self.assertEqual(idx().names_in(""), {"f01.txt", "f02.txt"})
        # a restore is refused while a run of the pair goes on (the watcher's
        # follow-up after the files the run wrote, under a loaded machine):
        # wait for none, as the window's user would after being told
        idle = lambda: self.engine.active is None and not self.engine.queue     # noqa: E731
        # restored as a copy next to the file: on Koofr after the next run
        self.assertTrue(spin_until(idle, 60))
        rel = self.engine.restore_version("p1", old.id, "copy")
        self.assertEqual((self.local / rel).read_text(), "1")
        self.assertTrue(spin_until(lambda: (self.remote / rel).exists() and self.status() is Status.OK, 60))
        self.assertEqual((self.remote / rel).read_text(), "1")
        self.assertEqual((self.local / "f01.txt").read_text(), "from another computer")
        # the deleted one goes back in its place, and up again
        self.assertTrue(spin_until(idle, 60))
        self.engine.restore_version("p1", idx().deleted_under("")[0].id, "copy")
        self.assertTrue(spin_until(lambda: (self.remote / "f02.txt").exists() and self.status() is Status.OK, 60))
        # replaced in place: the current file is kept first, so it can be undone
        self.assertTrue(spin_until(idle, 60))
        self.engine.restore_version("p1", old.id, "replace")
        self.assertEqual((self.local / "f01.txt").read_text(), "1")
        self.assertTrue(spin_until(lambda: any(v.reason == "restored" for v in idx().of("f01.txt")), 30))
        self.assertTrue(spin_until(lambda: text_of(self.remote / "f01.txt") == "1" and self.status() is Status.OK, 60))
        self.assertTrue(all(versions_mod.copy_path(self.engine.paths, v).exists() for v in idx().versions))

    def test_a_version_is_not_restored_during_a_run(self):
        # rclone may have the file open (on Windows it cannot then be moved
        # aside), and the run's listing would not know what the restore wrote
        rt = self.engine.runtime["p1"]
        with mock.patch.object(rt, "run", object()), \
                mock.patch.object(rt, "task", engine_mod.Task("p1", Mode.SYNC)):
            with self.assertRaises(ValueError) as caught:
                self.engine.restore_version("p1", "any", "copy")
        self.assertEqual(str(caught.exception), engine_mod._("Wait for the current sync to finish."))
        # a preview (a dry run) writes nothing here: no reason to wait for it
        with mock.patch.object(rt, "run", object()), \
                mock.patch.object(rt, "task", engine_mod.Task("p1", Mode.SYNC, dry_run=True)):
            with self.assertRaises(ValueError) as caught:
                self.engine.restore_version("p1", "any", "copy")
        self.assertEqual(str(caught.exception), "no such version")

    def test_the_copy_not_kept_of_a_conflict_is_kept_among_the_copies(self):
        self.align()
        self.changed_on_both_sides("f00.txt", "new here", "old on Koofr", here_newer=True)
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.engine.conflicts("p1") and self.engine.runtime["p1"].run is None, 60))
        (conflict,) = self.engine.conflicts("p1")
        self.engine.resolve_conflict("p1", conflict, "local")
        self.assertEqual((self.local / "f00.txt").read_text(), "new here")
        idx = lambda: self.engine.versions_index("p1")      # noqa: E731
        self.assertTrue(spin_until(lambda: any(v.reason == "conflict" for v in idx().of("f00.txt")), 30))
        (kept,) = [v for v in idx().of("f00.txt") if v.reason == "conflict"]
        self.assertEqual(versions_mod.copy_path(self.engine.paths, kept).read_text(), "old on Koofr")

    def test_koofr_trash_read_page_by_page_retried_and_kept_a_while(self):
        engine_mod.TRASH_RETRY_S = 0
        self.addCleanup(setattr, engine_mod, "TRASH_RETRY_S", 2)
        entry = lambda i: trash_mod.Entry("m", f"/test/f{i}.txt", 1, 1_800_000_000_000 - i)      # noqa: E731

        class Api:
            def __init__(self, fail_times=1, broken=False):
                self.calls, self.fail_times, self.broken = [], fail_times, broken

            def page(self, cursor):
                self.calls.append(cursor)
                if cursor == "2" and (self.broken or self.fail_times > 0):
                    self.fail_times -= 1
                    raise koofr_api.ApiError("/api/v2/trash: The read operation timed out")
                n = int(cursor or 1)
                return [entry(n * 10 + i) for i in range(3)], (str(n + 1) if n < 3 else None)
        api = Api()
        self.engine.koofr_trash = lambda creds: api
        seen = []
        self.engine.trash_entries(lambda entries, done, error: seen.append((len(entries), done, error)))
        self.assertTrue(spin_until(lambda: seen and seen[-1][1], 10), seen)
        self.assertEqual(seen[-1], (9, True, ""))
        self.assertIn((3, False, ""), seen)                       # the first page shown before the end
        self.assertEqual(api.calls, [None, "2", "2", "3"])        # the page that timed out asked again
        # read again within a few minutes: from memory, no call
        again = []
        self.engine.trash_entries(lambda entries, done, error: again.append((len(entries), done)))
        self.assertTrue(spin_until(lambda: again, 5))
        self.assertEqual((again, len(api.calls)), ([(9, True)], 4))
        # a page that keeps failing: what was read stays, with the reason
        self.engine.invalidate_trash()
        api2 = Api(broken=True)
        self.engine.koofr_trash = lambda creds: api2
        last = []
        self.engine.trash_entries(lambda entries, done, error: last.append((len(entries), done, error)))
        self.assertTrue(spin_until(lambda: last and last[-1][1], 10))
        self.assertEqual(last[-1][:2], (3, True))
        self.assertIn("timed out", last[-1][2])
        self.assertEqual(api2.calls.count("2"), engine_mod.TRASH_TRIES)
        # the deletions of one folder, from the same reading
        self.engine.invalidate_trash()
        self.engine.koofr_trash = lambda creds: Api(fail_times=0)
        self.engine.cfg.mount_id = "m"
        found = []
        self.engine.trash_deletions("p1", lambda d, err: found.append((d, err)))
        self.assertTrue(spin_until(lambda: found, 10))
        self.assertEqual(sum(len(d.entries) for d in found[0][0]), 9)

    def test_a_sign_out_during_a_trash_reading_is_told_once(self):
        import threading
        entry = lambda i: trash_mod.Entry("m", f"/test/f{i}.txt", 1, 1_800_000_000_000 - i)      # noqa: E731
        gate = threading.Event()

        class Api:
            calls = []

            def page(self, cursor):
                self.calls.append(cursor)
                if cursor:                  # the second page comes after the sign-out
                    gate.wait(10)
                n = int(cursor or 1)
                return [entry(n * 10 + i) for i in range(3)], str(n + 1)
        api = Api()
        self.engine.koofr_trash = lambda creds: api
        seen = []
        self.engine.trash_entries(lambda entries, done, error: seen.append((len(entries), done, error)))
        self.assertTrue(spin_until(lambda: (3, False, "") in seen, 10), seen)      # the first page shown
        self.engine.set_credentials(None)
        gate.set()
        spin_until(lambda: False, 1)        # the reading of the account signed out from ends meanwhile
        told = (0, True, engine_mod._("Sign in to Koofr first."))
        self.assertEqual(([s for s in seen if s[1]], seen[-1]), ([told], told))      # once, and nothing after
        self.assertEqual(api.calls, [None, "2"])                                    # its loop stopped too

    def test_a_verification_compares_both_sides_and_changes_nothing(self):
        rt = self.engine.runtime["p1"]
        self.align()
        # the alignment the user confirmed is followed by a verification of its own
        self.assertTrue(spin_until(lambda: rt.preview is not None and rt.preview.mode is Mode.CHECK
                                   and rt.preview.outcome is not None, 60))
        self.assertEqual(rt.preview.reason, "after-align")
        self.assertEqual(rt.preview.outcome.identical, 30)
        self.assertEqual(rt.preview.outcome.plan.conflicts, [])
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append((kind, msg)))
        # Koofr's copy rewritten with the same size and date: only a checksum tells
        stamp = os.stat(self.remote / "f03.txt").st_mtime_ns
        (self.remote / "f03.txt").write_text("4")
        os.utime(self.remote / "f03.txt", ns=(stamp, stamp))
        (self.local / "only-here.txt").write_text("x")
        (self.remote / "only-there.txt").write_text("y")
        self.engine.set_paused("p1", True)                   # no sync meanwhile
        results = []
        self.engine.verify("p1", lambda out, err: results.append((out, err)))
        self.assertTrue(spin_until(lambda: results, 60))
        out, err = results[0]
        self.assertEqual(err, "")
        self.assertEqual(out.plan.conflicts, ["f03.txt"])
        self.assertEqual(out.plan.upload, ["only-here.txt"])
        self.assertEqual(out.plan.download, ["only-there.txt"])
        self.assertEqual(out.identical, 29)
        self.assertEqual((self.remote / "f03.txt").read_text(), "4")        # nothing changed
        self.assertEqual((self.local / "f03.txt").read_text(), "3")
        self.assertFalse((self.local / "only-there.txt").exists())
        self.assertIn(("verify", "3 differences between this computer and Koofr"), told)
        self.assertIs(rt.preview.outcome, out)                             # kept for the dialog

    def test_an_automatic_verification_yields_to_a_sync(self):
        rt = self.engine.runtime["p1"]
        self.slow_prepare(2.0)
        self.align()
        self.assertTrue(spin_until(lambda: rt.preview is not None and rt.preview.mode is Mode.CHECK, 60))
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: rt.preview is None or rt.preview.mode is not Mode.CHECK, 30))
        self.assertTrue(spin_until(lambda: (self.remote / "new.txt").exists() and self.status() is Status.OK, 60))

    def test_the_status_file_follows_the_engine(self):
        from spola import status as status_mod
        status_mod.DEBOUNCE_MS = 100
        self.addCleanup(setattr, status_mod, "DEBOUNCE_MS", 1000)
        path = self.engine.paths.state / "status.json"
        following = status_mod.StatusFile(self.engine)
        self.assertTrue(path.exists())                               # written at once
        data = json.loads(path.read_text())
        self.assertEqual(data["pairs"][0]["status"], "needs_setup")
        self.align()
        self.assertTrue(spin_until(lambda: json.loads(path.read_text())["pairs"][0]["status"] == "ok", 60))
        data = json.loads(path.read_text())
        self.assertEqual((data["overall"], data["pid"], data["pairs"][0]["initialized"]), ("ok", os.getpid(), True))
        self.assertLess(time.time() - data["updated"], 60)
        following.stop()
        self.assertFalse(path.exists())                              # Spola quitting: nothing stale left

    def test_a_nearly_full_disk_stops_a_run_before_it_starts(self):
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        rt = self.engine.runtime["p1"]
        (self.local / "new.txt").write_text("x")
        usage = shutil._ntuple_diskusage(10**12, 10**12 - 10**7, 10**7)        # 10 MB free
        with mock.patch.object(engine_mod.shutil, "disk_usage", return_value=usage):
            self.engine.request_sync("p1", "manual")
            self.assertTrue(spin_until(lambda: self.status() is Status.ERROR, 30), rt.message)
            self.assertIn("nearly full", rt.message)
            self.assertFalse((self.remote / "new.txt").exists())
        # a preview knows what would come down: the disk must hold it, with a margin
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: (self.remote / "new.txt").exists() and self.status() is Status.OK, 60))
        with open(self.remote / "big.bin", "wb") as f:
            f.truncate(100 * 2**20)                                           # sparse: nothing on disk
        kept, got = self.preview_of("p1", Mode.SYNC)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertGreaterEqual(got[0][0].plan.download_bytes, 100 * 2**20)
        usage = shutil._ntuple_diskusage(10**12, 10**12 - 250 * 2**20, 250 * 2**20)     # 250 MB free
        with mock.patch.object(engine_mod.shutil, "disk_usage", return_value=usage):
            self.engine.request_sync("p1", "manual")
            self.assertTrue(spin_until(lambda: self.status() is Status.ERROR, 30), rt.message)
            self.assertIn("would download", rt.message)
            self.assertFalse((self.local / "big.bin").exists())
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: (self.local / "big.bin").exists() and self.status() is Status.OK, 60))

    def test_many_files_changed_here_at_once_wait_for_a_confirmation(self):
        # a snapshot put back, a bulk edit: the files changed here would go up
        # over Koofr's copies; above the threshold the run does not start
        from spola import rclone as rclone_mod
        rclone_mod.MIN_FREE_CHANGES = 3
        self.addCleanup(setattr, rclone_mod, "MIN_FREE_CHANGES", 50)
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        self.engine.set_paused("p1", True)
        for i in range(10):
            (self.local / f"f{i:02}.txt").write_text(f"rewritten {i}")
        self.engine.set_paused("p1", False)
        rt = self.engine.runtime["p1"]
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30), rt.message)
        self.assertEqual(rt.safety, "changes")
        self.assertIn("10 of 30", rt.message)
        self.assertIn("confirm", told)
        self.assertEqual((self.remote / "f03.txt").read_text(), "3")        # nothing went up
        self.assertIsNone(self.engine.active)
        self.engine.sync_all("poll")                                       # automatic runs stay stopped
        spin_until(lambda: False, 1.5)
        self.assertEqual((self.remote / "f03.txt").read_text(), "3")
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got, 60))
        self.assertEqual(len(got[0][0].plan.upload), 10)
        # more files change before the confirmed run is prepared: it stops again
        self.slow_prepare(1.5)
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append((out, err))))
        self.assertTrue(spin_until(lambda: rt.task is not None, 5))
        for i in range(10, 25):
            (self.local / f"f{i:02}.txt").write_text(f"rewritten {i}")
        self.assertTrue(spin_until(lambda: results, 30))
        self.assertEqual(results[0][0], None)
        self.assertIn("25 of 30", results[0][1])
        self.assertIs(self.status(), Status.NEEDS_CONFIRMATION)
        self.assertEqual((self.remote / "f03.txt").read_text(), "3")
        # the watcher has told the 15 writes (Windows tells them some 3 s
        # later): a change told after the preview would put it out of date
        written = {f"f{i:02}.txt" for i in range(10, 25)}
        self.assertTrue(spin_until(lambda: written <= rt.states.pending, 30))
        # seen and confirmed for what it is now: up they go
        kept, got = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: got and self.engine.runtime["p1"].preview is kept, 60))
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True))
        self.assertTrue(spin_until(lambda: (self.remote / "f03.txt").read_text() == "rewritten 3"
                                   and self.status() is Status.OK, 60))

    def test_a_preview_is_kept_until_something_changes(self):
        kept, got = self.preview_of("p1")
        self.assertIs(self.status(), Status.NEEDS_SETUP)        # a preview decides nothing
        self.assertTrue(spin_until(lambda: got, 60))
        first, runs = got[0][0], self.logs("p1")
        self.assertIsNotNone(kept.free_bytes)                   # (the disk of the local "Koofr")
        # opened again: at once, the same answer, no new run
        again, got2 = self.preview_of("p1")
        self.assertTrue(spin_until(lambda: got2, 5))
        self.assertIs(again, kept)
        self.assertIs(got2[0][0], first)
        self.assertEqual(self.logs("p1"), runs)
        # after a change it is asked again
        self.engine.set_filters("p1", jobs.read_filters(self.engine.paths, self.pair) + "- *.tmp\n")
        third, got3 = self.preview_of("p1")
        self.assertIsNot(third, kept)
        self.assertTrue(spin_until(lambda: got3, 60))
        self.assertIsNot(got3[0][0], first)
        # on an aligned folder, a local change is enough
        self.align()
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))
        _kept, got4 = self.preview_of("p1", Mode.SYNC)
        self.assertTrue(spin_until(lambda: got4, 60))
        self.assertTrue(got4[0][0].plan.empty)
        (self.local / "new.txt").write_text("x")
        self.assertTrue(spin_until(lambda: self.engine.runtime["p1"].preview is None
                                   or self.engine.runtime["p1"].preview.mode is not Mode.SYNC, 10))

    def test_cancel_stops_a_preview(self):
        kept, got = self.preview_of("p1")
        self.engine.cancel_preview("p1")
        self.assertEqual(got, [(None, "Stopped.")])
        self.assertIsNone(self.engine.runtime["p1"].preview)
        spin_until(lambda: False, 3)
        self.assertEqual(got, [(None, "Stopped.")])              # told once, nothing after
        self.assertIsNone(self.engine.previewing)
        self.assertIs(self.status(), Status.NEEDS_SETUP)

    def test_a_preview_whose_dialog_was_closed_tells_when_ready(self):
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append((kind, msg)))
        changed = []
        self.engine.connect("pair-changed", lambda _e, pid: changed.append(pid))
        kept, got = self.preview_of("p1")
        changed.clear()
        self.engine.forget_listener("p1", kept.listeners[0])
        self.assertEqual(changed, ["p1"])                       # the banner says it goes on
        self.assertTrue(spin_until(lambda: told, 60))
        self.assertEqual(told[0], ("preview-ready", "30 files to Koofr"))
        self.assertEqual(got, [])
        self.assertTrue(kept.fresh())
        self.assertFalse(kept.seen)                             # ready, not looked at yet
        _again, got2 = self.preview_of("p1")
        self.assertTrue(spin_until(lambda: got2, 5))
        self.assertTrue(kept.seen)

    def test_a_first_alignment_with_nothing_to_align_goes_on_its_own(self):
        engine_mod.PREVIEW_AHEAD_DELAY_S = 2
        # the two sides already match: the preview prepared ahead finds
        # nothing to do, so the alignment needs no word from the user
        local, remote = self.tmp / "same", self.remote_root / "same"
        local.mkdir()
        remote.mkdir()
        for i in range(5):
            (local / f"same-{i}.txt").write_text(str(i))
            shutil.copy2(local / f"same-{i}.txt", remote / f"same-{i}.txt")
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        pair = self.engine.add_pair(str(local), "/same")
        rt = self.engine.runtime[pair.id]
        self.assertIs(rt.status, Status.NEEDS_SETUP)
        self.assertTrue(spin_until(lambda: rt.status is Status.OK and pair.initialized, 60), rt.message)
        self.assertEqual(sorted(os.listdir(remote)), sorted(n for n in os.listdir(local) if not n.startswith(".")))
        self.assertNotIn("preview-ready", told)
        # a side with more than the other: the first alignment still waits for the user
        other = self.other_pair()
        rt2 = self.engine.runtime[other.id]
        self.assertTrue(spin_until(lambda: rt2.preview is not None and rt2.preview.fresh(), 60))
        spin_until(lambda: False, 2)
        self.assertIs(rt2.status, Status.NEEDS_SETUP)
        self.assertEqual(len(rt2.preview.outcome.plan.upload), 5)

    def test_a_folder_waiting_for_the_user_gets_its_preview_ahead(self):
        engine_mod.PREVIEW_AHEAD_DELAY_S = 1
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append(kind))
        other = self.other_pair()
        rt = self.engine.runtime[other.id]
        self.assertTrue(spin_until(lambda: rt.preview is not None and rt.preview.fresh(), 60))
        self.assertEqual((rt.preview.mode, rt.preview.reason), (Mode.RESYNC, "ahead"))
        self.assertEqual(len(rt.preview.outcome.plan.upload), 5)
        self.assertNotIn("preview-ready", told)                  # nobody asked: no notification
        # not on a metered connection
        self.engine.blocked_reason = lambda: "Paused on a metered connection"
        third = self.other_pair("third")
        spin_until(lambda: False, 3)
        self.assertIsNone(self.engine.runtime[third.id].preview)

    def test_the_space_on_koofr_is_read_and_a_full_account_is_told(self):
        # read at sign-in (here: the disk of the local "Koofr")
        self.assertTrue(spin_until(lambda: self.engine.space is not None, 20))
        self.assertGreater(self.engine.space.total, 0)
        self.assertFalse(self.engine.low_space)
        changed = []
        self.engine.connect("status-changed", lambda _e: changed.append(1))
        self.engine._space_read_done(Quota(total=1000, used=950, free=50))     # 5% free
        self.assertTrue(self.engine.low_space)
        self.assertEqual(changed, [1])
        self.engine._space_read_done(Quota(total=1000, used=950, free=50))     # the same: no news
        self.assertEqual(changed, [1])
        self.engine._space_read_done(Quota(total=1000, used=850, free=150))    # 15%: fine
        self.assertFalse(self.engine.low_space)
        # signed out: no space known, nothing read
        self.engine.set_credentials(None)
        self.assertIsNone(self.engine.space)
        self.engine.refresh_space(min_age=0)
        self.assertFalse(self.engine._space_busy)

    def test_koofr_desktop_on_the_same_folder_stops_every_run(self):
        self.align()
        home = self.tmp / "home"
        # Path.home() reads USERPROFILE on Windows, Koofr Desktop's folder is in APPDATA there
        for var, value in (("HOME", home), ("USERPROFILE", home), ("APPDATA", home / "AppData" / "Roaming")):
            saved = os.environ.get(var)
            os.environ[var] = str(value)
            self.addCleanup(lambda v=var, s=saved: os.environ.__setitem__(v, s) if s is not None
                            else os.environ.pop(v, None))
        koofr = preflight.koofr_desktop_dir()
        (koofr / "log").mkdir(parents=True)
        (koofr / "config.json").write_text(
            json.dumps({"users": {"u": {"syncs": [{"localPath": str(self.local), "remotePath": "/test"}]}}}))
        if _setup.WINDOWS:          # its processes and what starts at login tell, not its logs
            patcher = mock.patch.object(preflight, "koofr_desktop_active", return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        else:
            (koofr / "log" / "sync_x.log").write_text("running")      # written just now
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append((kind, msg)))
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.ERROR, 20))
        self.assertIn("Koofr Desktop", self.engine.runtime["p1"].message)
        self.engine._on_conditions()                              # the error stays until a run starts
        self.assertIs(self.status(), Status.ERROR)
        self.assertTrue(self.engine.runtime["p1"].retry_source)   # and it is tried again
        self.assertNotIn("new.txt", self.remote_files())
        self.engine.request_sync("p1", "poll")                 # the next attempt: same error, told once
        spin_until(lambda: False, 2)
        self.assertEqual([k for k, _m in told], ["error"])

    def test_never_runs_on_its_own_before_alignment(self):
        self.engine.sync_all("poll")
        spin_until(lambda: False, 2)
        self.assertEqual(self.remote_files(), [])
        self.assertIs(self.status(), Status.NEEDS_SETUP)
        self.engine.set_paused("p1", True)                # on hold before its first alignment
        self.assertIs(self.status(), Status.PAUSED)
        self.assertIs(self.engine.overall(), Status.PAUSED)      # every pair is paused
        other = Pair("p2", str(self.tmp), "/other", initialized=True)
        self.engine.cfg.pairs.append(other)
        self.engine.runtime["p2"] = PairRuntime(status=Status.OK)
        self.assertIs(self.engine.overall(), Status.OK)          # one on hold says nothing about the rest
        self.engine.cfg.pairs.remove(other)
        del self.engine.runtime["p2"]
        self.engine.set_paused("p1", False)
        self.assertIs(self.status(), Status.NEEDS_SETUP)

    def test_full_cycle(self):
        self.align()

        # a new local file: the monitor sees it, the debounce starts a run
        (self.local / "new.txt").write_text("hello")
        self.assertTrue(spin_until(lambda: "new.txt" in self.remote_files(), 30))
        self.assertTrue(spin_until(lambda: self.status() is Status.OK, 30))
        # the tray's recent files: this one, newest first, deletions never listed
        self.assertEqual([(f.path, f.direction) for f in self.engine.recent.items][:1], [("new.txt", "up")])

        # a mass delete stops and waits for the user
        for i in range(10):
            (self.local / f"f{i:02}.txt").unlink()
        self.assertTrue(spin_until(lambda: self.status() is Status.NEEDS_CONFIRMATION, 30))
        self.assertEqual(len(self.remote_files()), 31)
        self.engine.sync_all("poll")                      # automatic runs stay stopped
        spin_until(lambda: False, 2)
        self.assertEqual(len(self.remote_files()), 31)

        _kept, seen = self.preview_of("p1", Mode.SYNC, True)
        self.assertTrue(spin_until(lambda: seen, 60))
        results = []
        self.assertTrue(self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out)))
        self.assertTrue(spin_until(lambda: results, 30))
        self.assertIs(results[0].result, Result.OK)
        self.assertEqual(len(self.remote_files()), 21)
        self.assertIs(self.status(), Status.OK)
        self.assertNotIn("f00.txt", [f.path for f in self.engine.recent.items])

        # paused: changes wait
        self.engine.set_paused("p1", True)
        self.assertIs(self.status(), Status.PAUSED)
        (self.local / "later.txt").write_text("x")
        self.engine.request_sync("p1", "poll")
        spin_until(lambda: False, 2)
        self.assertNotIn("later.txt", self.remote_files())
        self.engine.set_paused("p1", False)
        self.assertTrue(spin_until(lambda: "later.txt" in self.remote_files(), 30))


class HelpersTest(unittest.TestCase):
    def test_what_a_shorter_retention_would_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "c", Path(tmp) / "s", Path(tmp) / "d")
            now = time.mktime(time.strptime("20261017-120000", "%Y%m%d-%H%M%S"))
            for stamp, size in (("20261001-120000", 10), ("20261010-120000", 20), ("20261016-120000", 30)):
                run = paths.backup_dir("p1") / stamp
                run.mkdir(parents=True)
                (run / "f").write_bytes(b"x" * size)
            (paths.backup_dir("p1") / "not-ours").mkdir()
            self.assertEqual(jobs.backups_older_than(paths, "p1", 30, now), (0, 0))
            self.assertEqual(jobs.backups_older_than(paths, "p1", 10, now), (1, 10))
            self.assertEqual(jobs.backups_older_than(paths, "p1", 3, now), (2, 30))
            self.assertEqual(jobs.backups_older_than(paths, "none", 3, now), (0, 0))

    def test_conflict_words_follow_the_chosen_language(self):
        from spola.jobs import conflict_words
        if not (Path(__file__).resolve().parent.parent / "locale/it/LC_MESSAGES/spola.mo").exists():
            self.skipTest("run tools/compile_po.py first")
        self.assertEqual(conflict_words("it"), ("conflitto-locale", "conflitto-koofr"))
        self.assertEqual(conflict_words("en"), ("conflict-local", "conflict-koofr"))

    def test_resume_detection(self):
        self.assertFalse(slept(60.2, 60.0))
        self.assertFalse(slept(3600, 3600))          # a long uptime is not a sleep
        self.assertTrue(slept(60 + 1800, 60))        # half an hour suspended

    def test_conflict_names(self):
        pattern = conflict_name_pattern()
        # the Italian names too, whatever the language of this process
        for name in ("doc.conflict1.odt", "a/doc.conflict-local-2026-10-10-1.odt",
                     "a/doc.conflitto-locale-2026-10-10-1.odt", "doc.conflitto-koofr-2026-10-10-2.txt",
                     "doc.conflict-koofr-2026-10-10-12", "x.conflict2"):
            self.assertTrue(pattern.search(name), name)
        for name in ("doc.odt", "report-2026-10-10.pdf", "conflict.txt", "a.conflict-other-2026-10-10-1.odt"):
            self.assertFalse(pattern.search(name), name)


if __name__ == "__main__":
    unittest.main()


class ChangedHereTest(unittest.TestCase):
    def test_files_whose_size_or_date_differ_from_the_last_listing(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "sub").mkdir()
        stamp = "2026-10-15T09:00:59.704779200+0000"
        known = listings.modtime_ns(stamp)
        for name, text, mtime_ns in (("same.txt", "ab", known), ("bigger.txt", "abc", known),
                                     ("later.txt", "ab", known + 2 * 10**9), ("close.txt", "ab", known + 500_000),
                                     ("sub/deep.txt", "ab", known - 3 * 10**9)):
            (root / name).write_text(text)
            os.utime(root / name, ns=(mtime_ns, mtime_ns))
        previous = [listings.Entry("sub", True, -1, "-", stamp)] + [
            listings.Entry(name, False, 2, "-", stamp)
            for name in ("same.txt", "bigger.txt", "later.txt", "close.txt", "gone.txt", "sub/deep.txt")]
        self.assertEqual(jobs.changed_here(str(root), previous), ["bigger.txt", "later.txt", "sub/deep.txt"])
        self.assertEqual(jobs.changed_here(str(root), []), [])
