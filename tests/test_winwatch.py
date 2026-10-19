#!/usr/bin/env python3
"""The change monitor on Windows: one handle over the whole tree, paths
with "/", the exclusions, a buffer that overflows, and a stop that lets
the folder go."""

import ctypes
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _setup  # noqa: F401

if sys.platform != "win32":
    raise unittest.SkipTest("ReadDirectoryChangesW")

from gi.repository import GLib  # noqa: E402

from spola import watcher, winwatch  # noqa: E402
from spola.rclone import default_excluded  # noqa: E402


def handles() -> int:
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return count.value


def spin_until(condition, seconds: float = 10) -> bool:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)
    return condition()


class WinWatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "Work"
        for i in range(300):
            (self.root / f"d{i:03}" / "inner").mkdir(parents=True)
        self.told: list[str] = []

    def watch(self, **options) -> winwatch.WinTreeWatcher:
        w = watcher.TreeWatcher(str(self.root), self.told.append, **options)
        self.assertIsInstance(w, winwatch.WinTreeWatcher)
        w.start()
        self.addCleanup(w.stop)
        time.sleep(0.2)
        return w

    def test_the_whole_tree_with_one_handle(self):
        before = handles()
        w = self.watch(ignore=default_excluded)
        self.assertLessEqual(handles() - before, 4)         # GLib's monitor: two per folder, 600 folders
        self.assertEqual((w.count, w.overflow), (1, False))
        deep = self.root / "d042" / "inner" / "report.txt"
        deep.write_text("x")
        (self.root / "d007" / "inner" / "~$lock.docx").write_text("x")     # Office's lock file: excluded
        self.assertTrue(spin_until(lambda: "d042/inner/report.txt" in self.told))
        deep.rename(deep.with_name("final.txt"))
        self.assertTrue(spin_until(lambda: "d042/inner/final.txt" in self.told))
        self.assertIn("d042/inner/report.txt", self.told)
        shutil.rmtree(self.root / "d100")
        self.assertTrue(spin_until(lambda: "d100" in self.told))
        self.assertNotIn("d007/inner/~$lock.docx", self.told)
        self.assertFalse(any("\\" in rel for rel in self.told))

    def test_more_than_the_buffer_holds_tells_the_root(self):
        w = winwatch.WinTreeWatcher(str(self.root), self.told.append)
        w.BUFFER_BYTES = 16                                  # not even one name
        w.start()
        self.addCleanup(w.stop)
        time.sleep(0.2)
        (self.root / "d001" / "a long enough name.txt").write_text("x")
        self.assertTrue(spin_until(lambda: "" in self.told))

    def test_stopped_it_tells_nothing_and_lets_the_folder_go(self):
        w = self.watch()
        start = time.monotonic()
        w.stop()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(w.count, 0)
        (self.root / "after.txt").write_text("x")
        spin_until(lambda: False, 0.6)
        self.assertEqual(self.told, [])
        shutil.rmtree(self.root)                             # no handle left on it
        w.stop()                                             # twice: fine

    def test_names_of_the_records(self):
        def record(name: str, last: bool = False) -> bytes:
            raw = name.encode("utf-16-le")
            size = (12 + len(raw) + 3) // 4 * 4
            return struct.pack("<III", 0 if last else size, 1, len(raw)) + raw + b"\0" * (size - 12 - len(raw))
        data = record("a\\b.txt") + record("Foto 😀.jpg", last=True)
        self.assertEqual(winwatch.names_in(data), {"a/b.txt", "Foto 😀.jpg"})
        self.assertEqual(winwatch.names_in(b""), set())


if __name__ == "__main__":
    unittest.main()
