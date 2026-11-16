#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The troubleshooting text of the About window: the quick part (versions,
paths, settings), the slow part (the room the files take), the sizes said
for a person."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _setup

diagnostics = _setup.shared("app.diagnostics")
errorlog = _setup.shared("app.errorlog")
Paths = _setup.shared("app.paths").Paths


def settings(**changes):
    """What summary() reads of the settings, made up."""
    return SimpleNamespace(**{"theme": "dark", "language": "it", "check_updates": True, "notices": [], **changes})


class DiagnosticsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.paths = Paths(root / "config", root / "state", root / "data")

    def test_the_quick_part_names_the_app_the_toolkit_the_folders_and_the_settings(self):
        text = diagnostics.summary("My App", "1.2.3", settings(), self.paths, "Toolkit 4.2")
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("My App 1.2.3 ("), lines[0])
        self.assertIn("from the sources", lines[0])                 # the tests run from the sources
        self.assertEqual(lines[1], "Toolkit 4.2")
        self.assertIn(f"settings: {self.paths.config_file}", lines)
        self.assertIn(f"state: {self.paths.state}", lines)
        self.assertIn(f"data: {self.paths.data}", lines)
        self.assertIn("preferences: theme 'dark', language 'it', check updates True", lines)
        self.assertNotIn("settings file:", text)

    def test_the_errors_file_is_said_and_whether_it_is_there(self):
        path = errorlog.log_file(self.paths.state)
        line = [x for x in diagnostics.summary("My App", "0", settings(), self.paths, "").splitlines()
                if x.startswith("errors:")]
        self.assertEqual(line, [f"errors: {path} (none)"])
        path.parent.mkdir(parents=True)
        path.write_text("x", encoding="utf-8")
        line = [x for x in diagnostics.summary("My App", "0", settings(), self.paths, "").splitlines()
                if x.startswith("errors:")]
        self.assertEqual(line, [f"errors: {path}"])

    def test_what_the_settings_file_said_when_it_was_read(self):
        notice = SimpleNamespace(kind="recovered", detail="config.json.broken-1", reason="JSONDecodeError")
        text = diagnostics.summary("My App", "0", settings(notices=[notice]), self.paths, "")
        self.assertIn("settings file: recovered config.json.broken-1 JSONDecodeError", text)

    def test_the_slow_part_counts_the_files_of_each_folder(self):
        (self.paths.state / "logs").mkdir(parents=True)
        (self.paths.state / "logs" / "a.log").write_text("x" * 2500, encoding="utf-8")
        (self.paths.state / "window.json").write_text("{}", encoding="utf-8")
        slow = diagnostics.storage(self.paths)
        self.assertIn("state: 2 files, 2.5 kB", slow)
        self.assertIn("data: none", slow)
        self.assertIn("python: ", slow)
        self.assertEqual(diagnostics.tree_size(self.paths.state), (2, 2502))
        self.assertEqual(diagnostics.tree_size(self.paths.data), (0, 0))          # not there: nothing, no error

    def test_sizes_for_a_person(self):
        self.assertEqual(diagnostics.human(0), "0 B")
        self.assertEqual(diagnostics.human(999), "999 B")
        self.assertEqual(diagnostics.human(1500), "1.5 kB")
        self.assertEqual(diagnostics.human(73 * 10**9), "73.0 GB")
        self.assertEqual(diagnostics.human(5 * 10**15), "5000.0 TB")
        self.assertEqual(diagnostics.human(None), "?")


if __name__ == "__main__":
    unittest.main()
