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
from spola.engine import Engine, Status, conflict_name_pattern, slept
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

    def test_never_runs_on_its_own_before_alignment(self):
        self.engine.sync_all("poll")
        spin_until(lambda: False, 2)
        self.assertEqual(self.remote_files(), [])
        self.assertIs(self.status(), Status.NEEDS_SETUP)

    def test_full_cycle(self):
        self.align()

        # a new local file: the monitor sees it, the debounce starts a run
        (self.local / "new.txt").write_text("hello")
        self.assertTrue(spin_until(lambda: "new.txt" in self.remote_files(), 30))
        self.assertTrue(spin_until(lambda: self.status() is Status.OK, 30))

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
