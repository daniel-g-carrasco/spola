#!/usr/bin/env python3
"""The Files extension, driven with fake FileInfo objects and a fake D-Bus
proxy: never blocks, asks each folder once, refreshes when the answer lands,
forgets everything when Spola goes away."""

import importlib.util
import unittest
from pathlib import Path

import _setup  # noqa: F401

from gi.repository import GLib

EXT = Path(__file__).resolve().parent.parent / "extensions" / "spola-files.py"
spec = importlib.util.spec_from_file_location("spola_nautilus", EXT)
ext = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ext)


class FakeFile:
    def __init__(self, path):
        self.uri = ext.uri_of(path)
        self.emblems = []

    def get_uri(self):
        return self.uri

    def add_emblem(self, name):
        self.emblems.append(name)


class FakeProxy:
    def __init__(self, owner=":1.5"):
        self.owner = owner
        self.calls = []
        self.replies = {}

    def get_name_owner(self):
        return self.owner

    def call(self, method, params, flags, timeout, cancellable, callback):
        self.calls.append((method, params.unpack() if params else None))
        reply = self.replies.get(method)
        callback(self, reply)

    def call_finish(self, reply):
        if reply is None:
            raise GLib.Error("no reply")
        return reply


def spin():
    ctx = GLib.MainContext.default()
    while ctx.pending():
        ctx.iteration(False)


class CacheTest(unittest.TestCase):
    def test_roots_and_folders(self):
        c = ext.StateCache()
        c.set_roots([("/h/data/work/", "p1"), ("/h/photos", "p2")])
        self.assertEqual(c.root_of("/h/data/work/a/b.txt"), "/h/data/work")
        self.assertEqual(c.root_of("/h/data/work"), "/h/data/work")
        self.assertIsNone(c.root_of("/h/data/workshop/x"))
        c.set_folder("/h/data/work/a", {"b.txt": 1, "sub": 3})
        self.assertEqual(c.state("/h/data/work/a/b.txt"), 1)
        self.assertTrue(c.folder_known("/h/data/work/a"))
        self.assertEqual(c.apply({"/h/data/work/a/b.txt": 2, "/h/data/work/elsewhere/x": 4, "/h/data/work": 2}),
                         ["/h/data/work/a/b.txt", "/h/data/work"])
        self.assertEqual(c.state("/h/data/work/a/b.txt"), 2)
        self.assertIsNone(c.state("/h/data/work/elsewhere/x"))      # never shown: not cached
        gone = c.drop_root("/h/data/work")
        self.assertIn("/h/data/work/a/b.txt", gone)
        self.assertFalse(c.folder_known("/h/data/work/a"))

    def test_cache_is_bounded(self):
        c = ext.StateCache()
        for i in range(ext.MAX_FOLDERS + 10):
            c.set_folder(f"/r/f{i}", {"x": 1})
        self.assertEqual(len(c.folders), ext.MAX_FOLDERS)
        self.assertIsNone(c.state("/r/f0/x"))
        self.assertEqual(c.state(f"/r/f{ext.MAX_FOLDERS + 9}/x"), 1)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = ext.SpolaProvider.__new__(ext.SpolaProvider)
        self.provider.cache = ext.StateCache()
        self.provider.proxy = FakeProxy()
        self.provider._pending_folders = set()
        self.provider._schedule = 0
        self.refreshed = []
        self.provider._refresh = lambda paths: self.refreshed.extend(paths)
        self.provider.cache.set_roots([("/h/data/work", "p1")])

    def test_miss_then_hit(self):
        f = FakeFile("/h/data/work/a/doc.odt")
        self.provider.proxy.replies["GetFolderStates"] = GLib.Variant("(a{su})", ({"doc.odt": 1, "new.txt": 3},))
        self.provider.update_file_info(f)
        self.assertEqual(f.emblems, [])                               # never waits
        spin()                                                        # the idle fetch runs
        self.assertEqual(self.provider.proxy.calls, [("GetFolderStates", (ext.uri_of("/h/data/work/a"),))])
        self.assertIn("/h/data/work/a/doc.odt", self.refreshed)
        self.provider.update_file_info(f)
        self.assertEqual(f.emblems, ["spola-synced-symbolic"])
        g = FakeFile("/h/data/work/a/new.txt")
        self.provider.update_file_info(g)
        self.assertEqual(g.emblems, ["spola-pending-symbolic"])
        # the folder is asked once, even for many files
        for _ in range(5):
            self.provider.update_file_info(FakeFile("/h/data/work/a/other.txt"))
        spin()
        self.assertEqual(len(self.provider.proxy.calls), 1)

    def test_roots_get_their_own_state(self):
        root = "/h/data/work"
        self.provider.proxy.replies["GetStates"] = GLib.Variant("(a{su})", ({ext.uri_of(root): 3},))
        f = FakeFile(root)
        self.provider.update_file_info(f)                 # seen from /h/data: a root, nobody's child
        self.assertEqual(f.emblems, [])
        self.assertEqual(self.provider.proxy.calls, [("GetStates", ([ext.uri_of(root)],))])
        self.assertIn(root, self.refreshed)
        self.provider.update_file_info(f)
        self.assertEqual(f.emblems, ["spola-pending-symbolic"])
        # asked once more only after a RootChanged
        self.provider.update_file_info(FakeFile(root))
        self.assertEqual(len(self.provider.proxy.calls), 1)
        self.provider._on_signal(None, None, "RootChanged", GLib.Variant("(s)", (ext.uri_of(root),)))
        self.assertEqual(len(self.provider.proxy.calls), 2)
        # and when the roots arrive
        self.provider._roots_ready(GLib.Variant("(a(ss))", ([(ext.uri_of(root), "p1")],)))
        self.assertEqual(self.provider.proxy.calls[-1][0], "GetStates")

    def test_outside_roots_and_no_spola(self):
        f = FakeFile("/h/elsewhere/x.txt")
        self.provider.update_file_info(f)
        spin()
        self.assertEqual(self.provider.proxy.calls, [])
        self.provider.proxy.owner = None
        self.provider.update_file_info(FakeFile("/h/data/work/y.txt"))
        spin()
        self.assertEqual(self.provider.proxy.calls, [])             # not even asked

    def test_files_seen_while_spola_was_away_are_asked_again(self):
        # Spola quits: the roots go, Files asks again and gets no emblem
        self.provider.proxy.owner = None
        self.provider._owner_changed()
        bare = FakeFile("/h/data/work/a/doc.odt")
        self.provider.update_file_info(bare)
        self.provider.update_file_info(FakeFile("/h/elsewhere/x.txt"))
        self.assertEqual(bare.emblems, [])
        # Spola is back: the files Files has were left without emblem, so they are refreshed
        self.refreshed.clear()
        self.provider.proxy.owner = ":1.9"
        self.provider._roots_ready(GLib.Variant("(a(ss))", ([(ext.uri_of("/h/data/work"), "p1")],)))
        self.assertIn("/h/data/work/a/doc.odt", self.refreshed)
        self.assertNotIn("/h/elsewhere/x.txt", self.refreshed)

    def test_roots_are_asked_again_when_spola_is_still_starting(self):
        self.provider._roots_retry = 0
        timers = []
        real = ext.GLib.timeout_add_seconds
        ext.GLib.timeout_add_seconds = lambda delay, fn: timers.append((delay, fn)) or 1
        try:
            self.provider._roots_ready(None)                # the name is there, the object not yet
            self.assertEqual([d for d, _f in timers], [1])
            self.provider.proxy.replies["GetRoots"] = GLib.Variant("(a(ss))", ([(ext.uri_of("/h/data/work"), "p1")],))
            timers[0][1]()
            self.assertEqual(self.provider.proxy.calls[0][0], "GetRoots")
            self.assertEqual(self.provider._roots_retry, 0)
            # gone for good: no endless retries
            for _ in range(10):
                self.provider._roots_ready(None)
            self.assertEqual([d for d, _f in timers][1:], list(ext.ROOTS_RETRY_S))
        finally:
            ext.GLib.timeout_add_seconds = real

    def test_seen_is_bounded(self):
        cache = ext.StateCache()
        for i in range(ext.MAX_SEEN + 10):
            cache.saw(f"/h/f{i}")
        self.assertEqual(len(cache.seen), ext.MAX_SEEN)
        self.assertNotIn("/h/f0", cache.seen)

    def test_signals(self):
        self.provider.cache.set_folder("/h/data/work/a", {"doc.odt": 1})
        self.provider._on_signal(None, None, "StatesChanged",
                                 GLib.Variant("(a{su})", ({ext.uri_of("/h/data/work/a/doc.odt"): 2},)))
        self.assertEqual(self.provider.cache.state("/h/data/work/a/doc.odt"), 2)
        self.assertEqual(self.refreshed, ["/h/data/work/a/doc.odt"])
        self.provider._on_signal(None, None, "RootChanged", GLib.Variant("(s)", (ext.uri_of("/h/data/work"),)))
        self.assertIsNone(self.provider.cache.state("/h/data/work/a/doc.odt"))

    def test_menus_only_inside_roots(self):
        self.assertEqual(self.provider._menu_items([FakeFile("/h/elsewhere/x")], False), [])
        items = self.provider._menu_items([FakeFile("/h/data/work/a")], False) if ext.FM else []
        if ext.FM:
            self.assertEqual([i.props.name for i in items], ["Spola::SyncNow", "Spola::OpenOnKoofr", "Spola::ShareLink"])
            two = self.provider._menu_items([FakeFile("/h/data/work/a"), FakeFile("/h/data/work/b")], False)
            self.assertEqual([i.props.name for i in two], ["Spola::SyncNow"])

    def test_menu_signatures_of_every_file_manager(self):
        # Nautilus 4 passes (files) and (folder); Nemo and Caja (window, files) and (window, folder)
        if not ext.FM:
            self.skipTest("no file manager bindings here")
        files = [FakeFile("/h/data/work/a")]
        self.assertEqual(len(self.provider.get_file_items(files)), len(self.provider.get_file_items(None, files)))
        self.assertEqual(len(self.provider.get_background_items(files[0])),
                         len(self.provider.get_background_items(None, files[0])))


if __name__ == "__main__":
    unittest.main()
