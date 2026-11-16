#!/usr/bin/env python3
"""Spola's own instances of the shared services (app/services.py): its
folders are where they were before the shared modules (the settings, the
listings and the backups of an installed Spola must be found again), and
the files of each pair inside them (app/storage.py). The updates' project
is tested in test_updates_spola.py; the shared modules themselves by the
common tests, with made-up names."""

import unittest
from pathlib import Path

import _setup

from spola.app import conflicts, services
from spola.app.storage import Paths


def absolute(name: str) -> str:
    """An absolute folder: the XDG variables are taken only when absolute."""
    return rf"C:\{name}" if _setup.WINDOWS else f"/{name}"


class FoldersTest(unittest.TestCase):
    def test_the_xdg_folders_are_spolas(self):
        paths = services.app_paths({"XDG_CONFIG_HOME": absolute("c"), "XDG_STATE_HOME": absolute("s"),
                                    "XDG_DATA_HOME": absolute("d"), "HOME": absolute("h")})
        self.assertIsInstance(paths, Paths)
        self.assertEqual((paths.config, paths.state, paths.data),
                         (Path(absolute("c")) / "spola", Path(absolute("s")) / "spola", Path(absolute("d")) / "spola"))
        self.assertEqual(paths.config_file, Path(absolute("c")) / "spola" / "config.json")

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_on_windows_the_folders_of_spola_before(self):
        paths = services.app_paths({"APPDATA": r"C:\U\AppData\Roaming", "LOCALAPPDATA": r"C:\U\AppData\Local"})
        self.assertEqual((paths.config, paths.state, paths.data),
                         (Path(r"C:\U\AppData\Roaming\Spola"), Path(r"C:\U\AppData\Local\Spola\state"),
                          Path(r"C:\U\AppData\Local\Spola\data")))

    def test_the_files_of_a_pair(self):
        paths = Paths(Path("c"), Path("s"), Path("d"))
        self.assertEqual(paths.workdir("p1"), Path("s") / "bisync" / "p1")
        self.assertEqual(paths.lock_file("p1"), Path("s") / "locks" / "p1.lock")
        self.assertEqual(paths.backup_dir("p1"), Path("d") / "backup" / "p1")
        self.assertEqual(paths.status_file, Path("s") / "status.json")
        with self.assertRaises(ValueError):
            paths.forget("../p1")


class LanguagesTest(unittest.TestCase):
    def test_a_conflict_copy_is_known_in_every_language_spola_has(self):
        catalogues = sorted(path.stem for path in (_setup.ROOT / "po").glob("*.po"))
        self.assertEqual(sorted(conflicts.LANGUAGES), catalogues)
        words = conflicts.every_translation("conflict-local")
        self.assertIn("conflict-local", words)
        if (_setup.ROOT / "locale" / "it").is_dir():        # compiled: tools/compile_po.py
            self.assertGreater(len(words), 1, words)


if __name__ == "__main__":
    unittest.main()
