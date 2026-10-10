#!/usr/bin/env python3
"""The autostart entry outside the Flatpak: written, removed, never touching
a file Spola did not write."""

import tempfile
import unittest

import _setup  # noqa: F401

from spola import autostart


class AutostartTest(unittest.TestCase):
    def test_write_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "HOME": tmp}
            path = autostart.autostart_file(env)
            autostart.apply(True, None, env)
            text = path.read_text()
            self.assertIn("--background", text)
            self.assertIn("Exec=", text)
            self.assertIn(autostart.MARKER, text)
            autostart.apply(False, None, env)
            self.assertFalse(path.exists())

    def test_foreign_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_CONFIG_HOME": tmp, "HOME": tmp}
            path = autostart.autostart_file(env)
            path.parent.mkdir(parents=True)
            path.write_text("[Desktop Entry]\nExec=something-else\n")
            autostart.apply(False, None, env)
            self.assertTrue(path.exists())

    def test_quoting(self):
        self.assertIn('Exec="/my dir/spola" --background', autostart.desktop_entry("/my dir/spola"))
        self.assertIn("Exec=/usr/bin/spola --background", autostart.desktop_entry("/usr/bin/spola"))


if __name__ == "__main__":
    unittest.main()
