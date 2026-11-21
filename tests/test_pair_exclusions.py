#!/usr/bin/env python3
"""What a pair's own filters leave out is left out for every part of
Spola, as it is for rclone; and the list of the names Koofr refuses follows
the disk. With the real rclone and a local folder as the "Koofr" side, in
temporary folders only.

The case of 2026-11-21: a pair with "- /music/**" added by hand to its
filters, and in music/ a file with an emoji in its name. rclone never
looked at it; Spola listed it among the files Koofr refuses, the pair said
"1 file left out" and wanted a look, and when the file was renamed nothing
looked at it again, because the folder monitor, alone, did honour the
filter."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from _setup import rclone_binary

from gi.repository import GLib

from spola.app import engine as engine_mod
from spola.app.account import Credentials
from spola.app.config import Config, Pair
from spola.app.engine import Engine, Status
from spola.app.preflight import root_identity
from spola.app.rclone import DEFAULT_FILTERS, Mode
from spola.app.storage import Paths
from spola.core.states import State

RCLONE = rclone_binary()
EMOJI = "08 - \U0001F534.flac"


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
class PairExclusionsTest(unittest.TestCase):
    FILTERS = DEFAULT_FILTERS + "# kept on this computer only\n- /esclusa/**\n"

    def setUp(self):
        for name, value in (("DEBOUNCE_S", 1), ("PREVIEW_AHEAD_DELAY_S", 3600)):
            self.addCleanup(setattr, engine_mod, name, getattr(engine_mod, name))
            setattr(engine_mod, name, value)
        self.addCleanup(setattr, engine_mod.jobs, "SETTLE_S", engine_mod.jobs.SETTLE_S)
        engine_mod.jobs.SETTLE_S = 0
        self.tmp = Path(tempfile.mkdtemp(prefix="spola-exclusions-"))
        self.local = self.tmp / "local"
        self.remote_root = self.tmp / "remote"
        self.remote = self.remote_root / "test"
        (self.local / "synced").mkdir(parents=True)
        (self.local / "esclusa" / "album").mkdir(parents=True)
        self.remote.mkdir(parents=True)
        for i in range(5):
            (self.local / "synced" / f"f{i}.txt").write_text(str(i))
        os.environ["SPOLA_DEV_REMOTE"] = str(self.remote_root)
        os.environ["SPOLA_RCLONE"] = RCLONE
        self.paths = Paths(self.tmp / "config", self.tmp / "state", self.tmp / "data")
        dev, ino = root_identity(str(self.local))
        self.pair = Pair("p1", str(self.local), "/test", root_dev=dev, root_ino=ino)
        self.engine = None

    def start(self, filters: str | None = None, more=()):
        if filters is not None:
            path = self.paths.filters_file("p1")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(filters, encoding="utf-8")
        self.engine = Engine(self.paths, Config(user="dev", pairs=[self.pair, *more]))
        self.engine.cfg.pause_on_metered = False
        self.engine.cfg.pause_on_power_saver = False
        self.engine.set_credentials(Credentials("dev", ""))
        self.engine.start()
        self.rt = self.engine.runtime["p1"]
        self.runs = []
        self.engine.connect("run-finished", lambda _e, pid, reason: self.runs.append(reason))

    def tearDown(self):
        if self.engine is not None:
            done = []
            self.engine.shutdown(lambda: done.append(True))
            spin_until(lambda: done, 100)
        os.environ.pop("SPOLA_DEV_REMOTE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def align(self, pair_id="p1"):
        got = []
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime[pair_id]
        self.engine.preview(pair_id, Mode.RESYNC, False, lambda out, err: got.append((out, err)))
        self.assertTrue(spin_until(lambda: got))
        self.assertEqual(got[0][1], "")
        self.assertTrue(self.engine.confirm(pair_id, Mode.RESYNC, False))
        self.assertTrue(spin_until(lambda: pair.initialized and self.engine.active is None
                                   and not self.engine.queue and rt.status is not Status.SYNCING),
                        (rt.status, rt.message))

    def quiet(self, seconds=3.0):
        """Let the monitor, the debounce and a run that would follow happen."""
        spin_until(lambda: False, seconds)
        self.assertTrue(spin_until(lambda: self.engine.active is None and not self.engine.queue, 30))

    # -- 1. the pair's own filters ------------------------------------------------------

    def test_a_name_koofr_refuses_in_a_folder_left_out_is_nobodys_business(self):
        (self.local / "esclusa" / "album" / EMOJI).write_text("flac")
        self.start(self.FILTERS)
        self.align()
        self.assertFalse((self.remote / "esclusa").exists())           # rclone honours the filter
        self.assertEqual([r.path for r in self.rt.refused], [])
        self.assertIs(self.rt.status, Status.OK)
        self.assertFalse(self.paths.exclude_file("p1").exists())

    def test_a_change_in_a_folder_left_out_starts_no_run(self):
        self.start(self.FILTERS)
        self.align()
        self.quiet(2)
        self.runs.clear()
        (self.local / "esclusa" / "album" / "new.flac").write_text("new")
        (self.local / "esclusa" / "top.txt").write_text("top")
        os.rename(self.local / "esclusa" / "album", self.local / "esclusa" / "album 2")
        self.quiet(3)
        self.assertEqual(self.runs, [])
        self.assertIs(self.rt.status, Status.OK)

    def test_exclusions_changed_while_spola_runs_reach_the_monitor(self):
        # the monitor chose its folders at start: after a change of the
        # exclusions (the dialog, then the repair) it must choose again
        self.start()
        self.align()
        self.assertTrue(self.engine.set_filters("p1", self.FILTERS))
        self.assertIs(self.rt.status, Status.NEEDS_REPAIR)
        self.align()
        self.quiet(2)
        self.assertIs(self.rt.status, Status.OK, self.rt.message)
        self.runs.clear()
        (self.local / "esclusa" / "album" / "new.flac").write_text("new")
        self.quiet(3)
        self.assertEqual(self.runs, [])
        self.assertIs(self.rt.status, Status.OK)
        # and what is synced is still watched
        (self.local / "synced" / "more.txt").write_text("more")
        self.assertTrue(spin_until(lambda: (self.remote / "synced" / "more.txt").exists(), 30))

    def test_a_folder_left_out_deeper_than_the_root_is_not_watched_either(self):
        # "/a/b/**": the old matcher of the monitor read anchored rules one level deep only
        (self.local / "synced" / "cache" / "deep").mkdir(parents=True)
        self.start(DEFAULT_FILTERS + "- /synced/cache/**\n")
        self.align()
        self.quiet(2)
        self.runs.clear()
        (self.local / "synced" / "cache" / "deep" / "x.bin").write_text("x")
        self.quiet(3)
        self.assertEqual(self.runs, [])

    def test_the_file_managers_see_a_folder_left_out_as_left_out(self):
        (self.local / "esclusa" / "album" / "song.flac").write_text("flac")
        self.start(self.FILTERS)
        self.align()
        self.assertTrue(spin_until(lambda: "synced/f0.txt" in self.rt.states.listed, 30))
        states = self.rt.states
        self.assertIs(states.state_of("synced/f0.txt"), State.SYNCED)
        self.assertIs(states.state_of("esclusa", True), State.EXCLUDED)
        self.assertIs(states.state_of("esclusa/album", True), State.EXCLUDED)
        self.assertIs(states.state_of("esclusa/album/song.flac"), State.EXCLUDED)     # was "pending" for ever
        self.assertIs(states.state_of("", True), State.SYNCED)

    # -- 2. the names Koofr refuses follow the disk -------------------------------------

    def test_a_refused_file_renamed_as_rsync_does_leaves_the_list_and_is_synced(self):
        self.start(self.FILTERS)
        self.align()
        (self.local / "synced" / EMOJI).write_text("flac")
        self.assertTrue(spin_until(lambda: self.rt.status is Status.SKIPPED, 30), (self.rt.status, self.rt.message))
        self.assertEqual([r.path for r in self.rt.refused], [f"synced/{EMOJI}"])
        # rsync: the old name deleted, a new file next to it
        (self.local / "synced" / EMOJI).unlink()
        (self.local / "synced" / "08 - _.flac").write_text("flac")
        self.assertTrue(spin_until(lambda: (self.remote / "synced" / "08 - _.flac").exists()
                                   and self.rt.status is Status.OK, 30), (self.rt.status, self.rt.message))
        self.assertEqual(self.rt.refused, [])

    def test_a_refused_file_gone_leaves_the_list_before_the_next_run(self):
        self.start(self.FILTERS)
        self.align()
        (self.local / "synced" / EMOJI).write_text("flac")
        self.assertTrue(spin_until(lambda: self.rt.status is Status.SKIPPED, 30))
        self.quiet(2)
        engine_mod.DEBOUNCE_S = 3600            # no run comes to the rescue
        (self.local / "synced" / EMOJI).unlink()
        self.assertTrue(spin_until(lambda: not self.rt.refused, 10), self.rt.refused)
        self.assertIsNot(self.rt.status, Status.SKIPPED)
        self.assertIs(self.rt.states.state_of(f"synced/{EMOJI}"), State.PENDING)        # no longer "left out"

    def test_looking_at_the_list_checks_it_against_the_disk(self):
        # a file gone while nobody watched (a folder past the monitor's budget,
        # Spola not running): opening the list finds out, and a run follows
        self.start(self.FILTERS)
        self.align()
        (self.local / "synced" / EMOJI).write_text("flac")
        self.assertTrue(spin_until(lambda: self.rt.status is Status.SKIPPED, 30))
        self.rt.watcher.stop()
        (self.local / "synced" / EMOJI).unlink()
        (self.local / "synced" / "renamed.flac").write_text("flac")
        self.quiet(2)
        self.assertEqual(len(self.rt.refused), 1)                  # nobody saw it
        self.runs.clear()
        self.assertEqual(self.engine.recheck_refused("p1"), 1)
        self.assertEqual(self.rt.refused, [])
        self.assertTrue(spin_until(lambda: (self.remote / "synced" / "renamed.flac").exists()
                                   and self.rt.status is Status.OK, 30), (self.rt.status, self.rt.message))
        # nothing gone: nothing done
        self.runs.clear()
        self.assertEqual(self.engine.recheck_refused("p1"), 0)
        self.quiet(1)
        self.assertEqual(self.runs, [])

    def test_a_refused_file_in_a_folder_then_left_out_leaves_the_list(self):
        self.start()
        (self.local / "esclusa" / "album" / EMOJI).write_text("flac")
        self.align()
        self.assertEqual([r.path for r in self.rt.refused], [f"esclusa/album/{EMOJI}"])
        self.assertTrue(self.engine.set_filters("p1", self.FILTERS))
        self.align()
        self.quiet(2)
        self.assertEqual(self.rt.refused, [])
        self.assertIs(self.rt.status, Status.OK, self.rt.message)

    # -- 3. Sync Now answers --------------------------------------------------------------

    def test_sync_now_in_the_skipped_state_runs_and_says_so(self):
        self.start(self.FILTERS)
        self.align()
        (self.local / "synced" / EMOJI).write_text("flac")
        self.assertTrue(spin_until(lambda: self.rt.status is Status.SKIPPED, 30))
        self.quiet(1)
        self.runs.clear()
        self.assertEqual(self.engine.request_sync("p1", "manual"), "")
        self.assertTrue(spin_until(lambda: "manual" in self.runs, 30))
        self.assertIs(self.rt.status, Status.SKIPPED)

    def test_sync_now_over_a_run_already_on_its_way_is_answered_too(self):
        self.start(self.FILTERS)
        self.align()
        self.quiet(1)
        self.runs.clear()
        self.engine.request_sync("p1", "poll")              # queued by itself
        self.assertEqual(self.engine.request_sync("p1", "manual"), "")
        self.assertTrue(spin_until(lambda: self.runs, 30))
        self.assertEqual(self.runs, ["manual"])             # one run, and the user who asked hears of it

    def test_sync_now_that_cannot_run_says_why(self):
        self.start(self.FILTERS)
        self.align()
        self.assertTrue(self.engine.set_filters("p1", DEFAULT_FILTERS))
        self.assertIs(self.rt.status, Status.NEEDS_REPAIR)
        self.assertIn("repair", self.engine.request_sync("p1", "manual"))
        self.assertEqual(self.engine.request_sync("p1", "poll"), "")       # only the user is answered

    def test_sync_now_behind_another_folder_says_what_it_waits_for(self):
        """One folder syncs at a time: Sync Now on a folder whose turn has not
        come (another one is uploading) used to do nothing one could see."""
        other_local = self.tmp / "other"
        other_local.mkdir()
        (self.remote_root / "other").mkdir()
        dev, ino = root_identity(str(other_local))
        self.start(self.FILTERS, more=[Pair("p2", str(other_local), "/other", root_dev=dev, root_ino=ino)])
        self.align()
        self.align("p2")
        self.quiet(1)
        self.runs.clear()
        os.environ["SPOLA_DEV_BWLIMIT"] = "40k"                 # a slow upload: some ten seconds
        self.addCleanup(os.environ.pop, "SPOLA_DEV_BWLIMIT", None)
        (other_local / "large.bin").write_bytes(os.urandom(400_000))
        other = self.engine.runtime["p2"]
        self.assertTrue(spin_until(lambda: other.run is not None and other.run.running, 30))
        self.assertEqual(self.engine.waits_behind("p1"), "")    # nothing of its own to wait for yet
        (self.local / "synced" / "new.txt").write_text("new")
        told = []
        self.engine.connect("pair-changed", lambda _e, pid: told.append((pid, self.engine.waits_behind(pid))))
        answer = self.engine.sync_now("p1")
        self.assertIn(("p1", "other"), told)                    # the views are told, to say what it waits for
        self.assertEqual(answer, "“local” syncs as soon as “other” is done.")
        self.assertEqual(self.engine.waits_behind("p1"), "other")
        self.assertTrue(spin_until(lambda: "manual" in self.runs, 60), self.runs)
        self.assertTrue((self.remote / "synced" / "new.txt").exists())
        self.assertEqual(self.engine.waits_behind("p1"), "")

    def test_sync_all_that_starts_nothing_says_why(self):
        self.start(self.FILTERS)
        self.align()
        self.quiet(1)
        self.runs.clear()
        self.assertEqual(self.engine.sync_all("manual"), "")        # a run follows, and answers when it ends
        self.assertTrue(spin_until(lambda: self.runs == ["manual"], 30))
        self.quiet(1)
        self.engine.set_paused("p1", True)
        self.assertIn("is paused", self.engine.sync_all("manual"))
        self.assertEqual(self.engine.sync_all("poll"), "")          # nobody asked: nothing to answer


if __name__ == "__main__":
    unittest.main()
