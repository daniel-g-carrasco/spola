#!/usr/bin/env python3
"""Koofr's activity feed as a doorbell: which pairs a change concerns, our
own changes ignored, a full page treated as "everything changed"."""

import itertools
import unittest

import _setup  # noqa: F401

from spola.config import Pair
from spola.koofr_api import ACTIVITY_LIMIT, Activity, folder_url, touched_pairs, user_agent, web_url

PAIRS = [Pair("work", "/h/data/work", "/data/work"), Pair("photos", "/h/p", "/data/media/photos"),
         Pair("test", "/h/s/t", "/spola-test")]
MOUNT = "m-1"


_ids = itertools.count(1)


def act(path, new_path="", agent="Koofr web", mount=MOUNT):
    i = next(_ids)
    return Activity(str(i), 1000 + i, "file_uploaded", mount, path, new_path, agent)


class TouchedPairsTest(unittest.TestCase):
    def touched(self, *acts, instance="abcd1234"):
        return touched_pairs(list(acts), PAIRS, MOUNT, instance)

    def test_paths_map_to_pairs(self):
        self.assertEqual(self.touched(act("/data/work/a.txt")), ({"work"}, False))
        self.assertEqual(self.touched(act("/data/workshop/a.txt")), (set(), False))   # prefix, not parent
        self.assertEqual(self.touched(act("/data/media/photos")), ({"photos"}, False))
        self.assertEqual(self.touched(act("/elsewhere/x", "/spola-test/x")), ({"test"}, False))   # moved in
        self.assertEqual(self.touched(act("/data")), ({"work", "photos"}, False))     # a parent changed

    def test_own_changes_and_other_mounts_are_ignored(self):
        mine = act("/data/work/a.txt", agent=user_agent("abcd1234"))
        self.assertIn("abcd1234", mine.user_agent)
        self.assertEqual(self.touched(mine), (set(), False))
        self.assertEqual(self.touched(act("/data/work/a.txt", mount="dropbox")), (set(), False))
        # another computer running Spola has another instance id: not ignored
        self.assertEqual(self.touched(act("/data/work/a.txt", agent=user_agent("ffff0000"))), ({"work"}, False))

    def test_full_page_means_everything(self):
        acts = [act("/elsewhere/x") for _ in range(ACTIVITY_LIMIT)]
        self.assertEqual(self.touched(*acts), ({"work", "photos", "test"}, True))

    def test_web_url(self):
        self.assertEqual(web_url("m-1", "/spola-test/a b"), "https://app.koofr.net/app/storage/m-1?path=%2Fspola-test%2Fa+b")

    def test_folder_url_is_the_folder_holding_the_file(self):
        base = "https://app.koofr.net/app/storage/m-1?path="
        self.assertEqual(folder_url("m-1", "/data/imports", "Foto/2025/a.jpg"), base + "%2Fdata%2Fimports%2FFoto%2F2025")
        self.assertEqual(folder_url("m-1", "/data/imports", "a.jpg"), base + "%2Fdata%2Fimports")
        self.assertEqual(folder_url("m-1", "/", "a.jpg"), base + "%2F")
        self.assertEqual(web_url("", "/x"), "https://app.koofr.net/app/storage")


if __name__ == "__main__":
    unittest.main()
