#!/usr/bin/env python3
"""A whole folder renamed or moved here is found, so that Spola can move it
on Koofr as a folder, with the public links that point to it; and the links
a run would remove are named. No rclone, no Koofr: folders and listings."""

import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

import _setup  # noqa: F401

from spola.app import jobs
from spola.app.config import Pair
from spola.app.preflight import CaseRename, folder_moves, news, successor
from spola.data import listings


def entry(root, rel: str) -> listings.Entry:
    """The file as a listing of rclone bisync has it."""
    st = os.stat(os.path.join(root, rel))
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(st.st_mtime_ns // 10**9))
    return listings.Entry(rel, False, st.st_size, "-", f"{whole}.{st.st_mtime_ns % 10**9:09d}+0000")


class FolderMovesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="spola-moves-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.files = ["events/2026-10-20/a.pdf", "events/2026-10-20/b.pdf", "events/2026-10-20/photos/c.jpg",
                      "events/2026-10-20/photos/d.jpg", "events/other/e.txt", "top.txt"]
        for i, rel in enumerate(self.files):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel * (i + 1))
            os.utime(path, ns=(1_700_000_000_000_000_000 + i * 10**9, 1_700_000_000_000_000_000 + i * 10**9))
        self.before = [entry(self.root, rel) for rel in self.files]

    def moves(self, koofr=None):
        found = news(str(self.root), self.before)
        return folder_moves(str(self.root), found, self.files if koofr is None else koofr)

    def test_nothing_gone_nothing_looked_at(self):
        (self.root / "new-folder").mkdir()
        found = news(str(self.root), self.before)
        self.assertFalse(found.anything_gone)
        self.assertEqual(found.new_dirs, [])        # no walk when the listing's files are all here
        self.assertEqual(self.moves(), [])

    def test_a_folder_renamed_is_one_move_of_the_topmost_folder(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/2026-11-20")
        self.assertEqual(self.moves(), [CaseRename("events/2026-10-20", "events/2026-11-20",
                                                   "events/2026-10-20", "events/2026-11-20")])

    def test_a_folder_moved_into_folders_not_there_yet(self):
        (self.root / "archive/2026").mkdir(parents=True)
        os.rename(self.root / "events/2026-10-20", self.root / "archive/2026/october")
        self.assertEqual(self.moves(), [CaseRename("events/2026-10-20", "archive/2026/october",
                                                   "events/2026-10-20", "archive/2026/october")])

    def test_koofr_is_asked_for_its_own_spelling(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/next")
        koofr = [p.replace("events/", "Events/") for p in self.files]
        self.assertEqual(self.moves(koofr), [CaseRename("events/2026-10-20", "events/next", "Events/2026-10-20",
                                                        "events/next")])

    def test_files_changed_or_removed_after_the_rename_do_not_hide_it(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/next")
        (self.root / "events/next/a.pdf").write_text("edited after the rename")
        (self.root / "events/next/photos/d.jpg").unlink()
        (self.root / "events/next/new.txt").write_text("added")
        self.assertEqual([(m.old, m.new) for m in self.moves()], [("events/2026-10-20", "events/next")])

    def test_less_than_half_of_its_files_is_not_the_same_folder(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/next")
        for rel in ("a.pdf", "b.pdf", "photos/c.jpg"):
            (self.root / "events/next" / rel).unlink()
        self.assertEqual(self.moves(), [])

    def test_not_a_move(self):
        # deleted, with no new folder
        shutil.rmtree(self.root / "events/2026-10-20")
        self.assertEqual(self.moves(), [])

    def test_a_copy_next_to_the_folder_still_here_is_not_a_move(self):
        shutil.copytree(self.root / "events/2026-10-20", self.root / "events/copy", copy_function=shutil.copy2)
        self.assertEqual(self.moves(), [])

    def test_two_places_it_could_have_gone_to_is_no_move(self):
        shutil.copytree(self.root / "events/2026-10-20", self.root / "events/one", copy_function=shutil.copy2)
        os.rename(self.root / "events/2026-10-20", self.root / "events/two")
        self.assertEqual(self.moves(), [])

    def test_something_already_there_on_koofr_is_left_to_bisync(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/next")
        self.assertEqual(self.moves(self.files + ["Events/NEXT/already.txt"]), [])
        # and a folder that Koofr does not have where it was
        self.assertEqual(self.moves(["top.txt"]), [])

    def test_a_rename_by_case_alone_is_not_taken_twice(self):
        if not _setup.case_sensitive(self.root):
            self.skipTest("a file system that does not tell the two names apart")
        os.rename(self.root / "events/other", self.root / "events/Other")
        self.assertEqual(self.moves(), [])          # Koofr has "other" there: preflight.case_renames' work

    def test_where_the_files_seem_to_be_now(self):
        os.rename(self.root / "events/2026-10-20", self.root / "events/next")
        for rel in ("a.pdf", "b.pdf", "photos/c.jpg"):
            (self.root / "events/next" / rel).unlink()
        found = news(str(self.root), self.before)
        self.assertEqual(successor(str(self.root), found, "events/2026-10-20"), "events/next")
        self.assertEqual(successor(str(self.root), found, "events/other"), "")


class LinksAtRiskTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="spola-links-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.files = ["shared/a.pdf", "shared/b.pdf", "kept/c.txt", "report.odt"]
        for i, rel in enumerate(self.files):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel * (i + 2))
        self.before = [entry(self.root, rel) for rel in self.files]
        self.pair = Pair("p1", str(self.root), "/data/Community")

    def link(self, path, lid="l1", opened=4):
        return NS(id=lid, path=path, counter=opened)

    def at_risk(self, links, planned=()):
        found = news(str(self.root), self.before)
        found.koofr_hash = {rel.casefold(): "md5:" + hashlib.md5((rel * (i + 2)).encode()).hexdigest()
                            for i, rel in enumerate(self.files)}
        return jobs.links_at_risk(self.pair, found, links, planned)

    def test_a_link_to_a_folder_deleted_here_is_at_risk(self):
        shutil.rmtree(self.root / "shared")
        risks = self.at_risk([self.link("/data/community/Shared"), self.link("/data/community/kept", "l2"),
                              self.link("/elsewhere/shared", "l3"), self.link("/data/community/never-synced", "l4")])
        self.assertEqual(risks, [jobs.LinkAtRisk("l1", "shared", True, 4, "")])

    def test_what_is_still_here_is_not_at_risk(self):
        self.assertEqual(self.at_risk([self.link("/data/community/shared"), self.link("/data/community/report.odt")]), [])

    def test_a_folder_spola_moves_on_the_server_keeps_its_links(self):
        os.rename(self.root / "shared", self.root / "shared-2026")
        links = [self.link("/data/community/shared"), self.link("/data/community/shared/a.pdf", "l2")]
        self.assertEqual([r.id for r in self.at_risk(links)], ["l1"])         # the file is moved by rclone: it follows
        self.assertEqual(self.at_risk(links, [CaseRename("shared", "shared-2026", "shared", "shared-2026")]), [])

    def test_a_folder_not_moved_names_where_its_files_are(self):
        os.rename(self.root / "shared", self.root / "shared-2026")
        self.assertEqual(self.at_risk([self.link("/data/community/shared")]),
                         [jobs.LinkAtRisk("l1", "shared", True, 4, "shared-2026")])

    def test_a_file_moved_here_keeps_its_link_and_a_file_deleted_does_not(self):
        os.rename(self.root / "report.odt", self.root / "kept/report-final.odt")
        self.assertEqual(self.at_risk([self.link("/data/community/report.odt")]), [])
        (self.root / "kept/report-final.odt").unlink()
        self.assertEqual([r.path for r in self.at_risk([self.link("/data/community/report.odt")])], ["report.odt"])

    def test_without_a_checksum_in_the_listing_koofr_is_asked_for_it(self):
        # right after a first alignment the listing of Koofr has no checksums
        os.rename(self.root / "report.odt", self.root / "kept/report-final.odt")
        found = news(str(self.root), self.before)
        asked = []
        real = "md5:" + hashlib.md5(("report.odt" * 5).encode()).hexdigest()
        risks = jobs.links_at_risk(self.pair, found, [self.link("/data/Community/report.odt")], (),
                                   lambda path: asked.append(path) or real)
        self.assertEqual((risks, asked), ([], ["/data/Community/report.odt"]))
        # another content, or Koofr not answering: the link counts as at risk
        self.assertEqual(len(jobs.links_at_risk(self.pair, found, [self.link("/data/Community/report.odt")], (),
                                                lambda path: "md5:" + "0" * 32)), 1)

        def fails(path):
            raise RuntimeError("no answer")
        self.assertEqual(len(jobs.links_at_risk(self.pair, found, [self.link("/data/Community/report.odt")], (), fails)), 1)

    def test_another_file_of_the_same_size_and_date_is_not_the_file_moved(self):
        st = os.stat(self.root / "report.odt")
        (self.root / "report.odt").unlink()
        other = self.root / "kept/other.bin"
        other.write_text("x" * st.st_size)
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual([r.path for r in self.at_risk([self.link("/data/community/report.odt")])], ["report.odt"])


class ListingsRenameTest(unittest.TestCase):
    """The folder moved on both sides, written in bisync's two listings."""

    HEAD = "# bisync listing v1 from 2026-11-13T10:00:00+0100"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="spola-lst-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def listing(self, name, paths, folders=()):
        lines = [self.HEAD]
        for path in sorted([*paths, *folders], key=lambda p: p.encode("utf-8", "surrogateescape")):
            flag, size = ("d", -1) if path in folders else ("-", len(path))
            lines.append(f"{flag} {size:>8} md5:{len(path):032x} - 2026-10-20T08:00:00.123456789+0000 "
                         f"{listings.go_quote(path)}")
        target = self.dir / name
        with open(target, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write("\n".join(lines) + "\n")
        return target

    def test_what_a_folder_held_is_where_the_folder_is_now(self):
        paths = ["a.txt", "events/2026-10-20/a.pdf", "events/2026-10-20/photos/c.jpg", "events/2026-10-200/x", "z.txt"]
        folders = ["events", "events/2026-10-20", "events/2026-10-20/photos", "events/2026-10-200"]
        first = self.listing("p.path1.lst", paths, folders)
        second = self.listing("p.path2.lst", [p.replace("events/", "Events/") for p in paths],
                              [f.replace("events", "Events") for f in folders])
        before = {e.path: e for e in listings.parse(first.read_text())}
        moved = listings.rename(first, second, [("events/2026-10-20", "events/2026-11-20")],
                                [("Events/2026-10-20", "events/2026-11-20")])
        self.assertEqual(moved, 8)          # the folder, its subfolder and two files, on each side
        after = listings.parse(first.read_text())
        self.assertEqual([e.path for e in after],
                         ["a.txt", "events", "events/2026-10-200", "events/2026-10-200/x", "events/2026-11-20",
                          "events/2026-11-20/a.pdf", "events/2026-11-20/photos", "events/2026-11-20/photos/c.jpg",
                          "z.txt"])
        was = before["events/2026-10-20/a.pdf"]
        now = next(e for e in after if e.path == "events/2026-11-20/a.pdf")
        self.assertEqual((now.size, now.hash, now.modtime, now.is_dir), (was.size, was.hash, was.modtime, False))
        self.assertIn("events/2026-11-20/a.pdf", [e.path for e in listings.parse(second.read_text())])
        self.assertTrue(first.read_text().endswith("\n"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["p.path1.lst", "p.path2.lst"])

    def test_names_that_need_quoting_come_back_as_they_were(self):
        for name in ('quote " and back\\slash', "line\nbreak\ttab", "è ö 日本 🎉", "bad-\udcff-byte", "bell\x07"):
            self.assertEqual(listings.go_unquote(listings.go_quote(name)), name)
        first = self.listing("p.path1.lst", ['old/we "said".txt'])
        second = self.listing("p.path2.lst", ['old/we "said".txt'])
        listings.rename(first, second, [("old", 'new "name"')], [("old", 'new "name"')])
        self.assertEqual([e.path for e in listings.parse(first.read_text())], ['new "name"/we "said".txt'])

    def test_a_listing_not_understood_leaves_both_alone(self):
        first = self.listing("p.path1.lst", ["old/a.txt"])
        second = self.listing("p.path2.lst", ["old/a.txt"])
        second.write_text(second.read_text() + "something else\n")
        kept = first.read_text(), second.read_text()
        with self.assertRaises(ValueError):
            listings.rename(first, second, [("old", "new")], [("old", "new")])
        self.assertEqual((first.read_text(), second.read_text()), kept)

    def test_nothing_to_move_writes_nothing(self):
        first = self.listing("p.path1.lst", ["a.txt"])
        second = self.listing("p.path2.lst", ["a.txt"])
        stamp = os.stat(first).st_mtime_ns
        self.assertEqual(listings.rename(first, second, [("old", "new")], [("old", "new")]), 0)
        self.assertEqual(os.stat(first).st_mtime_ns, stamp)


if __name__ == "__main__":
    unittest.main()
