#!/usr/bin/env python3
"""The window's groups: one per parent folder, a lone pair joins the
nearest group above it with a relative title."""

import unittest

import _setup  # noqa: F401

from spola.config import Pair
from spola.grouping import expected_remote, group_pairs

H = "/home/u"


def pairs(*locals_):
    return [Pair(f"p{i}", path, "/x") for i, path in enumerate(locals_)]


class GroupingTest(unittest.TestCase):
    def test_groups_by_parent_and_merges_lone_pairs_up(self):
        layout = pairs(f"{H}/data/work", f"{H}/data/inbox", f"{H}/data/media/video", f"{H}/data/media/audio",
                       f"{H}/data/media/photos/camera-roll", f"{H}/scratch/test", f"{H}/scratch/Elettrico")
        groups = group_pairs(layout)
        self.assertEqual([(parent, [title for _id, title in rows]) for parent, rows in groups], [
            (f"{H}/data", ["inbox", "work"]),
            (f"{H}/data/media", ["audio", "photos/camera-roll", "video"]),
            (f"{H}/scratch", ["Elettrico", "test"]),
        ])

    def test_a_lone_pair_with_no_group_above_stays_alone(self):
        groups = group_pairs(pairs(f"{H}/Documents", "/run/media/u/disk/Photos"))
        self.assertEqual([(parent, [t for _i, t in rows]) for parent, rows in groups],
                         [(H, ["Documents"]), ("/run/media/u/disk", ["Photos"])])

    def test_chains_of_lone_pairs_merge(self):
        groups = group_pairs(pairs(f"{H}/a/one", f"{H}/a/two", f"{H}/a/b/c/deep"))
        self.assertEqual([(parent, [t for _i, t in rows]) for parent, rows in groups],
                         [(f"{H}/a", ["b/c/deep", "one", "two"])])

    def test_expected_remote(self):
        self.assertEqual(expected_remote(f"{H}/data/work", H), "/data/work")
        self.assertIsNone(expected_remote("/run/media/u/disk", H))


if __name__ == "__main__":
    unittest.main()
