#!/usr/bin/env python3
"""bisync listings (format pinned to the shipped rclone), config file,
backup pruning."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from _setup import FIXTURES

from spola import listings
from spola.config import Config, Pair, load, normalize_remote, save
from spola.jobs import prune_backups
from spola.paths import Paths, nearest_folder


class ListingTest(unittest.TestCase):
    def test_real_listing(self):
        entries = listings.parse((FIXTURES / "listing.path1.lst").read_text())
        self.assertEqual([e.path for e in entries],
                         ["f1.txt.conflict1", "f1.txt.conflict2", "new.txt", "sub", "sub/f2.txt"])
        sub = entries[3]
        self.assertTrue(sub.is_dir)
        self.assertEqual(sub.size, -1)
        self.assertTrue(entries[0].hash.startswith("md5:"))

    def test_quoting(self):
        entries = listings.parse((FIXTURES / "listing-quoting.lst").read_text(encoding="utf-8"))
        self.assertEqual([e.path for e in entries], ['con "virgolette" e àccènti.txt', "tab\there"])
        self.assertEqual(listings.go_unquote(r'"\x41è\U0001F600"'), "Aè\U0001F600")

    def test_unknown_format_is_an_error(self):
        with self.assertRaises(ValueError):
            listings.parse("# bisync listing v2 from ...\n")
        with self.assertRaises(ValueError):
            listings.parse('# bisync listing v1 from x\nweird line "a"\n')

    def test_find_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            self.assertIsNone(listings.count(wd))
            text = (FIXTURES / "listing.path1.lst").read_text()
            (wd / "x..y.path1.lst").write_text(text)
            self.assertIsNone(listings.count(wd))
            (wd / "x..y.path2.lst").write_text(text)
            (wd / "x..y.path2.lst-old").write_text(text)
            self.assertEqual(listings.count(wd), 5)


class ConfigTest(unittest.TestCase):
    def test_roundtrip_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c" / "config.json"
            self.assertEqual(load(path).pairs, [])
            cfg = Config(user="me@example.com", pairs=[Pair("abc", "/h/d", "/d", root_dev=1, root_ino=2)])
            save(cfg, path)
            data = json.loads(path.read_text())
            data["future_key"] = 1
            data["pairs"][0]["future_pair_key"] = 2
            path.write_text(json.dumps(data))
            again = load(path)
            self.assertEqual(again.user, "me@example.com")
            self.assertEqual(again.pairs[0].root_ino, 2)
            self.assertEqual(os.listdir(path.parent), ["config.json"])   # no temp leftovers

    def test_new_preferences_have_safe_defaults(self):
        cfg = Config()
        self.assertTrue(cfg.autostart)
        self.assertEqual(cfg.conflict_language, "")
        self.assertEqual(cfg.full_check_minutes, 60)
        self.assertIsNone(cfg.bwlimit)
        cfg.upload_limit_kb = 500
        self.assertEqual(cfg.bwlimit, "500k:off")
        cfg.download_limit_kb = 2000
        self.assertEqual(cfg.bwlimit, "500k:2000k")
        cfg.upload_limit_kb = 0
        self.assertEqual(cfg.bwlimit, "off:2000k")

    def test_password_is_never_saved(self):
        self.assertNotIn("password", json.dumps(Config(user="u").to_json()).lower())

    def test_normalize_remote(self):
        self.assertEqual(normalize_remote("data/work/"), "/data/work")
        self.assertEqual(normalize_remote(""), "/")
        self.assertEqual(normalize_remote("/a//b/./c"), "/a/b/c")
        with self.assertRaises(ValueError):
            normalize_remote("/a/../b")


class NearestFolderTest(unittest.TestCase):
    def test_the_closest_folder_that_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a").mkdir()
            self.assertEqual(nearest_folder(f"{tmp}/a/file.txt"), f"{tmp}/a")
            self.assertEqual(nearest_folder(f"{tmp}/a/gone/deeper/file.txt"), f"{tmp}/a")
            self.assertEqual(nearest_folder("/no-such-folder-here/x/file"), "/")


class PruneTest(unittest.TestCase):
    def test_only_old_run_folders_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "c", Path(tmp) / "s", Path(tmp) / "d")
            root = paths.backup_dir("p")
            now = time.time()
            old = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 40 * 86400))
            new = time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 2 * 86400))
            for name in (old, new, "not-a-run"):
                (root / name).mkdir(parents=True)
                (root / name / "f").write_text("x")
            self.assertEqual(prune_backups(paths, "p", 30, now), 1)
            self.assertEqual(sorted(os.listdir(root)), sorted([new, "not-a-run"]))


if __name__ == "__main__":
    unittest.main()
