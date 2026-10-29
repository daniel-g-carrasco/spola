#!/usr/bin/env python3
"""File Explorer's entries: which pair an item is in, Koofr's page of it,
the condition that shows the entries only in the synced folders, and the
registry keys written and removed."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import _setup

from spola import explorer, places
from spola.config import Pair
from spola.paths import Paths
from spola.rclone import clean_env, default_excluded


class LocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "Work"
        (self.root / "Sub dir").mkdir(parents=True)
        (self.root / "Sub dir" / "a.txt").write_text("x")
        self.pair = Pair("p1", str(self.root), "/data/work", initialized=True)
        self.other = Pair("p2", str(Path(self.tmp.name) / "Workshop"), "/shop")

    def test_inside_the_pair_or_not(self):
        pairs = [self.other, self.pair]
        self.assertEqual(places.locate(pairs, str(self.root)), (self.pair, ""))
        self.assertEqual(places.locate(pairs, str(self.root / "Sub dir" / "a.txt")), (self.pair, "Sub dir/a.txt"))
        self.assertIsNone(places.locate([self.pair], str(Path(self.tmp.name) / "Workshop" / "x")))   # not a prefix
        self.assertIsNone(places.locate(pairs, self.tmp.name))

    @unittest.skipUnless(_setup.WINDOWS, "NTFS: case and separators do not matter")
    def test_windows_case_and_separators(self):
        path = str(self.root / "Sub dir" / "a.txt").upper().replace("/", "\\")
        pair, relative = places.locate([self.pair], path)
        self.assertEqual(relative.casefold(), "sub dir/a.txt")

    def test_koofr_page_and_remote(self):
        self.assertEqual(explorer.remote_path(self.pair, ""), "/data/work")
        self.assertEqual(explorer.remote_path(self.pair, "Sub dir/a.txt"), "/data/work/Sub dir/a.txt")
        file_page = explorer.page_url("m1", self.pair, "Sub dir/a.txt", is_dir=False)
        folder_page = explorer.page_url("m1", self.pair, "Sub dir", is_dir=True)
        self.assertEqual(file_page, folder_page)                # a file: the folder that holds it
        self.assertIn("m1", folder_page)

    def test_no_link_for_what_is_not_synced(self):
        from spola.rclone import default_excluded
        self.assertNotEqual(explorer.link_refusal(self.pair, ".spola-root", False, default_excluded), "")
        self.assertEqual(explorer.link_refusal(self.pair, "Sub dir/a.txt", False, default_excluded), "")
        self.assertEqual(explorer.link_refusal(self.pair, "", True, default_excluded), "")     # the folder itself

    def test_sharing_needs_a_confirmed_pair(self):
        from spola.rclone import default_excluded
        self.pair.initialized = False
        self.assertNotEqual(explorer.link_refusal(self.pair, "Sub dir/a.txt", False, default_excluded), "")
        self.assertNotEqual(explorer.link_refusal(self.pair, "", True, default_excluded), "")

    def test_share_filters_respect_order_anchors_and_directory_traversal(self):
        paths = Paths(Path(self.tmp.name) / "c", Path(self.tmp.name) / "s", Path(self.tmp.name) / "d")
        filters = paths.filters_file(self.pair.id)
        filters.parent.mkdir(parents=True)
        cases = (
            ("+ *.pdf\n- **\n", "report.pdf", False, True),
            ("+ *.pdf\n- **\n", "nested/report.pdf", False, True),
            ("+ *.pdf\n- **\n", "nested/report.txt", False, False),
            ("+ *.pdf\n- **\n", "nested", True, True),
            ("- *.pdf\n+ report.pdf\n", "report.pdf", False, False),
            ("- /secret.txt\n", "secret.txt", False, False),
            ("- /secret.txt\n", "nested/secret.txt", False, True),
            ("- /A/B/**\n", "A/B/secret.txt", False, False),
            ("- /A/B/**\n", "A/B", True, False),
            ("- /A/B/**\n", "nested/A/B/secret.txt", False, True),
            ("- A/B/**\n", "nested/A/B/secret.txt", False, False),
            ("- private/\n", "nested/private/secret.txt", False, False),
            ("- /private/\n+ *.pdf\n- **\n", "private/report.pdf", False, False),
            ("+ *.pdf\n- /private/\n- **\n", "private/report.pdf", False, True),
            ("+ /allowed/report.pdf\n- **\n", "allowed", True, True),
            ("+ /allowed/report.pdf\n- **\n", "elsewhere", True, False),
            ("+ /allowed/\n- **\n", "allowed/report.pdf", False, False),
            ("- *.pdf\n!\n+ *.pdf\n- **\n", "report.pdf", False, True),
            ("+ *.part\n- **\n", "report.part", False, True),
            ("; comment\n# comment\n- report?.txt\n", "nested/report1.txt", False, False),
            ("- report?.txt\n", "nested/report10.txt", False, True),
            ("- /A/*/secret.txt\n", "A/B/C/secret.txt", False, True),
            ("- /A/**/secret.txt\n", "A/B/C/secret.txt", False, False),
            ("- /A/*\n", "A/B", True, True),
            ("- *\n", "A/B", True, False),
            ("-  secret.txt\n", "secret.txt", False, True),
            ("-  secret.txt\n", " secret.txt", False, False),
            ("- secret.txt # literal\n", "secret.txt", False, True),
        )
        for text, relative, is_dir, included in cases:
            with self.subTest(filters=text, relative=relative, is_dir=is_dir):
                filters.write_text(text)
                refusal = explorer.link_refusal(self.pair, relative, is_dir, default_excluded, paths)
                self.assertEqual(refusal == "", included)

    def test_share_filters_refuse_unsupported_or_malformed_syntax_explicitly(self):
        paths = Paths(Path(self.tmp.name) / "c", Path(self.tmp.name) / "s", Path(self.tmp.name) / "d")
        filters = paths.filters_file(self.pair.id)
        filters.parent.mkdir(parents=True)
        for text in ("- *.{{pdf}}\n", "- *.[pt]df\n", "- *.{pdf,txt}\n", "- report\\?.pdf\n",
                     "- ***\n", "invalid\n", "+ **\n- *.[pt]df\n"):
            with self.subTest(filters=text):
                filters.write_text(text)
                refusal = explorer.link_refusal(self.pair, "report.pdf", False, default_excluded, paths)
                self.assertIn("unsupported", refusal.lower())

    @unittest.skipUnless(_setup.rclone_binary(), "rclone is needed to compare filter semantics")
    def test_share_filter_subset_matches_rclone_listings(self):
        paths = Paths(Path(self.tmp.name) / "c", Path(self.tmp.name) / "s", Path(self.tmp.name) / "d")
        filters = paths.filters_file(self.pair.id)
        filters.parent.mkdir(parents=True)
        for relative in ("report.pdf", "report.txt", "secret.txt", "report.part", "A/B/secret.txt",
                         "A/B/report.pdf", "nested/secret.txt", "nested/report.pdf", "private/report.pdf",
                         "allowed/report.pdf", "nested/private/report.pdf"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture")
        for text in ("+ *.pdf\n- **\n", "- /secret.txt\n", "- /A/B/**\n", "- A/B/**\n",
                     "- private/\n", "- /private/\n+ *.pdf\n- **\n", "+ *.pdf\n- /private/\n- **\n",
                     "+ /allowed/report.pdf\n- **\n", "+ /allowed/\n- **\n", "- *.pdf\n!\n",
                     "+ *.part\n- **\n", "- /A/*/secret.txt\n", "- /A/**/secret.txt\n", "- *\n"):
            with self.subTest(filters=text):
                filters.write_text(text)
                result = subprocess.run([_setup.rclone_binary(), "lsf", "-R", str(self.root),
                                         "--config", os.devnull, "--filter-from", str(filters)],
                                        capture_output=True, text=True, timeout=15, env=clean_env())
                self.assertEqual(result.returncode, 0, result.stderr)
                listed = set(result.stdout.splitlines())
                for item in self.root.rglob("*"):
                    relative = item.relative_to(self.root).as_posix()
                    is_dir = item.is_dir()
                    refusal = explorer.link_refusal(self.pair, relative, is_dir, default_excluded, paths)
                    self.assertEqual(refusal == "", relative + ("/" if is_dir else "") in listed,
                                     (text, relative, is_dir))

    def test_share_menu_opens_the_dialog(self):
        entries = explorer.verbs(r"C:\Spola\Spola.exe")
        share = next(entry for entry in entries if entry[0] == "Spola.CopyKoofrLink")
        self.assertEqual(share[1], "Share…")
        self.assertEqual(share[2], '"C:\\Spola\\Spola.exe" --share "%1"')

    def test_link_errors_in_words(self):
        rclone_400 = ("2026/10/14 23:19:23 NOTICE: Failed to link: Invalid response status! Got 400, "
                      "expected [200 201]; headers: map[Cache-Control:[no-cache]]")
        self.assertIn("a.txt", explorer.link_error("a.txt", rclone_400))
        self.assertNotIn("Cache-Control", explorer.link_error("a.txt", rclone_400))
        other = explorer.link_error("a.txt", "line one\nconnection refused")
        self.assertIn("connection refused", other)

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
                    self.assertEqual(winreg.QueryValueEx(key, "")[0], f'"{exe}" --share "%1"')
                # the kept copies: a file's previous versions, a folder's deleted files
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{classes}\*\shell\Spola.Versions\command") as key:
                    self.assertEqual(winreg.QueryValueEx(key, "")[0], f'"{exe}" --versions "%1"')
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    rf"{classes}\Directory\shell\Spola.Deleted\command") as key:
                    self.assertEqual(winreg.QueryValueEx(key, "")[0], f'"{exe}" --deleted "%1"')
                for missing in (r"Directory\shell\Spola.Versions", r"*\shell\Spola.Deleted"):
                    with self.assertRaises(OSError):
                        winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{classes}\{missing}")
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
            (Path(inside) / ".spola-root").write_text("p\n")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, verb)
                winreg.SetValueEx(key, "AppliesTo", 0, winreg.REG_SZ, explorer.applies_to([Pair("p", inside, "/x")]))
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "cmd /c exit")
            try:
                items = ", ".join("@('%s', '%s')" % (os.path.normpath(f).replace("/", "\\"), name)
                                  for f, name in ((inside, "f.txt"), (inside, ".spola-root"), (outside, "f.txt")))
                script = ("$sh = New-Object -ComObject Shell.Application; foreach ($i in @(%s)) { "
                          "[bool]($sh.Namespace($i[0]).ParseName($i[1]).Verbs() | Where-Object { $_.Name -eq '%s' }) }"
                          % (items, verb))
                out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                     capture_output=True, text=True, timeout=60).stdout.split()
            finally:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path + r"\command")
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        self.assertEqual(out, ["True", "False", "False"])     # inside; Spola's own file; outside


if __name__ == "__main__":
    unittest.main()
