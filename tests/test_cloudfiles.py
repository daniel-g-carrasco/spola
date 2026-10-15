#!/usr/bin/env python3
"""File Explorer's sync status: what is marked in sync (bisync's listing),
the keys of a root the shell lost, and a real sync root (a scratch folder),
registered, marked, and taken out again, its files still ordinary files
for rclone."""

import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _setup

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

from spola import cloudfiles  # noqa: E402
from spola.explorer_status import ExplorerStatus  # noqa: E402
from spola.rclone import clean_env  # noqa: E402

LISTING = ('# bisync listing v1 from 2026-10-15T09:17:40.592143500+0000\n'
           'd       -1 - - 2026-10-15T09:00:00.000000000+0000 "sub"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "sub/a.txt"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "b.txt"\n')


class MarkTest(unittest.TestCase):
    def test_what_the_listing_holds_once_then_what_a_run_touched(self):
        # what the listing does not hold: excluded if the filters leave it
        # out, else Windows shows it waiting (changed here since)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            (workdir / "x..y.path1.lst").write_bytes(LISTING.encode())
            (workdir / "x..y.path2.lst").write_bytes(LISTING.encode())
            root = os.path.join(tmp, "root")
            for rel in ("sub/a.txt", "b.txt", ".spola-root", "new.txt", "sub/~$doc.docx", ".cache/x"):
                Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(root, rel).write_text("x")
            engine = SimpleNamespace(connect=lambda *a: None)
            marked, excluded = [], []

            def relative(paths):
                return [os.path.relpath(p, root).replace("\\", "/") for p in paths]

            with (mock.patch.object(cloudfiles, "mark_in_sync", lambda p: marked.append(p) or True),
                  mock.patch.object(cloudfiles, "mark_excluded", lambda p, d: excluded.append(p) or True)):
                status = ExplorerStatus(engine, "", "0")
                status.stop()
                status._mark("p", root, workdir)
                self.assertEqual(relative(marked), ["sub/a.txt", "b.txt", "sub", "."])     # a folder after what it holds
                self.assertEqual(sorted(relative(excluded)), [".cache", ".spola-root", "sub/~$doc.docx"])
                marked.clear()
                excluded.clear()
                Path(root, "Thumbs.db").write_text("x")
                status._mark("p", root, workdir, touched=["b.txt"])
                self.assertEqual(relative(marked), ["b.txt", "."])
                self.assertEqual(relative(excluded), ["Thumbs.db"])            # only what is new, next to it


class KeysTest(unittest.TestCase):
    def test_a_root_the_shell_lost_is_found_and_forgotten(self):
        # what Unregister leaves of a root whose folder is gone: the
        # navigation pane's entry (a scratch namespace key here)
        identity = f"Spola!test!gone-{os.getpid()}"
        clsid = "{%s}" % uuid.uuid4()
        namespace = rf"Software\SpolaTest-{os.getpid()}\NameSpace"
        with mock.patch.object(cloudfiles, "NAMESPACE", namespace):
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{namespace}\{clsid}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, identity)
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{clsid}").Close()
            try:
                self.assertIn(identity, cloudfiles.left_behind("Spola!test!"))
                cloudfiles.forget_keys(identity)
                self.assertNotIn(identity, cloudfiles.left_behind("Spola!test!"))
                with self.assertRaises(OSError):
                    winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\CLSID\{clsid}")
            finally:
                for path in (rf"{namespace}\{clsid}", namespace, rf"Software\SpolaTest-{os.getpid()}",
                             rf"Software\Classes\CLSID\{clsid}"):
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
                    except OSError:
                        pass


class HelpersTest(unittest.TestCase):
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
            (Path(tmp) / ".cache").mkdir()
            (Path(tmp) / ".cache" / "c.txt").write_text("tre")
            (Path(tmp) / ".spola-root").write_text("")
            try:
                cloudfiles.register(identity, tmp, "Spola test", "", "0")
            except OSError as e:
                self.skipTest(f"no sync roots here: {e}")
            key = None
            try:
                self.assertIn(identity, cloudfiles.registered())
                key = cloudfiles.connect(tmp)
                for rel in ("sub/b.txt", "a.txt", "sub", ""):
                    path = os.path.join(tmp, *rel.split("/")) if rel else tmp
                    self.assertTrue(cloudfiles.mark_in_sync(path), path)
                self.assertTrue(cloudfiles.mark_in_sync(os.path.join(tmp, "a.txt")))    # a placeholder already
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".spola-root"), False))
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".cache"), True))
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".cache"), True))   # again: no harm
                os.rename(os.path.join(tmp, ".spola-root"), os.path.join(tmp, "kept.txt"))
                self.assertTrue(cloudfiles.mark_in_sync(os.path.join(tmp, "kept.txt")))   # excluded no more
                if _setup.rclone_binary():
                    listed = subprocess.run([_setup.rclone_binary(), "lsf", "-R", tmp], capture_output=True,
                                            text=True, env=clean_env(), check=True).stdout.split()
                    self.assertEqual(sorted(listed), [".cache/", ".cache/c.txt", "a.txt", "kept.txt", "sub/",
                                                      "sub/b.txt"])
            finally:
                if key is not None:
                    cloudfiles.disconnect(key)
                cloudfiles.unregister(identity)
            self.assertNotIn(identity, cloudfiles.registered())
            self.assertEqual((Path(tmp) / "a.txt").read_text(), "uno")
            self.assertEqual((Path(tmp) / ".cache" / "c.txt").read_text(), "tre")


if __name__ == "__main__":
    unittest.main()
