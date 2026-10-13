#!/usr/bin/env python3
"""How far a run is, from rclone's stats: copies, then deletions and the
folders left empty."""

import unittest

import _setup  # noqa: F401

from spola.progress import progress_text, time_left


class ProgressTest(unittest.TestCase):
    def test_copies(self):
        text, fraction = progress_text({"transfers": 3, "totalTransfers": 10, "bytes": 300, "totalBytes": 1000})
        self.assertEqual((text, fraction), ("3 of 10 files", 0.3))
        self.assertEqual(progress_text({"checks": 4200}), ("Comparing files (4200 checked)", None))
        self.assertEqual(progress_text(None), ("", None))

    def test_after_the_copies_the_deletions_and_the_empty_folders(self):
        # recorded on a reorganised folder: 1,937 copies done, then 7,468
        # deletions on Koofr for ten minutes, then 1,331 folders left empty
        done = {"transfers": 1937, "totalTransfers": 1937, "bytes": 81_000_000, "totalBytes": 81_000_000}
        self.assertEqual(progress_text({**done, "deletes": 2000}, deletes_queued=8148),
                         ("Deleting: 2000 of 8148", 2000 / 8148))
        text, fraction = progress_text({**done, "deletes": 7468, "deletedDirs": 1331}, deletes_queued=8148)
        self.assertEqual((text, fraction), ("Removing empty folders: 1331", None))
        # more deleted than announced (folders): never past the end
        self.assertEqual(progress_text({**done, "deletes": 9000}, deletes_queued=8148)[1], 1.0)
        # a run that only deletes
        self.assertEqual(progress_text({"deletes": 5}, deletes_queued=10), ("Deleting: 5 of 10", 0.5))
        # while copies go on, the copies are what counts
        self.assertEqual(progress_text({"transfers": 1, "totalTransfers": 2, "deletes": 50}, deletes_queued=100)[0],
                         "1 of 2 files")

    def test_time_left(self):
        self.assertEqual(time_left(30), "less than a minute left")
        self.assertEqual(time_left(600), "about 10 minutes left")
        self.assertEqual(time_left(None), "")


if __name__ == "__main__":
    unittest.main()
