#!/usr/bin/env python3
"""The autostart entry outside the Flatpak: written, removed, never touching
a file Spola did not write."""

import tempfile
import unittest

import _setup

from spola import autostart


@unittest.skipIf(_setup.WINDOWS, "the .desktop entry is Linux only (Windows: the Run key, below)")
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


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class WindowsTest(unittest.TestCase):
    """The Run key and the Credential Manager, for real (a CI runner)."""

    def test_run_key(self):
        from spola import win32
        before = win32.autostart_command()
        self.addCleanup(win32.set_autostart, before)
        autostart.apply(True)
        self.assertIn("--background", win32.autostart_command())
        autostart.apply(False)
        self.assertIsNone(win32.autostart_command())

    def test_credential_manager(self):
        from spola import secrets
        user = "spola-test@example.invalid"
        self.addCleanup(secrets.clear, user)
        self.assertIsNone(secrets.lookup(user))
        secrets.store(user, "pàssword \U0001F511")
        self.assertEqual(secrets.lookup(user), "pàssword \U0001F511")
        secrets.store(user, "another")
        self.assertEqual(secrets.lookup(user), "another")
        secrets.clear(user)
        self.assertIsNone(secrets.lookup(user))
        secrets.clear(user)                 # twice: nothing to remove, no error

