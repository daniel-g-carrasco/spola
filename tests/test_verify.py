"""The verification of the two sides: rclone check's report as an outcome,
and a real check between two local folders."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401
from _setup import rclone_binary
from spola import verify
from spola.rclone import DEFAULT_FILTERS, BisyncRequest, Event, Kind, Mode, Result, parse_line

RCLONE = rclone_binary()


def request(tmp: Path, path1: str, path2: str) -> BisyncRequest:
    filters = tmp / "filters.txt"
    filters.write_text(DEFAULT_FILTERS)
    return BisyncRequest(path1=path1, path2=path2, workdir=tmp / "wd", rclone_config=tmp / "none.conf",
                         filters_file=filters, backup_dir1=tmp / "bk", max_delete=10, mode=Mode.CHECK,
                         dry_run=True, force=False, conflict_suffix="x")


class ReportTest(unittest.TestCase):
    def test_the_report_becomes_an_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "verify.txt"
            report.write_text("= same.txt\n+ only here.txt\n- only/on koofr.txt\n* differs.txt\n! unreadable.txt\n"
                              "= sub/deep.txt\nnoise\n")
            stats = Event(Kind.STATS, "", "notice", stats={"checks": 6})
            out = verify.summarize_check(1, [stats], report)
            self.assertIs(out.result, Result.OK)
            self.assertEqual(out.identical, 2)
            self.assertEqual(out.plan.upload, ["only here.txt"])
            self.assertEqual(out.plan.download, ["only/on koofr.txt"])
            self.assertEqual(out.plan.conflicts, ["differs.txt"])
            self.assertEqual(out.plan.error_paths, ["unreadable.txt"])
            self.assertEqual(out.stats, {"checks": 6})
            # all the same: exit 0
            report.write_text("= a\n= b\n")
            out = verify.summarize_check(0, [], report)
            self.assertIs(out.result, Result.OK)
            self.assertEqual((out.identical, out.plan.upload), (2, []))
            # no report and an error: failed, in rclone's words or as a network problem
            report.unlink()
            error = Event(Kind.OTHER, "Failed to check: directory not found", "error")
            out = verify.summarize_check(3, [error], report)
            self.assertIs(out.result, Result.FAILED)
            self.assertIn("directory not found", out.message)
            down = Event(Kind.OTHER, "error: dial tcp: lookup app.koofr.net: no such host", "error")
            out = verify.summarize_check(1, [down], report)
            self.assertIs(out.result, Result.FAILED)
            self.assertTrue(out.network)
            self.assertIs(verify.summarize_check(1, [], report, cancelled=True).result, Result.CANCELLED)


@unittest.skipUnless(RCLONE, "rclone not found")
class RealCheckTest(unittest.TestCase):
    def test_two_folders_compared_by_size_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a, b = tmp / "a", tmp / "b"
            (a / "sub").mkdir(parents=True)
            b.mkdir()
            (a / "same.txt").write_text("same")
            (b / "same.txt").write_text("same")
            (a / "sub" / "here.txt").write_text("here")
            (b / "there.txt").write_text("there")
            (a / "differs.txt").write_text("aaaa")
            (b / "differs.txt").write_text("bbbb")                  # same size, other content
            (a / ".DS_Store").write_text("x")                        # left out by the default filters
            (a / "link").symlink_to(a / "same.txt")                  # skipped, not an error
            os.utime(b / "differs.txt", ns=(os.stat(a / "differs.txt").st_mtime_ns,) * 2)
            report = tmp / "verify.txt"
            argv = verify.check_args(RCLONE, request(tmp, str(a), str(b)), report)
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            events = [e for e in (parse_line(line) for line in done.stderr.splitlines()) if e]
            out = verify.summarize_check(done.returncode, events, report)
            self.assertIs(out.result, Result.OK, done.stderr)
            self.assertEqual(out.identical, 1)
            self.assertEqual(out.plan.upload, ["sub/here.txt"])
            self.assertEqual(out.plan.download, ["there.txt"])
            self.assertEqual(out.plan.conflicts, ["differs.txt"])
            self.assertEqual(out.plan.error_paths, [])
            # nothing was changed
            self.assertEqual((b / "differs.txt").read_text(), "bbbb")
            self.assertFalse((b / "sub").exists())
