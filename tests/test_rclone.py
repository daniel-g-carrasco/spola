#!/usr/bin/env python3
"""rclone command lines and log parsing, on logs recorded from rclone 1.74.3."""

import unittest
from pathlib import Path

from _setup import fixture_lines

from spola.rclone import (
    FORBIDDEN_FLAGS, BisyncRequest, Kind, Mode, Result, bisync_args, clean_env,
    effective_max_delete, koofr_env, parse_line, remote_spec, summarize,
)


def request(**kw):
    base = dict(path1="/home/u/data/work", path2="koofr:data/work", workdir=Path("/s/wd"),
                rclone_config=Path("/s/no.conf"), filters_file=Path("/s/f.txt"),
                backup_dir1=Path("/d/bk"), max_delete=10)
    base.update(kw)
    return BisyncRequest(**base)


class ArgsTest(unittest.TestCase):
    def test_safe_defaults(self):
        args = bisync_args("rclone", request())
        self.assertEqual(args[:4], ["rclone", "bisync", "/home/u/data/work/", "koofr:data/work"])
        self.assertEqual(args[args.index("--max-delete") + 1], "10")
        for flag in ("--resilient", "--recover", "--use-json-log", "--suffix-keep-extension",
                     "--skip-links", "--slow-hash-sync-only"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--conflict-resolve") + 1], "none")
        self.assertEqual(args[args.index("--config") + 1], "/s/no.conf")
        for flag in ("--resync", "--force", "--dry-run", "--check-sync"):
            self.assertNotIn(flag, args)
        self.assertFalse(set(args) & FORBIDDEN_FLAGS)

    def test_resync_force_dryrun_only_when_asked(self):
        args = bisync_args("rclone", request(mode=Mode.RESYNC, dry_run=True))
        self.assertIn("--resync", args)
        self.assertEqual(args[args.index("--resync-mode") + 1], "newer")
        self.assertIn("--dry-run", args)
        self.assertNotIn("--force", args)
        self.assertIn("--force", bisync_args("rclone", request(force=True)))
        args = bisync_args("rclone", request(mode=Mode.CHECK))
        self.assertEqual(args[args.index("--check-sync") + 1], "only")

    def test_forbidden_flags_are_refused(self):
        for flag in ("--delete-excluded", "--inplace", "--copy-links", "-L"):
            with self.assertRaises(ValueError, msg=flag):
                bisync_args("rclone", request(extra_test_flags=[flag]))

    def test_password_never_in_argv_and_env_is_clean(self):
        env = koofr_env("me@example.com", "OBSCURED", base={"HOME": "/h", "RCLONE_CONFIG_PASS": "x",
                                                            "RCLONE_CONFIG_KOOFR_ENDPOINT": "evil"})
        self.assertEqual(env["RCLONE_CONFIG_KOOFR_PASSWORD"], "OBSCURED")
        self.assertEqual(env["RCLONE_CONFIG_KOOFR_TYPE"], "koofr")
        self.assertNotIn("RCLONE_CONFIG_PASS", env)
        self.assertNotIn("RCLONE_CONFIG_KOOFR_ENDPOINT", env)
        self.assertEqual(env["HOME"], "/h")
        self.assertNotIn("OBSCURED", " ".join(bisync_args("rclone", request())))
        self.assertEqual(clean_env({"RCLONE_X": "1", "A": "2"}), {"A": "2"})

    def test_remote_spec(self):
        self.assertEqual(remote_spec("/data/work"), "koofr:data/work")
        self.assertEqual(remote_spec("/"), "koofr:")

    def test_max_delete(self):
        self.assertEqual(effective_max_delete(10, None), 10)
        self.assertEqual(effective_max_delete(10, 60000), 10)
        self.assertEqual(effective_max_delete(10, 20), 25)     # 5 of 20 files
        self.assertEqual(effective_max_delete(10, 3), 100)     # tiny folder
        self.assertEqual(effective_max_delete(0, None), 1)     # never "no limit"
        self.assertEqual(effective_max_delete(500, None), 100)


def events(name):
    return [e for e in (parse_line(line) for line in fixture_lines(name)) if e]


class ParseTest(unittest.TestCase):
    def test_max_delete_abort(self):
        out = summarize(1, events("max-delete-abort.jsonl"), "/ROOT/a/", "/ROOT/b/")
        self.assertIs(out.result, Result.NEEDS_CONFIRMATION)
        self.assertIn("too many deletes", out.message)

    def test_sync_with_conflict(self):
        evs = events("sync-with-conflict.jsonl")
        out = summarize(0, evs, "/ROOT/a/", "/ROOT/b/")
        self.assertIs(out.result, Result.OK)
        self.assertEqual(out.plan.conflicts, ["f1.txt"])
        self.assertEqual(out.plan.upload, ["f1.txt.conflict1", "new.txt"])
        self.assertEqual(out.plan.download, ["f1.txt.conflict2"])
        self.assertEqual(out.plan.delete_local, ["f3.txt"])
        self.assertEqual(out.plan.delete_remote, [])
        self.assertEqual(out.stats["transfers"], 5)
        deltas = [(e.side, e.action, e.path) for e in evs if e.kind is Kind.DELTA]
        self.assertIn(("Path2", "File was deleted", "f3.txt"), deltas)

    def test_sync_dry_run_plan(self):
        out = summarize(0, events("sync-dry-run.jsonl"), "/ROOT/a/", "/ROOT/b/")
        # the preview already names the conflict copies as they will be
        self.assertEqual(out.plan.upload, ["f1.txt.conflict1", "new.txt"])
        self.assertEqual(out.plan.download, ["f1.txt.conflict2"])
        self.assertEqual(out.plan.delete_local, ["f3.txt"])
        self.assertEqual(out.plan.conflicts, ["f1.txt"])

    def test_resync_preview_and_real(self):
        for name in ("resync-dry-run.jsonl", "resync.jsonl"):
            out = summarize(0, events(name), "/ROOT/a/", "/ROOT/b/")
            self.assertIs(out.result, Result.OK, name)
            self.assertEqual(sorted(out.plan.download), ["differ.txt", "only-remote.txt"], name)
            self.assertEqual(out.plan.upload, ["only-local.txt"], name)
            self.assertEqual(out.plan.replace_local, ["differ.txt"], name)
            self.assertEqual(out.plan.delete_local + out.plan.delete_remote, [], name)

    def test_real_koofr_paths(self):
        # recorded on Koofr: queue lines carry "koofr{hash}:" and "local{hash}:" prefixes
        for name in ("koofr-sync.jsonl", "koofr-sync-dry-run.jsonl"):
            out = summarize(0, events(name), "/home/user/scratch/spola-test/", "koofr:spola-test")
            self.assertIs(out.result, Result.OK, name)
            self.assertEqual(sorted(out.plan.upload), ["appunti.txt", "nuovo-locale.txt"], name)
            self.assertEqual(sorted(out.plan.download), ["dal-cloud.txt", "nuovo-cloud.txt"], name)
            self.assertEqual(out.plan.delete_remote, ["Progetti/nota-01.txt"], name)
        out = summarize(0, events("koofr-resync.jsonl"), "/home/user/scratch/spola-test/", "koofr:spola-test")
        self.assertEqual(len(out.plan.upload), 23)
        self.assertEqual(sorted(out.plan.download), ["comune.txt", "dal-cloud.txt"])
        self.assertEqual(out.plan.replace_local, ["comune.txt"])

    def test_root_remote(self):
        out = summarize(0, [parse_line('{"level":"info","msg":"- Path1 Queue copy to Path2 - koofr{ab12}:a/b.txt"}')],
                        "/h/x/", "koofr:")
        self.assertEqual(out.plan.upload, ["a/b.txt"])

    def test_critical(self):
        out = summarize(7, events("critical-no-listings.jsonl"), "/ROOT/a/", "/ROOT/b/")
        self.assertIs(out.result, Result.NEEDS_REPAIR)

    def test_exit_codes(self):
        self.assertIs(summarize(1, [], "a", "b").result, Result.RETRY)
        self.assertIs(summarize(2, [], "a", "b").result, Result.FAILED)
        self.assertIs(summarize(7, [], "a", "b").result, Result.NEEDS_REPAIR)
        self.assertIs(summarize(1, [], "a", "b", cancelled=True).result, Result.CANCELLED)

    def test_plain_and_garbage_lines(self):
        self.assertIs(parse_line("2026/10/10 NOTICE: something").kind, Kind.OTHER)
        self.assertIsNone(parse_line("   "))
        self.assertIsNone(parse_line("[1, 2]"))


if __name__ == "__main__":
    unittest.main()
