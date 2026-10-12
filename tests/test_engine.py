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
from spola.account import Credentials
from spola.config import Config, Pair
from spola.engine import Engine, PairRuntime, Status, conflict_name_pattern, slept
from spola.paths import Paths
from spola.preflight import root_identity
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
