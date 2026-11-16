# SPDX-License-Identifier: GPL-3.0-or-later
"""The language of the interface (i18n.py): the preference first, then the
variables of the desktop, or on Windows (which sets none) the language of
its own interface; plurals, contexts, numbers in the language of the
interface, and the language given to the processes an app starts.

The catalogue is one of the tests' own, written in a temporary checkout:
an app's own strings are tested by the app."""

import locale
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup

i18n = _setup.shared("i18n")

DOMAIN = "my-app"
VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
SHARED = "MY_APP_WINDOW_LANGUAGE"
HEADER = ("Language: it\nContent-Type: text/plain; charset=UTF-8\n"
          "Plural-Forms: nplurals=2; plural=(n != 1);\n")
ITALIAN = {
    "": HEADER,
    "Preferences": "Preferenze",
    "{n} file in “{name}”\x00{n} files in “{name}”":
        "{n} file in «{name}»\x00{n} file in «{name}»",
    "menu\x04Open": "Apri…",
    "Open": "Apri",
}


def write_mo(path: Path, messages: dict[str, str]) -> None:
    """A GNU catalogue (.mo) as msgfmt writes it, without its hash table."""
    keys = sorted(messages)
    ids = [key.encode("utf-8") for key in keys]
    strs = [messages[key].encode("utf-8") for key in keys]
    start = 7 * 4 + 16 * len(keys)
    offsets, blob = [], b""
    for data in ids + strs:
        offsets.append((len(data), start + len(blob)))
        blob += data + b"\x00"
    head = struct.pack("<7I", 0x950412DE, 0, len(keys), 7 * 4, 7 * 4 + 8 * len(keys), 0, start)
    table = b"".join(struct.pack("<2I", *pair) for pair in offsets)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + table + blob)


class CatalogueTestCase(unittest.TestCase):
    """A checkout with an Italian catalogue; the module's state and the
    variables put back after each test."""

    def setUp(self):
        saved = {var: os.environ.pop(var, None) for var in (*VARS, SHARED)}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        before = i18n._translation, i18n._domain, i18n._checkout       # the other tests read the source strings
        self.addCleanup(lambda: (setattr(i18n, "_translation", before[0]), setattr(i18n, "_domain", before[1]),
                                 setattr(i18n, "_checkout", before[2])))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.checkout = Path(tmp.name)
        write_mo(self.checkout / "locale" / "it" / "LC_MESSAGES" / f"{DOMAIN}.mo", ITALIAN)


class LanguageTest(CatalogueTestCase):
    def test_the_variables_first(self):
        os.environ["LANG"] = "it_IT.UTF-8"
        with mock.patch.object(i18n, "_system_languages", return_value=["en_US"]):
            i18n.setup(DOMAIN, self.checkout)
        self.assertEqual(i18n._("Preferences"), "Preferenze")

    def test_the_preference_over_the_variables(self):
        os.environ["LANG"] = "it_IT.UTF-8"
        i18n.setup(DOMAIN, self.checkout, "en")
        self.assertEqual(i18n._("Preferences"), "Preferences")
        i18n.use("it")
        self.assertEqual(i18n._("Preferences"), "Preferenze")

    def test_without_variables_the_system_language(self):
        with mock.patch.object(i18n, "_system_languages", return_value=["it_IT"]):
            i18n.setup(DOMAIN, self.checkout)
        self.assertEqual(i18n._("Preferences"), "Preferenze")
        with mock.patch.object(i18n, "_system_languages", return_value=["en_US"]):
            i18n.use()
        self.assertEqual(i18n._("Preferences"), "Preferences")

    def test_plurals_and_contexts(self):
        i18n.setup(DOMAIN, self.checkout, "it")
        one, many = "{n} file in “{name}”", "{n} files in “{name}”"
        self.assertEqual(i18n.ngettext(one, many, 2), "{n} file in «{name}»")
        self.assertEqual(i18n.pgettext("menu", "Open"), "Apri…")
        self.assertEqual(i18n._("Open"), "Apri")

    def test_one_language_whatever_the_interface_s(self):
        i18n.setup(DOMAIN, self.checkout, "en")
        self.assertEqual(i18n.translation_for("it").gettext("Preferences"), "Preferenze")
        self.assertEqual(i18n.translation_for("en").gettext("Preferences"), "Preferences")
        self.assertEqual(i18n._("Preferences"), "Preferences")

    def test_a_string_marked_for_later_is_itself(self):
        i18n.setup(DOMAIN, self.checkout, "it")
        names = (i18n.N_("Preferences"),)
        self.assertEqual(names, ("Preferences",))
        self.assertEqual(i18n._(names[0]), "Preferenze")


class NumberTest(CatalogueTestCase):
    def test_the_language_of_the_catalogue_loaded(self):
        i18n.setup(DOMAIN, self.checkout, "it")
        self.assertEqual(i18n.language(), "it")
        i18n.setup(DOMAIN, self.checkout, "en")                 # no English catalogue: the source strings
        self.assertEqual(i18n.language(), "en")

    def test_numbers_in_the_language_of_the_interface(self):
        i18n.setup(DOMAIN, self.checkout, "it")
        self.assertEqual(i18n.number(3.5791), "3,58")
        self.assertEqual(i18n.number(-11.94, 1, sign=True), "-11,9")
        self.assertEqual(i18n.number(3.5, 1, sign=True), "+3,5")
        self.assertEqual(i18n.number(12345.6, 1, grouping=True), "12.345,6")
        self.assertEqual([i18n.number(v, None) for v in (450.0, 13.5, 0.045)], ["450", "13,5", "0,045"])
        self.assertEqual(i18n.number(7, 0), "7")
        i18n.setup(DOMAIN, self.checkout, "en")
        self.assertEqual(i18n.number(3.5791), "3.58")
        self.assertEqual(i18n.number(12345.6, 1, grouping=True), "12,345.6")


class ChildProcessTest(CatalogueTestCase):
    def test_the_language_is_given_to_the_children(self):
        i18n.setup(DOMAIN, self.checkout, "it")
        i18n.share_with_children(SHARED)
        self.assertEqual(os.environ[SHARED], "it")
        i18n.setup(DOMAIN, self.checkout, "en")                 # as a child starts
        i18n.setup_as_child(DOMAIN, SHARED, self.checkout)
        self.assertEqual(i18n._("Preferences"), "Preferenze")

    def test_a_child_started_on_its_own_takes_its_default(self):
        self.assertEqual(i18n.inherited(SHARED), "")
        self.assertEqual(i18n.inherited(SHARED, "it"), "it")
        os.environ[SHARED] = "en"
        self.assertEqual(i18n.inherited(SHARED, "it"), "en")
        os.environ.pop(SHARED)
        i18n.setup_as_child(DOMAIN, SHARED, self.checkout, default="it")
        self.assertEqual(i18n._("Preferences"), "Preferenze")


class WindowsLanguageTest(unittest.TestCase):
    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_interface_language(self):
        languages = i18n._system_languages()
        self.assertEqual(len(languages), 1)
        self.assertRegex(languages[0], r"^[a-z]{2,3}(_[A-Z]{2})?")

    def test_counts_as_the_locale_writes_them(self):
        before = locale.setlocale(locale.LC_NUMERIC)
        self.addCleanup(locale.setlocale, locale.LC_NUMERIC, before)
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.assertEqual(i18n.count(1200), "1200")                      # the C locale groups nothing
        for name in ("it_IT.UTF-8", "it_IT.utf8", "it-IT", "Italian_Italy.1252"):
            try:
                locale.setlocale(locale.LC_NUMERIC, name)
                break
            except locale.Error:
                continue
        else:
            self.skipTest("no Italian locale on this machine: the grouping is not tried")
        sep = locale.localeconv()["thousands_sep"]          # "." or a space: the system's tables differ (Windows)
        self.assertTrue(sep)
        self.assertEqual((i18n.count(1200), i18n.count(1234567)), (f"1{sep}200", f"1{sep}234{sep}567"))


if __name__ == "__main__":
    unittest.main()
