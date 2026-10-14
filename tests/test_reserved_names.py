#!/usr/bin/env python3
"""The names Windows 10 takes for devices (CON, PRN, AUX, NUL, COM1-9,
LPT1-9, with any extension): left out of every run there, on both sides."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup

from spola import jobs
from spola.config import Config, Pair
from spola.paths import Paths
from spola.rclone import Mode, clean_env, reserved_exclude_lines


@unittest.skipUnless(_setup.rclone_binary(), "rclone")
class ReservedRulesTest(unittest.TestCase):
    def test_the_rules_leave_out_those_names_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            names = ["CON", "con.txt", "Aux.H", "nul.tar.gz", "COM1", "lpt9.doc", "PRN .pdf",
                     "auxiliary.txt", "console.txt", "com10.txt", "lpt.txt", "a.con", "normal.txt"]
            for name in names:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("x")
            (root / "sub" / "Com3").mkdir(parents=True)
            (root / "sub" / "Com3" / "inside.txt").write_text("x")
            (root / "console").mkdir()
            (root / "console" / "ok.txt").write_text("x")
            rules = Path(tmp) / "rules.txt"
            rules.write_text(reserved_exclude_lines(), encoding="utf-8", newline="\n")
            listed = subprocess.run([_setup.rclone_binary(), "lsf", "-R", "--files-only", str(root),
                                     "--exclude-from", str(rules)],
                                    capture_output=True, text=True, encoding="utf-8", env=clean_env(), check=True)
            kept = sorted(listed.stdout.split())
        self.assertEqual(kept, sorted(["auxiliary.txt", "console.txt", "com10.txt", "lpt.txt", "a.con",
                                       "normal.txt", "console/ok.txt"]))


class PrepareTest(unittest.TestCase):
    def test_on_windows_10_every_run_leaves_them_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "local").mkdir()
            paths = Paths(t / "config", t / "state", t / "data")
            pair = Pair("p1", str(t / "local"), "/test")
            with mock.patch.object(jobs, "windows_reserved_names_matter", return_value=True):
                job = jobs.prepare(Config(pairs=[pair]), pair, paths, mode=Mode.RESYNC, dry_run=True)
            self.assertIsNotNone(job.request.exclude_from)
            self.assertEqual(Path(job.request.exclude_from).read_bytes(), reserved_exclude_lines().encode("utf-8"))
            with mock.patch.object(jobs, "windows_reserved_names_matter", return_value=False):
                job = jobs.prepare(Config(pairs=[pair]), pair, paths, mode=Mode.RESYNC, dry_run=True)
            self.assertIsNone(job.request.exclude_from)         # nothing to leave out


if __name__ == "__main__":
    unittest.main()
