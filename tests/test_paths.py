#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Where the app keeps its files: the XDG folders, Windows' profile
folders, the nearest folder of a path that is gone, and a file the app
ships found in a checkout and in a frozen build."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup

paths = _setup.shared("app.paths")
Paths, nearest_folder = paths.Paths, paths.nearest_folder

SLUG, EXE_NAME = "my-app", "MyApp"


def absolute(name: str) -> str:
    """An absolute folder for the tests: since Python 3.13 "/c" is not
    absolute on Windows (no drive), and the XDG variables are taken only
    when absolute."""
    return rf"C:\{name}" if _setup.WINDOWS else f"/{name}"


class PathsTest(unittest.TestCase):
    def test_xdg_variables_win(self):
        found = Paths.from_env(SLUG, EXE_NAME, {"XDG_CONFIG_HOME": absolute("c"), "XDG_STATE_HOME": absolute("s"),
                               "XDG_DATA_HOME": absolute("d"), "HOME": absolute("h")})
        self.assertEqual((found.config, found.state, found.data),
                         (Path(absolute("c")) / SLUG, Path(absolute("s")) / SLUG, Path(absolute("d")) / SLUG))
        self.assertEqual(found.config_file, Path(absolute("c")) / SLUG / "config.json")
        self.assertEqual(found.window_state_file, Path(absolute("s")) / SLUG / "window.json")
        self.assertEqual(found.cache, Path(absolute("h")) / ".cache" / SLUG)     # no XDG_CACHE_HOME: under HOME
        cached = Paths.from_env(SLUG, EXE_NAME, {"XDG_CONFIG_HOME": absolute("c"), "XDG_CACHE_HOME": absolute("k")})
        self.assertEqual(cached.cache_folder, Path(absolute("k")) / SLUG)
        self.assertEqual(Paths(Path("c"), Path("s"), Path("d")).cache_folder, Path("s") / "cache")

    @unittest.skipIf(_setup.WINDOWS, "Linux folders (Windows: the profile folders, below)")
    def test_the_home_folder_without_them(self):
        found = Paths.from_env(SLUG, EXE_NAME, {"HOME": "/h", "XDG_STATE_HOME": "relative/is/ignored"})
        self.assertEqual(found.config, Path("/h") / ".config" / SLUG)
        self.assertEqual(found.state, Path("/h") / ".local" / "state" / SLUG)
        self.assertEqual(found.cache, Path("/h") / ".cache" / SLUG)

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_profile_folders(self):
        found = Paths.from_env(SLUG, EXE_NAME, {"APPDATA": r"C:\U\me\AppData\Roaming",
                                                "LOCALAPPDATA": r"C:\U\me\AppData\Local"})
        self.assertEqual(found.config, Path(r"C:\U\me\AppData\Roaming") / EXE_NAME)
        self.assertEqual(found.state, Path(r"C:\U\me\AppData\Local") / EXE_NAME / "state")
        self.assertEqual(found.data, Path(r"C:\U\me\AppData\Local") / EXE_NAME / "data")
        self.assertEqual(found.cache, Path(r"C:\U\me\AppData\Local") / EXE_NAME / "cache")

    def test_the_log_takes_the_name_it_is_given_in_the_state_folder(self):
        found = Paths(Path("c"), Path("s"), Path("d"))
        self.assertEqual(found.log_file("my-app.log"), Path("s") / "my-app.log")

    def test_nearest_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(nearest_folder(os.path.join(tmp, "a", "b", "c.txt")), tmp)
            os.makedirs(os.path.join(tmp, "a"))
            self.assertEqual(nearest_folder(os.path.join(tmp, "a", "b", "c.txt")), os.path.join(tmp, "a"))


class ShippedTest(unittest.TestCase):
    """A file the app ships, found where the build put it."""

    SLUG = "my-app-made-up-for-the-tests"      # under no /usr/share of a real system

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_in_a_checkout_its_data_folder(self):
        icon = self.tmp / "data" / "icons" / "app.svg"
        icon.parent.mkdir(parents=True)
        icon.write_text("<svg/>", encoding="utf-8")
        self.assertEqual(paths.shipped("icons", "app.svg", checkout=self.tmp, slug=self.SLUG), icon)
        self.assertEqual(paths.shipped("icons", checkout=self.tmp, slug=self.SLUG), icon.parent)     # a folder too

    def test_in_the_frozen_build_the_share_folder_next_to_the_executable(self):
        bundle = self.tmp / "bundle"
        table = bundle / "share" / "tables" / "one.json"
        table.parent.mkdir(parents=True)
        table.write_text("{}", encoding="utf-8")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(paths.shipped("tables", "one.json", checkout=None, slug=self.SLUG), table)
        # from the sources the bundle is not looked at
        self.assertIsNone(paths.shipped("tables", "one.json", checkout=self.tmp, slug=self.SLUG))

    def test_the_checkout_comes_first(self):
        mine = self.tmp / "data" / "fonts"
        bundle = self.tmp / "bundle"
        mine.mkdir(parents=True)
        (bundle / "share" / "fonts").mkdir(parents=True)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(paths.shipped("fonts", checkout=self.tmp, slug=self.SLUG), mine)

    def test_nowhere_is_none(self):
        self.assertIsNone(paths.shipped("no", "such.file", checkout=self.tmp, slug=self.SLUG))
        self.assertIsNone(paths.shipped("no", "such.file", checkout=None, slug=self.SLUG))


if __name__ == "__main__":
    unittest.main()
