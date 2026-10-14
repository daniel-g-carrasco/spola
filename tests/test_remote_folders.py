#!/usr/bin/env python3
"""Choosing the Koofr folder of a new pair: listing one level at a time,
Koofr's case-insensitive names, which folders cannot be chosen, the
suggestions, and the folders created only when needed."""

import json
import os
import tempfile
import unittest
from unittest import mock

import _setup  # noqa: F401

from spola import koofr_api
from spola.config import Pair
from spola.remote_folders import (CachedFolders, LocalFolders, blocked, ensure, resolve, suggestions)


class Counting(LocalFolders):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []

    def list(self, path):
        self.calls.append(path)
        return super().list(path)


class RemoteFoldersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.remote = os.path.join(self.tmp.name, "remote")
        for d in ("Data/Work", "Data/Media/Photos", "Archive/Music", "Music", "Old/Deep/Music"):
            os.makedirs(os.path.join(self.remote, d))
        open(os.path.join(self.remote, "Data", "a-file.txt"), "w").close()
        for d in ("data/work", "data/music", "Music", "elsewhere/notes"):
            os.makedirs(os.path.join(self.home, d))
        self.folders = Counting(self.remote)
        self.pairs = [Pair("p1", os.path.join(self.home, "photos"), "/Data/Media/Photos")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_folders_only(self):
        self.assertEqual(self.folders.list("/"), ["Archive", "Data", "Music", "Old"])
        self.assertEqual(self.folders.list("/Data"), ["Media", "Work"])     # a-file.txt is not a folder
        self.assertEqual(self.folders.list("/missing"), [])

    def test_resolve_ignores_case_like_koofr(self):
        self.assertEqual(resolve(self.folders.list, "/data/work"), "/Data/Work")
        self.assertIsNone(resolve(self.folders.list, "/data/nothing"))

    def test_ensure_creates_only_the_missing_levels(self):
        self.assertEqual(ensure(self.folders, "/data/work"), "/Data/Work")
        self.assertEqual(sorted(os.listdir(os.path.join(self.remote, "Data"))), ["Media", "Work", "a-file.txt"])
        self.assertEqual(ensure(self.folders, "/data/New/Inner"), "/Data/New/Inner")
        self.assertTrue(os.path.isdir(os.path.join(self.remote, "Data", "New", "Inner")))

    def test_blocked(self):
        self.assertEqual(blocked("/", self.pairs)[0], "root")
        self.assertEqual(blocked("/data/media/photos", self.pairs)[0], "synced")       # same folder, other case
        self.assertEqual(blocked("/Data/Media/Photos/2026", self.pairs)[0], "synced")  # inside it
        kind, reason = blocked("/Data/Media", self.pairs)
        self.assertEqual(kind, "contains")
        self.assertIn("photos", reason)
        self.assertEqual(blocked("/Data/Work", self.pairs), ("", ""))
        self.assertEqual(blocked("/Data/Media Old", self.pairs), ("", ""))             # a prefix, not a parent

    def test_suggestions(self):
        found = suggestions(self.folders, os.path.join(self.home, "data/work"), self.pairs, home=self.home)
        self.assertEqual([(s.path, s.exists) for s in found], [("/Data/Work", True)])
        # not on Koofr in the same place: offered for creation, plus the same name elsewhere
        found = suggestions(self.folders, os.path.join(self.home, "data/music"), self.pairs, home=self.home)
        self.assertEqual([(s.path, s.exists) for s in found],
                         [("/data/music", False), ("/Archive/Music", True), ("/Music", True)])
        # (the third level, /Old/Deep/Music, is not searched)
        found = suggestions(self.folders, os.path.join(self.home, "Music"), self.pairs, home=self.home)
        self.assertEqual([s.path for s in found], ["/Music", "/Archive/Music"])

    def test_suggestions_leave_out_synced_folders(self):
        photos = os.path.join(self.home, "data/media/photos")
        os.makedirs(photos)
        found = suggestions(self.folders, photos, self.pairs, home=self.home)
        self.assertEqual(found, [])

    def test_unreachable_koofr_is_an_error_not_a_folder_to_create(self):
        class Broken:
            def list(self, path):
                raise koofr_api.ApiError("offline")
        with self.assertRaises(koofr_api.ApiError):
            suggestions(Broken(), os.path.join(self.home, "data/work"), self.pairs, home=self.home)

    def test_cache_lists_once_and_forgets_after_create(self):
        cached = CachedFolders(self.folders)
        cached.list("/Data")
        cached.list("/Data/")
        self.assertEqual(self.folders.calls, ["/Data"])
        cached.create("/Data", "Fresh")
        self.assertIn("Fresh", cached.list("/Data"))
        self.assertEqual(self.folders.calls, ["/Data", "/Data"])


class ApiTest(unittest.TestCase):
    """The two REST calls, against a fake urlopen: the right URL, method and body."""

    def test_list_and_create(self):
        requests = []

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout, context=None):
            requests.append(request)
            files = [{"name": "b", "type": "dir"}, {"name": "A", "type": "dir"}, {"name": "x.txt", "type": "file"}]
            return Response(json.dumps({"files": files}).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(koofr_api.list_folders("u", "p", "m-1", "/data work"), ["A", "b"])
            koofr_api.create_folder("u", "p", "m-1", "/data work", "New")
        get, post = requests
        self.assertEqual(get.get_method(), "GET")
        self.assertEqual(get.full_url, "https://app.koofr.net/api/v2/mounts/m-1/files/list?path=%2Fdata+work")
        self.assertEqual(post.get_method(), "POST")
        self.assertEqual(post.full_url, "https://app.koofr.net/api/v2/mounts/m-1/files/folder?path=%2Fdata+work")
        self.assertEqual(json.loads(post.data), {"name": "New"})
        self.assertTrue(post.get_header("Authorization").startswith("Basic "))


if __name__ == "__main__":
    unittest.main()
