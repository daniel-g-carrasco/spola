#!/usr/bin/env python3
"""The scheduler end to end, with the real rclone and a local folder as the
"Koofr" side (SPOLA_DEV_REMOTE): first alignment, a local change picked up by
the monitor, a mass delete that stops and waits, pause, shutdown."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from _setup import rclone_binary

from gi.repository import GLib

from spola import engine as engine_mod
from spola import koofr_api
from spola.account import Credentials, Quota
from spola.config import Config, Pair
from spola.conflicts import decide
from spola.engine import Engine, PairRuntime, Status, conflict_name_pattern, slept
from spola.paths import Paths
from spola.preflight import rename_plan, root_identity
from spola import jobs
from spola.rclone import Mode, Result

RCLONE = rclone_binary()


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

    def preview_of(self, pair_id, mode=Mode.RESYNC, force=False, **kw):
        got = []
        kept = self.engine.preview(pair_id, mode, force, lambda out, err: got.append((out, err)), **kw)
        return kept, got

    def logs(self, pair_id):
        folder = self.engine.paths.log_dir(pair_id)
        return len(list(folder.glob("*.jsonl"))) if folder.exists() else 0

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
        kept, got = self.preview_of("p1")
        self.engine.forget_listener("p1", kept.listeners[0])
        self.assertTrue(spin_until(lambda: told, 60))
        self.assertEqual(told[0], ("preview-ready", "30 files to Koofr"))
        self.assertEqual(got, [])
        self.assertTrue(kept.fresh())

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
        (home / ".koofr" / "log").mkdir(parents=True)
        (home / ".koofr" / "config.json").write_text(
            '{"users": {"u": {"syncs": [{"localPath": "%s", "remotePath": "/test"}]}}}' % self.local)
        (home / ".koofr" / "log" / "sync_x.log").write_text("running")      # written just now
        saved = os.environ["HOME"]
        os.environ["HOME"] = str(home)
        self.addCleanup(os.environ.__setitem__, "HOME", saved)
        told = []
        self.engine.connect("attention", lambda _e, pid, kind, msg: told.append((kind, msg)))
        (self.local / "new.txt").write_text("x")
        self.engine.request_sync("p1", "manual")
        self.assertTrue(spin_until(lambda: self.status() is Status.ERROR, 20))
        self.assertIn("Koofr Desktop", self.engine.runtime["p1"].message)
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

        results = []
        self.engine.confirm("p1", Mode.SYNC, True, lambda out, err: results.append(out))
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
