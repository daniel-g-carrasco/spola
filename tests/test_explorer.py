#!/usr/bin/env python3
"""File Explorer's entries: which pair an item is in, Koofr's page of it,
the condition that shows the entries only in the synced folders, and the
registry keys written and removed."""

import os
import tempfile
import unittest
from pathlib import Path

import _setup

from spola import explorer
from spola.config import Pair


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Work"
        (self.root / "Sub dir").mkdir(parents=True)
        (self.root / "Sub dir" / "a.txt").write_text("x")
        self.pair = Pair("p1", str(self.root), "/data/work")
        self.other = Pair("p2", str(Path(self.tmp.name) / "Workshop"), "/shop")

    def test_inside_the_pair_or_not(self):
        pairs = [self.other, self.pair]
        self.assertEqual(explorer.locate(pairs, str(self.root)), (self.pair, ""))
        self.assertEqual(explorer.locate(pairs, str(self.root / "Sub dir" / "a.txt")), (self.pair, "Sub dir/a.txt"))
        self.assertIsNone(explorer.locate([self.pair], str(Path(self.tmp.name) / "Workshop" / "x")))   # not a prefix
        self.assertIsNone(explorer.locate(pairs, self.tmp.name))

    @unittest.skipUnless(_setup.WINDOWS, "NTFS: case and separators do not matter")
    def test_windows_case_and_separators(self):
        path = str(self.root / "Sub dir" / "a.txt").upper().replace("/", "\\")
        pair, relative = explorer.locate([self.pair], path)
        self.assertEqual(relative.casefold(), "sub dir/a.txt")

    def test_koofr_page_and_remote(self):
        self.assertEqual(explorer.remote_path(self.pair, ""), "/data/work")
        self.assertEqual(explorer.remote_path(self.pair, "Sub dir/a.txt"), "/data/work/Sub dir/a.txt")
        file_page = explorer.page_url("m1", self.pair, "Sub dir/a.txt", is_dir=False)
        folder_page = explorer.page_url("m1", self.pair, "Sub dir", is_dir=True)
        self.assertEqual(file_page, folder_page)                # a file: the folder that holds it
        self.assertIn("m1", folder_page)

    def test_applies_to(self):
        condition = explorer.applies_to([Pair("p1", "C:/Users/me/Work", "/w"), Pair("p2", "D:\\Photos\\", "/p")])
        self.assertIn('System.ItemPathDisplay:~<"', condition)
        self.assertIn('Work\\"', condition)
        self.assertEqual(condition.count(" OR "), 3)


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class RegistryTest(unittest.TestCase):
    def test_written_for_the_pairs_and_removed(self):
        import winreg
        saved = [(t, v) for t in explorer.TARGETS for v in explorer.VERBS]
        exe = r"C:\Program Files\Spola\Spola.exe"
        try:
            explorer.register(exe, [Pair("p1", os.path.expanduser("~"), "/w")])
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\shell\Spola.OpenOnKoofr") as key:
                self.assertIn("System.ItemPathDisplay", winreg.QueryValueEx(key, "AppliesTo")[0])
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\*\shell\Spola.CopyKoofrLink\command") as key:
                self.assertEqual(winreg.QueryValueEx(key, "")[0], f'"{exe}" --copy-link "%1"')
            explorer.register(exe, [])                          # no pairs: no entries
            for target, verb in saved:
                with self.assertRaises(OSError):
                    winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{target}\shell\{verb}")
        finally:
            explorer.unregister()


if __name__ == "__main__":
    unittest.main()
