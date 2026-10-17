#!/usr/bin/env python3
"""bisync listings (format pinned to the shipped rclone), config file,
backup pruning."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock
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

    def test_counts_of_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            wd = Path(tmp)
            text = (FIXTURES / "listing.path1.lst").read_text(encoding="utf-8")
            (wd / "a..b.path1.lst").write_text(text, encoding="utf-8")
            self.assertIsNone(listings.counts(wd))                           # one side only
            (wd / "a..b.path2.lst").write_text(text + '-        1 - - 2026-10-10T12:52:41.508231189+0000 "x"\n',
                                               encoding="utf-8")
            n1, n2 = listings.counts(wd)
            self.assertEqual((n1, n2, listings.count(wd)), (n1, n1 + 1, n1))

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
            self.assertEqual(sorted(os.listdir(path.parent)), ["config.json", "config.json.bak"])   # no temp leftovers
            # what a newer Spola wrote survives a round trip through this one
            save(again, path)
            data = json.loads(path.read_text())
            self.assertEqual(data["future_key"], 1)
            self.assertEqual(data["pairs"][0]["future_pair_key"], 2)
            self.assertEqual(data["version"], 1)
            self.assertNotIn("extra", data)
            self.assertNotIn("notices", data)
            self.assertNotIn("extra", data["pairs"][0])

    def test_the_previous_save_is_kept_and_used_when_the_file_is_damaged(self):
        from spola.config import backup_file
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config(user="me@example.com", pairs=[Pair("abc", "/h/d", "/d")])
            save(cfg, path)
            cfg.pairs.append(Pair("def", "/h/e", "/e"))
            save(cfg, path)
            self.assertEqual(len(load(backup_file(path)).pairs), 1)      # the save before the last
            self.assertEqual(load(path).notices, [])
            path.write_text('{"user": "me@example.com", "pairs": [{"id": "abc", ')     # a crash mid-write elsewhere
            again = load(path)
            self.assertEqual([p.id for p in again.pairs], ["abc"])
            self.assertEqual(again.notices[0].kind, "recovered")
            aside = Path(again.notices[0].detail)
            self.assertTrue(aside.name.startswith("config.json.broken-"), aside)
            self.assertIn('"id": "abc", ', aside.read_text())
            self.assertIn("JSONDecodeError", again.notices[0].reason)
            self.assertTrue(path.exists())                               # written back at once
            self.assertEqual(load(path).notices, [])
            # the file gone, the previous save stands in
            path.unlink()
            again = load(path)
            self.assertEqual([p.id for p in again.pairs], ["abc"])
            self.assertEqual(again.notices[0].kind, "missing")
            self.assertTrue(path.exists())

    def test_damaged_with_nothing_to_recover_starts_anew_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("[1, 2]")
            cfg = load(path)
            self.assertEqual(cfg.pairs, [])
            self.assertTrue(cfg.instance_id)
            self.assertEqual(cfg.notices[0].kind, "fresh")
            self.assertEqual(Path(cfg.notices[0].detail).read_text(), "[1, 2]")
            self.assertEqual(sorted(f for f in os.listdir(tmp) if not f.startswith("config.json.broken")),
                             ["config.json"])
            save(cfg, path)                                             # now there is a .bak to fall back on
            # a value of the wrong kind is damage too, not a crash later on
            for bad in ('{"poll_minutes": "10"}', '{"paused_all": 1}', '{"pairs": [{"id": 3, "local": "/a", "remote": "/b"}]}',
                        '{"pairs": [{"local": "/a", "remote": "/b"}]}', '{"pairs": {}}', '{"version": "2"}'):
                path.write_text(bad)
                self.assertEqual(load(path).notices[0].kind, "recovered", bad)

    def test_format_versions(self):
        from spola import config as config_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            # a newer format: read for what is known, written back whole, said
            path.write_text(json.dumps({"version": 7, "user": "u", "brand_new": True,
                                        "pairs": [{"id": "abc", "local": "/a", "remote": "/b", "shiny": 1}]}))
            cfg = load(path)
            self.assertEqual(cfg.user, "u")
            self.assertEqual([n.kind for n in cfg.notices], ["newer"])
            self.assertEqual(cfg.notices[0].detail, "7")
            save(cfg, path)
            data = json.loads(path.read_text())
            self.assertEqual((data["brand_new"], data["pairs"][0]["shiny"], data["version"]), (True, 1, 1))
            # an older format goes through the migration steps, one by one
            path.write_text(json.dumps({"version": 0, "old_name": "u", "pairs": []}))
            with mock.patch.dict(config_mod.MIGRATIONS, {0: lambda d: {**d, "user": d.pop("old_name")}}):
                cfg = load(path)
            self.assertEqual(cfg.user, "u")
            self.assertEqual(cfg.notices, [])
            path.write_text(json.dumps({"version": 0, "pairs": []}))
            self.assertEqual(load(path).notices[0].kind, "recovered")   # no step for it: as damaged

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
