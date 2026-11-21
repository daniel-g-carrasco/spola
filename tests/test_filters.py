#!/usr/bin/env python3
"""A pair's exclusions read as rclone reads them (core/filters.py): the one
answer to "is this path left out?" for the names Koofr refuses, the folder
monitor, the states of the file managers and sharing. Held against
rclone's own listings where rclone is there."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import _setup

from spola.app.rclone import DEFAULT_EXCLUDES, DEFAULT_FILTERS, clean_env, default_excluded
from spola.core.filters import Filters, Unsupported

TREE = ("report.pdf", "report.txt", "secret.txt", "a.part", "music/a.flac", "music/Some Band/1998/08.flac",
        "other/music/b.flac", "A/B/secret.txt", "A/B/C/deep.txt", "A/x.txt", "node_modules/pkg/index.js",
        "src/node_modules/pkg/index.js", ".cache/x", "sub/.cache/x", "docs/Thumbs.db", "docs/~$budget.ods",
        "docs/.~lock.report.odt#", ".Trash-1000/files/x", "keep/.spola-root", "private/report.pdf",
        "nested/private/report.pdf", "allowed/report.pdf", "photos/2026/img.jpg", "photos/2026/img.tmp")


class FiltersTest(unittest.TestCase):
    def test_the_case_of_the_music_folder(self):
        filters = Filters(DEFAULT_FILTERS + "# on this computer only\n- /music/**\n")
        self.assertTrue(filters.excluded("music/"))
        self.assertTrue(filters.excluded("music/Some Band/"))
        self.assertTrue(filters.excluded("music/Some Band/1998 - An Album/08 - \U0001F534.flac"))
        self.assertFalse(filters.excluded("other/music/"))             # anchored at the root
        self.assertFalse(filters.excluded("other/music/b.flac"))
        self.assertFalse(filters.excluded("podcast/x.mp3"))
        self.assertFalse(filters.excluded("music"))                    # as a file of that name: "/music/**" is its content
        self.assertFalse(filters.excluded(""))
        self.assertEqual(filters.unread, [])

    def test_the_first_rule_that_matches_wins_and_includes_open_their_folders(self):
        filters = Filters("+ /music/keep/**\n- /music/**\n")
        self.assertFalse(filters.excluded("music/"))                   # it leads to what is kept
        self.assertFalse(filters.excluded("music/keep/a.flac"))
        self.assertTrue(filters.excluded("music/other/a.flac"))
        self.assertTrue(filters.excluded("music/other/"))
        self.assertTrue(Filters("- /music/**\n+ /music/keep/**\n").excluded("music/keep/a.flac"))

    def test_a_folder_rule_and_a_name_at_any_depth(self):
        filters = Filters("- private/\n- *.tmp\n- /A/B/**\n")
        self.assertTrue(filters.excluded("nested/private/"))
        self.assertTrue(filters.excluded("nested/private/x.txt"))
        self.assertTrue(filters.excluded("a/b/c.tmp"))
        self.assertTrue(filters.excluded("A/B/"))
        self.assertTrue(filters.excluded("A/B/C/deep.txt"))
        self.assertFalse(filters.excluded("nested/A/B/x.txt"))
        self.assertFalse(filters.excluded("A/"))

    def test_the_default_exclusions_are_the_default_filters(self):
        filters = Filters(DEFAULT_FILTERS)
        self.assertEqual(filters.unread, [])            # every default rule is read
        for rel in TREE + tuple(f"{part}/" for path in TREE for part in [path.rsplit("/", 1)[0]] if "/" in path):
            self.assertEqual(default_excluded(rel), filters.excluded(rel), rel)
        self.assertTrue(default_excluded("node_modules/"))
        self.assertTrue(default_excluded("src/node_modules/pkg/index.js"))
        self.assertTrue(default_excluded(".cache/"))
        self.assertFalse(default_excluded("sub/.cache/x"))             # "/.cache/**": at the root only
        self.assertTrue(default_excluded("docs/~$budget.ods"))
        self.assertFalse(default_excluded("docs/budget.ods"))
        self.assertGreater(len(DEFAULT_EXCLUDES), 20)

    def test_what_is_not_read_decides_nothing_and_hides_nothing_before_it(self):
        filters = Filters("- /music/**\n- *.{tmp,bak}\n- /late/**\n")
        self.assertEqual(filters.unread, ["- *.{tmp,bak}"])
        self.assertTrue(filters.excluded("music/a.flac"))       # said by a rule before the one not read
        self.assertFalse(filters.excluded("x.tmp"))             # what the unread rule may say is not known: included
        self.assertFalse(filters.excluded("late/x.txt"))        # nor what comes after it
        self.assertIsNone(filters.includes("late/x.txt", False))
        self.assertIs(filters.includes("music/a.flac", False), False)
        for text in ("- *.[pt]df\n", "- {{regex}}\n", "- a\\?b\n", "what is this\n", "- ***\n"):
            self.assertEqual(len(Filters(text).unread), 1, text)
            with self.assertRaises(Unsupported):
                Filters(text, strict=True)
        # "!" forgets the rules so far, the unread ones too
        self.assertTrue(Filters("- *.[pt]df\n!\n- /late/**\n").excluded("late/x.txt"))

    def test_thousands_of_paths_cost_little(self):
        import time
        filters = Filters(DEFAULT_FILTERS + "- /music/**\n")
        paths = [f"dir{d}/sub{s}/file{f}.txt" for d in range(40) for s in range(25) for f in range(40)]
        start = time.monotonic()
        left_out = sum(filters.excluded(path) for path in paths)
        self.assertEqual(left_out, 0)
        self.assertLess(time.monotonic() - start, 5.0)         # 40,000 files: well under a second here

    @unittest.skipUnless(_setup.rclone_binary(), "rclone is needed to compare filter semantics")
    def test_as_rclone_lists_them(self):
        with tempfile.TemporaryDirectory(prefix="spola-filters-") as tmp:
            root = Path(tmp) / "root"
            for relative in TREE:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture")
            config = Path(tmp) / "rclone.conf"          # Windows rclone rejects NUL as a configuration path
            config.write_text("", encoding="utf-8")
            file = Path(tmp) / "filters.txt"
            for added in ("", "- /music/**\n", "- music/**\n", "- /A/B/**\n", "+ /music/Some Band/**\n- /music/**\n",
                          "- private/\n", "- /photos/*/img.tmp\n", "+ *.pdf\n- **\n", "- /secret.txt\n- *.txt\n",
                          "+ /allowed/\n- /a*/**\n", "-  report.pdf\n", "- report.???\n", "- /A/**/deep.txt\n",
                          "!\n- /docs/**\n"):
                text = DEFAULT_FILTERS + added
                with self.subTest(added=added):
                    file.write_text(text, encoding="utf-8")
                    result = subprocess.run([_setup.rclone_binary(), "lsf", "-R", str(root), "--config", str(config),
                                             "--filter-from", str(file)], capture_output=True, text=True, timeout=30,
                                            env=clean_env())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    listed = set(result.stdout.splitlines())
                    filters = Filters(text)
                    self.assertEqual(filters.unread, [])
                    for item in root.rglob("*"):
                        rel = item.relative_to(root).as_posix() + ("/" if item.is_dir() else "")
                        # rclone lists a folder it goes into; one whose every file is left out but which is
                        # itself let in is listed too, as here
                        self.assertEqual(not filters.excluded(rel), rel in listed, (added, rel))


if __name__ == "__main__":
    unittest.main()
