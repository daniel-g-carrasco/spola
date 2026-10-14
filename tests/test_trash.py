#!/usr/bin/env python3
"""Restoring from Koofr's trash: deletions told apart, only the chosen one
restored, in batches, what a job left behind asked again; bisync's listings
retimed byte for byte."""

import shutil
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola import koofr_api, listings, trash
from spola.trash import Entry


class FakeTrash:
    """Pages of 3, newest first; a job may "forget" some files once."""

    def __init__(self, entries, forget=()):
        self.entries = sorted(entries, key=lambda e: -e.deleted)
        self.forget = set(forget)
        self.batches = []

    def page(self, cursor):
        start = int(cursor or 0)
        page = self.entries[start:start + 3]
        return page, (str(start + 3) if start + 3 < len(self.entries) else None)

    def undelete(self, entries):
        assert entries, "an empty list restores the whole trash"
        self.batches.append([e.path for e in entries])
        paths = {e.path for e in entries}
        if not any(e.path in paths for e in self.entries):
            raise koofr_api.NotInTrash("/undelete")
        kept = [e for e in self.entries if e.path in paths and e.path in self.forget]
        self.forget -= {e.path for e in kept}
        self.entries = [e for e in self.entries if e.path not in paths] + kept
        self.entries.sort(key=lambda e: -e.deleted)
        return "job"

    def job_done(self, job_id):
        return True


def entry(path, deleted, size=10):
    return Entry("m", path, size, deleted)


class TrashTest(unittest.TestCase):
    def setUp(self):
        self.old = [entry(f"/data/a/old{i}.txt", 1_000_000 + i) for i in range(4)]        # an intended deletion
        self.big = [entry(f"/data/a/sub/f{i}.txt", 9_000_000 - i) for i in range(7)]       # the mistake
        self.other = [entry("/other/x.txt", 9_000_100)]

    def test_deletions_under_a_folder(self):
        api = FakeTrash(self.old + self.big + self.other)
        found = trash.deletions(trash.read(api, "m", "/data/a"))
        self.assertEqual([len(d.entries) for d in found], [7, 4])
        self.assertEqual(found[0].folders("/data/a"), ["sub"])
        self.assertEqual(found[0].size, 70)
        # read stops once the pages are older than asked
        self.assertEqual(len(trash.read(api, "m", "/data/a", since=8_000_000)), 7)
        self.assertEqual(trash.read(api, "m", "/DATA/A/sub", since=8_000_000)[0].mount_id, "m")   # Koofr ignores case

    def test_only_the_chosen_deletion_comes_back_and_leftovers_are_asked_again(self):
        trash.BATCH = 2
        self.addCleanup(setattr, trash, "BATCH", 500)
        api = FakeTrash(self.old + self.big, forget={"/data/a/sub/f3.txt"})
        seen = []
        result = trash.restore(api, self.big, lambda n, total: seen.append((n, total)), poll_s=0)
        self.assertEqual((result.restored, result.left), (7, []))
        self.assertEqual(sorted(e.path for e in api.entries), sorted(e.path for e in self.old))
        self.assertEqual(api.batches[-1], ["/data/a/sub/f3.txt"])          # asked again, alone
        self.assertEqual(seen[-1][1], 7)
        self.assertTrue(all(b for b in api.batches))

    def test_restored_already_and_nothing_chosen(self):
        api = FakeTrash(self.old)
        self.assertEqual(trash.restore(api, self.big, poll_s=0).left, [])      # 404: not there any more
        self.assertEqual(trash.restore(api, [], poll_s=0).restored, 0)
        self.assertEqual(api.batches[:1], [[e.path for e in self.big]])
        with self.assertRaises(ValueError):
            koofr_api.trash_undelete("u", "p", [])


class RetimeTest(unittest.TestCase):
    TEXT = ('# bisync listing v1 from 2026-10-14T12:42:47.263830648+0000\n'
            '-      556 - - 2026-10-13T18:58:07.242672436+0000 "00-readme.md"\n'
            'd       -1 - - 2026-10-13T18:42:42.945656267+0000 "archive"\n'
            '-       12 md5:abc - 2024-04-25T16:36:41.000000000+0000 "archive/con \\"virgolette\\".txt"\n')

    def test_only_the_times_change(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        lst = tmp / "x.path2.lst"
        lst.write_text(self.TEXT, encoding="utf-8")
        new = listings.bisync_time("2026-10-14T18:19:40.095+02:00")
        self.assertEqual(new, "2026-10-14T16:19:40.095000000+0000")
        self.assertEqual(listings.retime(lst, {'archive/con "virgolette".txt': new, "missing": new}), 1)
        self.assertEqual(lst.read_text(encoding="utf-8"),
                         self.TEXT.replace("2024-04-25T16:36:41.000000000+0000", new))
        lst.write_text(self.TEXT + "weird line\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            listings.retime(lst, {"00-readme.md": new})
        self.assertIn("weird", lst.read_text(encoding="utf-8"))           # left alone


if __name__ == "__main__":
    unittest.main()
