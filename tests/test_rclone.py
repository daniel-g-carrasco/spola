#!/usr/bin/env python3
"""rclone command lines and log parsing, on logs recorded from rclone 1.74.3."""

import unittest
from pathlib import Path

from _setup import fixture_lines

from spola.rclone import (
    DEFAULT_EXCLUDES, DEFAULT_FILTERS, FORBIDDEN_FLAGS, BisyncRequest, Event, Kind, Mode, Plan, Result,
    bisync_args, clean_env, conflict_suffix, default_excluded, describe, effective_max_delete, fold_case_renames,
    koofr_env, parse_line, relative_path, remote_spec, summarize,
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
        self.assertIn("--track-renames", args)
        self.assertFalse(set(args) & FORBIDDEN_FLAGS)

    def test_times_within_a_millisecond_are_the_same(self):
        # Koofr keeps times to the millisecond: in every mode, or a file moved
        # here would look changed and go up again at the next run
        for mode in Mode:
            args = bisync_args("rclone", request(mode=mode))
            self.assertEqual(args[args.index("--modify-window") + 1], "1ms", mode)

    def test_resync_force_dryrun_only_when_asked(self):
        args = bisync_args("rclone", request(mode=Mode.RESYNC, dry_run=True))
        self.assertIn("--resync", args)
        self.assertEqual(args[args.index("--resync-mode") + 1], "newer")
        self.assertIn("--dry-run", args)
        self.assertNotIn("--force", args)
        self.assertIn("--force", bisync_args("rclone", request(force=True)))
        self.assertNotIn("--track-renames", args)
        args = bisync_args("rclone", request(mode=Mode.CHECK))
        self.assertEqual(args[args.index("--check-sync") + 1], "only")
        self.assertNotIn("--track-renames", args)

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

    def test_renames_recorded_on_koofr(self):
        # recorded on Koofr: two files renamed here (one into a new folder), a
        # new file, a deleted one and a conflict, whose two copies bisync renames
        # with the same "Moved ... to" line as a rename; then a rename on Koofr.
        # The preview and the run say the same.
        roots = ("/home/user/renames/", "koofr:renames")
        for name in ("koofr-renames-dry-run.jsonl", "koofr-renames.jsonl"):
            plan = summarize(0, events(name), *roots).plan
            self.assertEqual(sorted(plan.moved_remote),
                             [("IMG_00.jpg", "2026/holiday-00.jpg"), ("IMG_01.jpg", "renamed-01.jpg")], name)
            self.assertEqual(plan.moved_local, [], name)
            # what a move replaces is neither an upload nor a delete; the rest is
            self.assertEqual(sorted(plan.upload), ["2026", "IMG_05.conflict-local-2026-10-13-1.jpg", "new.txt"], name)
            self.assertEqual(plan.download, ["IMG_05.conflict-koofr-2026-10-13-1.jpg"], name)
            self.assertEqual(plan.delete_remote, ["old.txt"], name)
            self.assertEqual(plan.conflicts, ["IMG_05.jpg"], name)
            self.assertIn("2 moved", describe(plan), name)
        for name in ("koofr-renamed-there-dry-run.jsonl", "koofr-renamed-there.jsonl"):
            plan = summarize(0, events(name), *roots).plan
            self.assertEqual(plan.moved_local, [("IMG_02.jpg", "from-windows-02.jpg")], name)
            self.assertEqual(plan.upload + plan.download + plan.delete_local + plan.delete_remote, [], name)
            self.assertFalse(plan.empty, name)

    def test_a_rename_by_case_alone_is_one_move(self):
        # recorded on Koofr: "Screencasts" renamed "screencasts" here, the
        # files of "00. Screen" moved into it, "00-readme.md" deleted. The
        # preview plans the renamed folder as deleted and uploaded again (Koofr
        # cannot tell the two names apart); the run, with the folder renamed
        # on Koofr first, finds both sides the same. Both say one move.
        from spola.preflight import CaseRename
        roots = ("/home/user/scratch/spola-case-probe/local/", "koofr:case-rename")
        renames = [CaseRename("Screencasts", "screencasts", "Screencasts")]
        moves = [("00. Screen/screen-0.mp4", "screencasts/screen-0.mp4"),
                 ("00. Screen/screen-1.mp4", "screencasts/screen-1.mp4"),
                 ("00. Screen/screen-2.mp4", "screencasts/screen-2.mp4")]
        for name in ("koofr-case-rename-dry-run.jsonl", "koofr-case-rename.jsonl"):
            plan = summarize(0, events(name), *roots).plan
            fold_case_renames(plan, renames)
            self.assertEqual(plan.moved_remote[0], ("Screencasts", "screencasts"), name)
            self.assertEqual(sorted(plan.moved_remote[1:]), moves, name)
            self.assertEqual(plan.upload, [], name)
            self.assertEqual(plan.delete_remote, ["00-readme.md", "00. Screen"], name)
            self.assertEqual(plan.conflicts, [], name)
        # without the rename, the preview would delete the folder on Koofr
        plan = summarize(0, events("koofr-case-rename-dry-run.jsonl"), *roots).plan
        self.assertIn("screencasts/cast-0.mp4", plan.delete_remote)

    def test_windows_paths(self):
        # recorded on Windows: rclone writes "local{HK82T}://?/D:/..." for a
        # local path; the root may come with backslashes, in another case
        base = "D:/a/_temp/msys64/tmp/spola-win-2zruds14"
        for path1 in (f"{base}/local/", base.replace("/", "\\") + "\\local\\", "d:/A/_temp/msys64/tmp/spola-win-2zruds14/local"):
            for name in ("windows-sync-dry-run.jsonl", "windows-sync.jsonl"):
                plan = summarize(0, events(name), path1, f"{base}/remote").plan
                self.assertEqual(sorted(plan.upload), ["f00.txt", "new.txt"], (path1, name))
                self.assertEqual(plan.download, ["from-koofr.txt"], (path1, name))
                self.assertEqual(plan.delete_remote, ["f01.txt"], (path1, name))
                self.assertEqual(plan.moved_remote, [("sub/photo.jpg", "sub/renamed.jpg")], (path1, name))
        self.assertEqual(relative_path("local{HK82T}://?/C:/Users/me/Koofr/a/b.txt", ["C:\\Users\\me\\Koofr/"]), "a/b.txt")
        self.assertEqual(relative_path("koofr{kFw_7}:data/work/x.txt", ["C:\\Users\\me\\work/", "koofr:data/work"]), "x.txt")
        # on Linux a backslash is part of a name
        self.assertEqual(relative_path("/home/u/a\\b.txt", ["/home/u/"]), "a\\b.txt")

    def test_a_preview_says_how_much_goes_each_way(self):
        out = summarize(0, events("resync-dry-run.jsonl"), "/ROOT/a/", "/ROOT/b/")
        self.assertEqual((out.plan.upload_bytes, out.plan.download_bytes), (6, 20))
        out = summarize(0, events("koofr-renames-dry-run.jsonl"), "/home/user/renames/", "koofr:renames")
        self.assertEqual(out.plan.upload_bytes, 8)               # new.txt; the moves copy nothing
        # a real run does not say
        out = summarize(0, events("resync.jsonl"), "/ROOT/a/", "/ROOT/b/")
        self.assertEqual((out.plan.upload_bytes, out.plan.download_bytes), (0, 0))

    def test_a_move_without_its_confirmation_is_not_a_rename(self):
        evs = [parse_line(text) for text in (
            '{"level":"info","msg":"- Path1 Do queued copies to - Path2"}',
            '{"level":"info","msg":"Moved (server-side) to: a.conflict-local-2026-10-13-1.txt","object":"a.txt"}',
            '{"level":"info","msg":"Moved (server-side) to: b.txt","object":"c.txt"}',
            '{"level":"info","msg":"Renamed from \\"x.txt\\"","object":"y.txt"}')]
        self.assertEqual([e.kind for e in evs[1:]], [Kind.MOVED, Kind.MOVED, Kind.RENAMED])
        self.assertEqual((evs[1].path, evs[1].target), ("a.txt", "a.conflict-local-2026-10-13-1.txt"))
        self.assertEqual(summarize(0, evs, "/h/x/", "koofr:x").plan.moved_remote, [])

    def test_a_warning_is_a_conflict_only_when_the_copies_are_renamed(self):
        # "New or changed in both paths", then: renamed copies (a conflict),
        # "Files are equal" (the same file on both sides), nothing (a folder
        # made on both sides). A dry run says "skipped rename" instead.
        def plan(lines, path2="koofr:x"):
            return summarize(0, [parse_line(text) for text in lines], "/h/x/", path2).plan
        warn = '{"level":"notice","msg":"- WARNING           New or changed in both paths                - %s"}'
        real = plan([warn % "a.txt",
                     '{"level":"notice","msg":"- Path1 Renaming Path1 copy - local{HK82T}:/h/x/a.conflict-local-2026-10-13-1.txt"}',
                     warn % "b.jpg", '{"level":"info","msg":"Files are equal! Skipping: b.jpg"}',
                     warn % "2021"])
        self.assertEqual(real.conflicts, ["a.txt"])
        dry = plan([warn % "a.txt",
                    '{"level":"notice","msg":"Skipped rename as --dry-run is set","skipped":"rename","object":"a.txt"}',
                    warn % "b.jpg", '{"level":"info","msg":"Files are equal! Skipping: b.jpg"}'])
        self.assertEqual(dry.conflicts, ["a.txt"])

    def test_remote_hashes_with_underscore_and_dash(self):
        # URL-safe base64: koofr{kFw_7}, koofr{a-b9Z}
        roots = ["/home/u/camera-roll/", "koofr:data/media/photos/camera-roll"]
        self.assertEqual(relative_path("koofr{kFw_7}:data/media/photos/camera-roll/2021/a.jpg", roots), "2021/a.jpg")
        self.assertEqual(relative_path("koofr{a-b9Z}:data/media/photos/camera-roll/b.jpg", roots), "b.jpg")
        self.assertEqual(relative_path("local{HK82T}:/home/u/camera-roll/c.jpg", roots), "c.jpg")

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


class DefaultsTest(unittest.TestCase):
    def test_default_excluded_mirrors_the_filters(self):
        for rel in ("a/b/node_modules/x/y.js", "node_modules/", "SyncTrash/a/b", ".cache/x", ".cache/",
                    "~$doc.docx", "sub/.~lock.a.odt#", "x/Thumbs.db", "a.part", ".Trash-1000/f",
                    "d/desktop.ini", "x.syncState"):
            self.assertTrue(default_excluded(rel), rel)
        for rel in ("doc.txt", "sub/.cache/x", "sub/.cache/", "node_modules.txt", "my node_modules/x",
                    "Thumbs.db.bak", "a/SyncTrash.txt"):
            self.assertFalse(default_excluded(rel), rel)
        for pattern in DEFAULT_EXCLUDES:
            self.assertIn(f"- {pattern}\n", DEFAULT_FILTERS)

    def test_conflict_suffix(self):
        self.assertEqual(conflict_suffix("conflitto-locale", "conflitto-koofr"),
                         "conflitto-locale-{DateOnly}-,conflitto-koofr-{DateOnly}-")
        self.assertEqual(conflict_suffix("bad, word/x", ""), "bad-word-x-{DateOnly}-,conflict-{DateOnly}-")
        args = bisync_args("rclone", request(conflict_suffix="a-{DateOnly}-,b-{DateOnly}-"))
        self.assertEqual(args[args.index("--conflict-suffix") + 1], "a-{DateOnly}-,b-{DateOnly}-")
        self.assertEqual(args[args.index("--conflict-loser") + 1], "num")     # never "pathname"
        self.assertEqual(args[args.index("--checkers") + 1], "16")
        args = bisync_args("rclone", request(bwlimit="500k:off"))
        self.assertEqual(args[args.index("--bwlimit") + 1], "500k:off")
        args = bisync_args("rclone", request(user_agent="Spola/0.1.0 (abcd1234)"))
        self.assertEqual(args[args.index("--user-agent") + 1], "Spola/0.1.0 (abcd1234)")

    def test_describe(self):
        self.assertEqual(describe(Plan()), "")
        plan = Plan(upload=["a", "b", "c"], download=["d"], replace_local=["d"], delete_remote=["e"],
                    delete_local=["f"], conflicts=["g"])
        self.assertEqual(describe(plan), "3 files to Koofr, 1 file from Koofr, 2 deleted, 1 conflict")
        self.assertEqual(describe(Plan(moved_remote=[("a", "b")], moved_local=[("c", "d")])), "2 moved")

    def test_network_failure_is_a_retry_not_a_repair(self):
        net = Event(Kind.ERROR, "Failed to copy: Post https://app.koofr.net/...: dial tcp: lookup app.koofr.net: no such host", "error")
        crit = Event(Kind.CRITICAL, "Bisync critical error: ...", "error")
        out = summarize(7, [net, crit], "/a/", "koofr:b")
        self.assertIs(out.result, Result.RETRY)
        self.assertTrue(out.network)
        self.assertIs(summarize(7, [crit], "/a/", "koofr:b").result, Result.NEEDS_REPAIR)


if __name__ == "__main__":
    unittest.main()
