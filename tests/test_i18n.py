#!/usr/bin/env python3
"""The language of the interface: the variables of the desktop, or on
Windows (which sets none) the language of its own interface."""

import os
import unittest
from unittest import mock

import _setup

from spola import i18n

VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


@unittest.skipUnless((_setup.ROOT / "locale" / "it" / "LC_MESSAGES" / "spola.mo").exists(),
                     "tools/compile_po.py first")
class LanguageTest(unittest.TestCase):
    def setUp(self):
        saved = {var: os.environ.pop(var, None) for var in VARS}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) for k, v in saved.items() if v is not None])
        before = i18n._translation        # the other tests read the source strings
        self.addCleanup(lambda: setattr(i18n, "_translation", before))

    def test_the_variables_first(self):
        os.environ["LANG"] = "it_IT.UTF-8"
        with mock.patch.object(i18n, "_system_languages", return_value=["en_US"]):
            i18n.setup()
        self.assertEqual(i18n._("Add Folder"), "Aggiungi cartella")

    def test_without_variables_the_system_language(self):
        with mock.patch.object(i18n, "_system_languages", return_value=["it_IT"]):
            i18n.setup()
        self.assertEqual(i18n._("Add Folder"), "Aggiungi cartella")
        with mock.patch.object(i18n, "_system_languages", return_value=["en_US"]):
            i18n.setup()
        self.assertEqual(i18n._("Add Folder"), "Add Folder")

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_interface_language(self):
        languages = i18n._system_languages()
        self.assertEqual(len(languages), 1)
        self.assertRegex(languages[0], r"^[a-z]{2,3}(_[A-Z]{2})?")


if __name__ == "__main__":
    unittest.main()
