#!/usr/bin/env python3
"""Installing and removing the Files extension for a user."""

import tempfile
import unittest

import _setup  # noqa: F401

from spola import nautilus_integration as ni


class InstallTest(unittest.TestCase):
    def test_install_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_DATA_HOME": tmp, "HOME": tmp}
            self.assertFalse(ni.installed(env))
            written = ni.install(env)
            self.assertEqual(len(written), 1 + len(ni.EMBLEMS) + len(ni.catalogue_paths(env)))
            self.assertTrue(ni.installed(env))
            self.assertIn(ni.MARKER, ni.extension_path(env).read_text())
            for path in ni.emblem_paths(env):
                self.assertIn("<svg", path.read_text())
            removed = ni.uninstall(env)
            self.assertEqual(len(removed), len(written))
            self.assertFalse(ni.installed(env))

    def test_foreign_extension_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_DATA_HOME": tmp, "HOME": tmp}
            path = ni.extension_path(env)
            path.parent.mkdir(parents=True)
            path.write_text("# someone else's file\n")
            ni.uninstall(env)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
