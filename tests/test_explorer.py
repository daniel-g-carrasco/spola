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
        self.assertIn('System.ParsingPath:~<"', condition)
        self.assertIn('Work\\"', condition)
        self.assertEqual(condition.count(" OR "), 3)


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class RegistryTest(unittest.TestCase):
    def test_written_for_the_pairs_and_removed(self):
        # under a key of the test's own: the real entries of an installed
        # Spola on the machine that runs the tests stay as they are
        import winreg
        from unittest import mock
        classes = rf"Software\SpolaTest-{os.getpid()}\Classes"
        exe = r"C:\Program Files\Spola\Spola.exe"
        try:
            with mock.patch.object(explorer, "CLASSES", classes):
                # as MSYS2's Python writes sys.executable: Explorer cannot start that
                explorer.register(exe.replace("\\", "/"), [Pair("p1", os.path.expanduser("~"), "/w")])
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{classes}\Directory\shell\Spola.OpenOnKoofr") as key:
                    self.assertIn("System.ParsingPath", winreg.QueryValueEx(key, "AppliesTo")[0])
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    rf"{classes}\*\shell\Spola.CopyKoofrLink\command") as key:
                    self.assertEqual(winreg.QueryValueEx(key, "")[0], f'"{exe}" --copy-link "%1"')
                explorer.register(exe, [])                          # no pairs: no entries
                for target in explorer.TARGETS:
                    for verb in explorer.VERBS:
                        with self.assertRaises(OSError):
                            winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{classes}\{target}\shell\{verb}")
        finally:
            for sub in [rf"{classes}\{t}\shell" for t in explorer.TARGETS] + [rf"{classes}\{t}" for t in explorer.TARGETS] \
                    + [classes, rf"Software\SpolaTest-{os.getpid()}"]:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class ShellTest(unittest.TestCase):
    def test_the_shell_shows_the_entry_inside_only(self):
        # the shell itself asked (Shell.Application, in a PowerShell of its
        # own), with a verb of the test's own: AppliesTo on paths is easy to
        # get wrong (System.ItemPathDisplay never matches there)
        import subprocess
        import winreg
        verb = f"SpolaTest{os.getpid()}"
        key_path = rf"Software\Classes\*\shell\{verb}"
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as inside, \
                tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as outside:
            for folder in (inside, outside):
                (Path(folder) / "f.txt").write_text("x")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, verb)
                winreg.SetValueEx(key, "AppliesTo", 0, winreg.REG_SZ, explorer.applies_to([Pair("p", inside, "/x")]))
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "cmd /c exit")
            try:
                folders = ", ".join("'%s'" % os.path.normpath(f).replace("/", "\\") for f in (inside, outside))
                script = ("$sh = New-Object -ComObject Shell.Application; foreach ($d in @(%s)) { "
                          "[bool]($sh.Namespace($d).ParseName('f.txt').Verbs() | Where-Object { $_.Name -eq '%s' }) }"
                          % (folders, verb))
                out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                     capture_output=True, text=True, timeout=60).stdout.split()
            finally:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path + r"\command")
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        self.assertEqual(out, ["True", "False"])


if __name__ == "__main__":
    unittest.main()
