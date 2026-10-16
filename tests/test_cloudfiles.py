#!/usr/bin/env python3
"""File Explorer's sync status: what is marked in sync (bisync's listing),
the keys of a root the shell lost, and a real sync root (a scratch folder),
registered, marked, and taken out again, its files still ordinary files
for rclone."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _setup

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

import winreg  # noqa: E402

from spola import cloudfiles  # noqa: E402
from spola.explorer_status import ExplorerStatus  # noqa: E402
from spola.rclone import clean_env  # noqa: E402

LISTED_NS = 1792054859704779200        # 2026-10-15T09:00:59.704779200 UTC
LISTING = ('# bisync listing v1 from 2026-10-15T09:17:40.592143500+0000\n'
           'd       -1 - - 2026-10-15T09:00:00.000000000+0000 "sub"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "sub/a.txt"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "b.txt"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "edited.txt"\n'
           '-        6 - - 2026-10-15T09:00:59.704779200+0000 "written.txt"\n')


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
            for rel in ("sub/a.txt", "b.txt", "edited.txt", "written.txt"):         # as listed
                Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(root, rel).write_text("sei ok")
                os.utime(Path(root, rel), ns=(LISTED_NS, LISTED_NS))
            Path(root, "edited.txt").write_text("edited while Spola was closed")
            for rel in (".spola-root", "new.txt", "sub/~$doc.docx", ".cache/x"):
                Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(root, rel).write_text("x")
            engine = SimpleNamespace(connect=lambda *a: None)
            marked, excluded = [], []

            def relative(paths):
                return [os.path.relpath(p, root).replace("\\", "/") for p in paths]

            def in_sync(path):      # written.txt: same size and date, but written since it was marked
                return not path.endswith("written.txt")

            with (mock.patch.object(cloudfiles, "mark_in_sync", lambda p: marked.append(p) or True),
                  mock.patch.object(cloudfiles, "in_sync", in_sync),
                  mock.patch.object(cloudfiles, "mark_excluded", lambda p, d: excluded.append(p) or True)):
                status = ExplorerStatus(engine, "", "0")
                status.stop()
                status._mark("p", root, workdir)
                # a folder after what it holds; never what changed since
                self.assertEqual(relative(marked), ["sub/a.txt", "b.txt", "sub", "."])
                self.assertEqual(sorted(relative(excluded)), [".cache", ".spola-root", "sub/~$doc.docx"])
                marked.clear()
                excluded.clear()
                Path(root, "Thumbs.db").write_text("x")
                status._mark("p", root, workdir, touched=["b.txt", "written.txt"])
                self.assertEqual(relative(marked), ["b.txt", "written.txt", "."])     # the run just copied it
                self.assertEqual(relative(excluded), ["Thumbs.db"])            # only what is new, next to it


class CopiedTest(unittest.TestCase):
    def test_a_file_copied_is_marked_at_once_if_its_root_is_connected(self):
        engine = SimpleNamespace(connect=lambda *a: None)
        marked = []
        with mock.patch.object(cloudfiles, "mark_in_sync", lambda p: marked.append(p) or True):
            status = ExplorerStatus(engine, "", "0")
            status.stop()
            status._mark_copied("p", os.path.join("C:\\", "root"), "sub/a.txt")
            self.assertEqual(marked, [])                    # not registered: nothing to mark
            status._connected["p"] = 1
            status._mark_copied("p", os.path.join("C:\\", "root"), "sub/a.txt")
            self.assertEqual(marked, [os.path.join("C:\\", "root", "sub", "a.txt")])
            self.assertEqual(status._marked["p"], {"sub/a.txt"})


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


def tree(root: str) -> dict[str, tuple[int, int]]:
    """Every file below root: (size, mtime in ns), as Python sees them."""
    found = {}
    for folder, _dirs, files in os.walk(root):
        for name in files:
            st = os.lstat(os.path.join(folder, name))
            found[os.path.relpath(os.path.join(folder, name), root).replace("\\", "/")] = (st.st_size,
                                                                                          st.st_mtime_ns)
    return found


def rclone_lsl(root: str) -> list[str]:
    return sorted(subprocess.run([_setup.rclone_binary(), "lsl", root], capture_output=True, text=True,
                                 env=clean_env(), check=True).stdout.splitlines())


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
            (Path(tmp) / "w.txt").write_text("quattro")
            readonly = Path(tmp) / "ro.txt"
            readonly.write_text("cinque")
            os.chmod(readonly, stat.S_IREAD)
            before = tree(tmp)
            lsl_before = rclone_lsl(tmp) if _setup.rclone_binary() else []
            try:
                cloudfiles.register(identity, tmp, "Spola test", "", "0")
            except OSError as e:
                os.chmod(readonly, stat.S_IWRITE | stat.S_IREAD)
                self.skipTest(f"no sync roots here: {e}")
            key = None
            try:
                self.assertIn(identity, cloudfiles.registered())
                key = cloudfiles.connect(tmp)
                self.assertIsNone(cloudfiles.in_sync(os.path.join(tmp, "a.txt")))       # not a placeholder yet
                for rel in ("sub/b.txt", "a.txt", "w.txt", "ro.txt", "sub", ""):
                    path = os.path.join(tmp, *rel.split("/")) if rel else tmp
                    self.assertTrue(cloudfiles.mark_in_sync(path), path)      # a read-only file too
                self.assertTrue(cloudfiles.mark_in_sync(os.path.join(tmp, "a.txt")))    # a placeholder already
                self.assertEqual(tree(tmp), before)               # sizes and dates untouched
                if _setup.rclone_binary():
                    self.assertEqual(rclone_lsl(tmp), lsl_before)
                # written: Windows itself takes it out of sync
                self.assertIs(cloudfiles.in_sync(os.path.join(tmp, "w.txt")), True)
                with open(os.path.join(tmp, "w.txt"), "a") as f:
                    f.write(" e mezzo")
                self.assertIs(cloudfiles.in_sync(os.path.join(tmp, "w.txt")), False)
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".spola-root"), False))
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".cache"), True))
                self.assertTrue(cloudfiles.mark_excluded(os.path.join(tmp, ".cache"), True))   # again: no harm
                os.rename(os.path.join(tmp, ".spola-root"), os.path.join(tmp, "kept.txt"))
                self.assertTrue(cloudfiles.mark_in_sync(os.path.join(tmp, "kept.txt")))   # excluded no more
                marked = tree(tmp)
                if _setup.rclone_binary():
                    listed = subprocess.run([_setup.rclone_binary(), "lsf", "-R", tmp], capture_output=True,
                                            text=True, env=clean_env(), check=True).stdout.split()
                    self.assertEqual(sorted(listed), [".cache/", ".cache/c.txt", "a.txt", "kept.txt", "ro.txt",
                                                      "sub/", "sub/b.txt", "w.txt"])
                    lsl_marked = rclone_lsl(tmp)
            finally:
                if key is not None:
                    cloudfiles.disconnect(key)
                cloudfiles.unregister(identity)
            try:
                self.assertNotIn(identity, cloudfiles.registered())
                # taken out of the shell: the same files, plain ones, also in subfolders
                self.assertEqual(tree(tmp), marked)
                if _setup.rclone_binary():
                    self.assertEqual(rclone_lsl(tmp), lsl_marked)
                self.assertEqual((Path(tmp) / "a.txt").read_text(), "uno")
                self.assertEqual((Path(tmp) / "sub" / "b.txt").read_text(), "due")
                self.assertEqual((Path(tmp) / ".cache" / "c.txt").read_text(), "tre")
                for folder, dirs, files in os.walk(tmp):
                    for name in dirs + files:
                        attributes = os.lstat(os.path.join(folder, name)).st_file_attributes
                        self.assertFalse(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT, name)
            finally:
                os.chmod(readonly, stat.S_IWRITE | stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
