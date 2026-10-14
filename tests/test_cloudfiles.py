#!/usr/bin/env python3
"""File Explorer's sync status: what is marked in sync after a run, and a
real sync root (a scratch folder), registered, marked, and taken out again,
its files still ordinary files for rclone."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _setup

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

from spola import cloudfiles  # noqa: E402
from spola.rclone import clean_env  # noqa: E402


class HelpersTest(unittest.TestCase):
    def test_what_a_run_touched_and_the_folders_above(self):
        root = os.path.join("C:\\", "root")
        got = cloudfiles.paths_and_parents(root, ["a/b/c.txt", "a/d.txt", "e.txt"])
        rel = [os.path.relpath(p, root).replace("\\", "/") for p in got]
        self.assertEqual(rel, ["a/b/c.txt", "a/b", "a/d.txt", "a", "e.txt", "."])     # deepest first, the root last

    def test_walk_leaves_out_what_is_not_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("a.txt", "node_modules/x/y.js", "sub/b.txt", ".spola-root"):
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text("x")
            skip = lambda rel: rel.startswith("node_modules/") or rel.startswith(".spola-")    # noqa: E731
            got = sorted(os.path.relpath(p, tmp).replace("\\", "/") for p in cloudfiles.walk(tmp, skip))
        self.assertEqual(got, [".", "a.txt", "sub", "sub/b.txt"])

    def test_root_id(self):
        sid = cloudfiles.user_sid()
        self.assertTrue(sid.startswith("S-1-"))
        self.assertEqual(cloudfiles.root_id(sid, "p1"), f"Spola!{sid}!p1")


class SyncRootTest(unittest.TestCase):
    def test_a_scratch_sync_root(self):
        cloudfiles.winrt_thread()
        identity = cloudfiles.root_id(cloudfiles.user_sid(), f"test-{os.getpid()}")
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "a.txt").write_text("uno")
            (Path(tmp) / "sub" / "b.txt").write_text("due")
            try:
                cloudfiles.register(identity, tmp, "Spola test", "", "0")
            except OSError as e:
                self.skipTest(f"no sync roots here: {e}")
            key = None
            try:
                self.assertIn(identity, cloudfiles.registered())
                key = cloudfiles.connect(tmp)
                for path in cloudfiles.walk(tmp):
                    self.assertTrue(cloudfiles.mark_in_sync(path), path)
                self.assertTrue(cloudfiles.mark_in_sync(os.path.join(tmp, "a.txt")))    # a placeholder already
                if _setup.rclone_binary():
                    listed = subprocess.run([_setup.rclone_binary(), "lsf", "-R", tmp], capture_output=True,
                                            text=True, env=clean_env(), check=True).stdout.split()
                    self.assertEqual(sorted(listed), ["a.txt", "sub/", "sub/b.txt"])
            finally:
                if key is not None:
                    cloudfiles.disconnect(key)
                cloudfiles.unregister(identity)
            self.assertNotIn(identity, cloudfiles.registered())
            self.assertEqual((Path(tmp) / "a.txt").read_text(), "uno")


if __name__ == "__main__":
    unittest.main()
