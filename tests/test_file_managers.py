#!/usr/bin/env python3
"""Installing and removing Spola in the file managers of a user: the Python
extension for Files, Nemo and Caja, Dolphin's service menu, Thunar's custom
actions; only for the file managers that were used, never touching what is
not Spola's."""

import os
import stat
import tempfile
import unittest

if __import__("sys").platform == "win32":
    raise unittest.SkipTest("Nautilus, Nemo, Caja, Dolphin and Thunar are Linux only")
import xml.etree.ElementTree as ET
from pathlib import Path

import _setup  # noqa: F401

from spola import file_managers as fm

USER_UCA = """<?xml version="1.0" encoding="UTF-8"?>
<actions>
<action>
	<icon>utilities-terminal</icon>
	<name>Open Terminal Here</name>
	<unique-id>1-1</unique-id>
	<command>exo-open --working-directory %f --launch TerminalEmulator</command>
	<patterns>*</patterns>
	<directories/>
</action>
</actions>
"""


def uca_ids(env):
    root = ET.parse(fm._uca_file(env)).getroot()
    return [a.findtext("unique-id") for a in root.findall("action")]


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = {"HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_files_on_a_fresh_home(self):
        self.assertEqual(fm.detected(self.env), ["nautilus"])
        written = fm.install(self.env)
        self.assertEqual(len(written), 1 + len(fm.EMBLEMS) + len(fm.catalogue_paths(self.env)))
        self.assertTrue(fm.installed(self.env))
        text = fm.extension_path("nautilus", self.env).read_text()
        self.assertIn(fm.MARKER, text)
        self.assertIn('FILE_MANAGER = "Nautilus"', text)
        for path in fm.emblem_paths(self.env):
            self.assertIn("<svg", path.read_text())
        self.assertFalse(fm.extension_path("nemo", self.env).exists())
        self.assertFalse(fm.service_menu_path(self.env).exists())
        removed = fm.uninstall(self.env)
        self.assertEqual(len(removed), len(written))
        self.assertFalse(fm.installed(self.env))

    def test_every_file_manager_that_was_used(self):
        (self.home / ".config" / "nemo").mkdir(parents=True)
        (self.home / ".config" / "caja").mkdir()
        (self.home / ".config" / "dolphinrc").write_text("[General]\n")
        (self.home / ".config" / "Thunar").mkdir()
        fm._uca_file(self.env).write_text(USER_UCA)
        self.assertEqual(fm.detected(self.env), ["nautilus", "nemo", "caja", "dolphin", "thunar"])
        fm.install(self.env)
        for name, value in (("nemo", "Nemo"), ("caja", "Caja")):
            text = fm.extension_path(name, self.env).read_text()
            self.assertIn(f'FILE_MANAGER = "{value}"', text)
            compile(text, name, "exec")
        menu = fm.service_menu_path(self.env)
        self.assertTrue(menu.stat().st_mode & stat.S_IXUSR)          # Plasma 6 wants it executable
        text = menu.read_text()
        self.assertIn("Exec=gdbus call --session --dest io.github.daniel_g_carrasco.spola", text)
        self.assertIn(".Sync1.SyncNow %f", text)
        self.assertIn(".Sync1.ShowShareLink %f", text)
        self.assertIn("Name[it]=", text)
        self.assertEqual(uca_ids(self.env), ["1-1", "spola-sync-now", "spola-open-on-koofr", "spola-share-link"])
        # a newer Spola installs again at start-up: no duplicates
        fm.install(self.env)
        self.assertEqual(uca_ids(self.env), ["1-1", "spola-sync-now", "spola-open-on-koofr", "spola-share-link"])
        root = ET.parse(fm._uca_file(self.env)).getroot()
        names = [n for a in root.findall("action") for n in a.findall("name")]
        self.assertIn("it", [n.get(fm.XML_LANG) for n in names])
        fm.uninstall(self.env)
        self.assertEqual(uca_ids(self.env), ["1-1"])                 # the user's own action stays
        for name in fm.PYTHON_TARGETS:
            self.assertFalse(fm.extension_path(name, self.env).exists())
        self.assertFalse(menu.exists())

    def test_thunar_without_uca_xml_is_left_alone(self):
        # Thunar writes its default actions into uca.xml on its first start:
        # a file written before would replace them
        (self.home / ".config" / "Thunar").mkdir(parents=True)
        fm.install(self.env)
        self.assertFalse(fm._uca_file(self.env).exists())
        self.assertNotIn("thunar", fm.detected(self.env))

    def test_foreign_files_are_kept(self):
        path = fm.extension_path("nautilus", self.env)
        path.parent.mkdir(parents=True)
        path.write_text("# someone else's file\n")
        fm.uninstall(self.env)
        self.assertTrue(path.exists())

    def test_flatpak_writes_to_the_host_folders(self):
        sandbox = self.home / ".var" / "app" / "io.example"
        env = {"FLATPAK_ID": "io.example", "XDG_DATA_HOME": str(sandbox / "data"),
               "XDG_CONFIG_HOME": str(sandbox / "config"), "HOME": str(self.home)}
        self.assertEqual(fm.data_home(env), self.home / ".local" / "share")
        self.assertEqual(fm.config_home(env), self.home / ".config")
        env["HOST_XDG_DATA_HOME"] = str(self.home / "data")
        self.assertEqual(fm.data_home(env), self.home / "data")
        self.assertEqual(fm.extension_path("nautilus", env).parent.parent.parent, self.home / "data")

    def test_extension_text_for_each(self):
        for name, (_folder, _file, value) in fm.PYTHON_TARGETS.items():
            text = fm.extension_text(value)
            self.assertEqual(text.count("FILE_MANAGER = "), 1)
            compile(text, name, "exec")


if __name__ == "__main__":
    os.environ.pop("FLATPAK_ID", None)
    unittest.main()
