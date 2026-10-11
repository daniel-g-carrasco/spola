#!/usr/bin/env python3
"""The D-Bus service the file managers talk to: URIs and plain paths (the
menus of Dolphin and Thunar can only pass a path), and the actions that do
their whole job in Spola."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _setup  # noqa: F401

from spola.config import Pair
from spola.dbus_service import SyncService, path_for, uri_for


class FakeInvocation:
    def __init__(self):
        self.value, self.error = "unset", None

    def return_value(self, value):
        self.value = value

    def return_dbus_error(self, name, message):
        self.error = (name, message)


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "work"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "sub" / "doc.odt").write_text("x")
        pair = Pair("p1", str(self.root), "/data/work", initialized=True)
        self.synced, self.opened, self.told = [], [], []
        engine = SimpleNamespace(
            cfg=SimpleNamespace(pairs=[pair], mount_id="m-1", pair=lambda pid: pair if pid == "p1" else None),
            runtime={}, creds=None, rclone=None, paths=None,
            request_sync=lambda pid, reason: self.synced.append((pid, reason)),
            connect=lambda *args: 0)
        self.service = SyncService(engine, lambda what: None, open_url=self.opened.append,
                                   show_link=lambda name, url: None, tell=self.told.append)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, *args):
        from gi.repository import GLib
        inv = FakeInvocation()
        self.service._call(None, None, None, None, method, GLib.Variant("(s)", args), inv)
        return inv

    def test_paths_and_uris(self):
        doc = str(self.root / "sub" / "doc.odt")
        self.assertEqual(path_for(doc), doc)
        self.assertEqual(path_for(uri_for(doc)), doc)
        self.assertEqual(path_for(str(self.root) + "/sub/../sub/doc.odt"), doc)
        self.assertEqual(self.service.locate(doc)[1], "sub/doc.odt")
        self.assertIsNone(path_for("https://example.com/x"))

    def test_sync_now_from_a_menu(self):
        self.call("SyncNow", str(self.root / "sub" / "doc.odt"))
        self.assertEqual(self.synced, [("p1", "manual")])
        self.call("SyncNow", "/somewhere/else.txt")
        self.assertEqual(len(self.synced), 1)
        self.assertEqual(len(self.told), 1)                 # outside: a notification, not silence
        self.assertIn("else.txt", self.told[0])

    def test_open_on_koofr(self):
        self.call("OpenOnKoofr", str(self.root / "sub"))
        self.call("OpenOnKoofr", str(self.root / "sub" / "doc.odt"))    # a file: the folder it is in
        self.assertEqual(self.opened, ["https://app.koofr.net/app/storage/m-1?path=%2Fdata%2Fwork%2Fsub"] * 2)
        inv = self.call("GetWebUrl", uri_for(str(self.root)))
        self.assertEqual(inv.value.unpack(), ("https://app.koofr.net/app/storage/m-1?path=%2Fdata%2Fwork",))
        self.call("OpenOnKoofr", "/somewhere/else")
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(len(self.told), 1)

    def test_share_link_needs_a_synced_folder_and_an_account(self):
        self.call("ShowShareLink", "/somewhere/else")
        self.assertEqual(len(self.told), 1)
        inv = self.call("ShowShareLink", str(self.root / "sub" / "doc.odt"))
        self.assertIsNone(inv.value)                        # answered at once, the link comes later
        self.assertEqual(len(self.told), 2)                 # not signed in, said so
        inv = self.call("CreateShareLink", uri_for(str(self.root)))
        self.assertEqual(inv.error[0], "io.github.daniel_g_carrasco.spola.Sync1.NotSynced")


if __name__ == "__main__":
    unittest.main()
