#!/usr/bin/env python3
"""A preview's paths as a tree: folders first, their files and bytes,
chains of single folders on one line."""

import unittest

import _setup  # noqa: F401

from spola import filetree
from spola.rclone import summarize
from test_rclone import events


class TreeTest(unittest.TestCase):
    def test_folders_files_and_sizes(self):
        base = "work/10-projects/26149 - Vantage DC"
        entries = [(f"{base}/MXP21/a.pdf", "", ""), (f"{base}/MXP21/b.pdf", "", ""),
                   (f"{base}/MXP22/WS4/c.pdf", "", ""), (f"{base}/MXP22", "", ""),     # the folder itself, deleted too
                   ("readme.md", "", ""), (f"{base}/Empty", "", "")]
        sizes = {f"{base}/MXP21/a.pdf": 100, f"{base}/MXP21/b.pdf": 50, f"{base}/MXP22/WS4/c.pdf": 7, "readme.md": 3}
        top = filetree.build(entries, sizes, folders={f"{base}/Empty"})
        self.assertEqual([(n.name, n.folder, n.files, n.size) for n in top],
                         [("work / 10-projects / 26149 - Vantage DC", True, 3, 157), ("readme.md", False, 1, 3)])
        vantage = top[0]
        self.assertEqual([(n.name, n.files, n.size) for n in vantage.children],
                         [("Empty", 0, 0), ("MXP21", 2, 150), ("MXP22 / WS4", 1, 7)])
        self.assertEqual(filetree.totals(top), (4, 160))

    def test_the_share_of_each_folder(self):
        # the last run had 4 files in Vantage DC (2 in MXP21) and one elsewhere;
        # the change deletes MXP21's two and one in MXP22
        base = ["p/Vantage/MXP21/a.pdf", "p/Vantage/MXP21/b.pdf", "p/Vantage/MXP22/c.pdf",
                "p/Vantage/MXP22/d.pdf", "p/Other/e.pdf"]
        entries = [("p/Vantage/MXP21/a.pdf", "", ""), ("p/Vantage/MXP21/b.pdf", "", ""),
                   ("p/Vantage/MXP22/c.pdf", "", "")]
        (top,) = filetree.build(entries, base=base)
        self.assertEqual((top.name, top.files, top.total), ("p / Vantage", 3, 4))   # the share of Vantage
        mxp21, mxp22 = top.children
        self.assertEqual(filetree.share(mxp21.files, mxp21.total), "the whole folder")
        self.assertEqual(filetree.share(mxp22.files, mxp22.total), "50% of the folder")
        self.assertEqual(filetree.share(3, filetree.whole([e[0] for e in entries], base)), "60% of the folder")
        self.assertEqual(filetree.share(1, 400), "less than 1% of the folder")
        self.assertEqual(filetree.share(399, 400), "99% of the folder")           # not 100% while one is left
        # new files count in their folder: a new folder is all new
        (new,) = filetree.build([("n/x.txt", "", ""), ("n/y.txt", "", "")], base=base)
        self.assertEqual(filetree.share(new.files, new.total), "the whole folder")
        self.assertEqual(filetree.build(entries)[0].total, 0)                        # no listing: no share

    def test_names_in_natural_order(self):
        top = filetree.build([(f"d/old-{i}.docx", "", "") for i in (10, 2, 1)] + [("d/Sub/x", "", ""), ("d/a", "", "")])
        self.assertEqual([n.name for n in top[0].children], ["Sub", "a", "old-1.docx", "old-2.docx", "old-10.docx"])

    def test_a_move_is_shown_where_it_goes(self):
        top = filetree.build([("2026/holiday-00.jpg", "From “IMG_00.jpg”", "2026/holiday-00.jpg")], {"2026/holiday-00.jpg": 9})
        (folder,) = top
        (photo,) = folder.children
        self.assertEqual((photo.name, photo.note, photo.size, photo.show), ("holiday-00.jpg", "From “IMG_00.jpg”", 9, "2026/holiday-00.jpg"))

    def test_a_dry_run_says_the_sizes_and_the_folders(self):
        # recorded on Koofr: the reorganisation of a video folder (appendix V)
        plan = summarize(0, events("koofr-case-rename-dry-run.jsonl"),
                         "/home/user/scratch/spola-case-probe/local/", "koofr:case-rename").plan
        self.assertIn("00. Screen", plan.folders)
        self.assertEqual(plan.sizes.get("00-readme.md"), 7)
        self.assertTrue(all(size > 0 for size in plan.sizes.values()))
        top = filetree.build([(p, "", "") for p in plan.delete_remote], plan.sizes, plan.folders)
        # (the case-only rename not folded here: its folder shows among the deletions, as the raw plan has it)
        self.assertEqual([(n.name, n.folder) for n in top], [("00. Screen", True), ("screencasts", True), ("00-readme.md", False)])


if __name__ == "__main__":
    unittest.main()
