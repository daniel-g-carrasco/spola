#!/usr/bin/env python3
"""Preflight checks: which folders may be synced, overlaps, Koofr Desktop,
names Koofr refuses, and the root identity guard (unmounted drive)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.config import Config, Pair
from spola.preflight import (
    check_koofr_desktop, check_local_root, check_overlap, check_root_identity,
    root_identity, scan_names,
)


def codes(problems):
    return [p.code for p in problems]


class LocalRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        for d in ("data/work", ".config/foo", ".var/app/x", ".ssh", "Documents"):
            (self.home / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, rel):
        return codes(check_local_root(str(self.home / rel) if rel is not None else "/", str(self.home)))

    def test_folders_in_home_are_fine(self):
        self.assertEqual(self.check("data/work"), [])
        self.assertEqual(self.check("Documents"), [])

    def test_home_and_its_parents_are_refused(self):
        self.assertEqual(self.check(""), ["too-broad"])
        self.assertEqual(codes(check_local_root(str(self.home.parent), str(self.home))), ["too-broad"])
        self.assertEqual(codes(check_local_root("/", str(self.home))), ["too-broad"])

    def test_protected_trees_are_refused(self):
        for rel in (".config", ".config/foo", ".var/app/x", ".ssh"):
            self.assertEqual(self.check(rel), ["protected"], rel)

    def test_system_folders_are_refused(self):
        self.assertEqual(codes(check_local_root("/usr", str(self.home))), ["outside-home"])
        self.assertEqual(codes(check_local_root("/etc", str(self.home))), ["outside-home"])

    def test_missing_relative_and_symlink(self):
        self.assertEqual(self.check("nope"), ["missing"])
        self.assertEqual(codes(check_local_root("data/work", str(self.home))), ["not-absolute"])
        os.symlink(self.home / "data", self.home / "link")
        self.assertEqual(self.check("link"), ["symlink-root"])


class OverlapTest(unittest.TestCase):
    def test_nested_pairs_are_refused_on_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a/b").mkdir(parents=True)
            (Path(tmp) / "c").mkdir()
            cfg = Config(pairs=[Pair("p1", f"{tmp}/a", "/data")])
            self.assertEqual(codes(check_overlap(f"{tmp}/a/b", "/other", cfg)), ["local-overlap"])
            self.assertEqual(codes(check_overlap(f"{tmp}/c", "/data/work", cfg)), ["remote-overlap"])
            self.assertEqual(codes(check_overlap(f"{tmp}/c", "/", cfg)), ["remote-overlap"])
            self.assertEqual(check_overlap(f"{tmp}/c", "/database", cfg), [])
            self.assertEqual(check_overlap(f"{tmp}/a", "/data", cfg, ignore_id="p1"), [])


class KoofrDesktopTest(unittest.TestCase):
    def test_active_koofr_desktop_sync_blocks_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("data/work", "data/imports", "data/other"):
                (Path(tmp) / d).mkdir(parents=True)
            conf = Path(tmp) / "config.json"
            conf.write_text(json.dumps({"users": {"u": {"syncs": [
                {"localPath": f"{tmp}/data/work", "remotePath": "/data/work", "paused": False},
                {"localPath": f"{tmp}/data/imports", "remotePath": "/data/imports", "paused": True},
            ]}}}))
            self.assertEqual(codes(check_koofr_desktop(f"{tmp}/data/work", conf)), ["koofr-desktop"])
            self.assertEqual(codes(check_koofr_desktop(f"{tmp}/data", conf)), ["koofr-desktop"])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/imports", conf), [])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/other", conf), [])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/work", Path(tmp) / "missing.json"), [])


class RootIdentityTest(unittest.TestCase):
    def test_replaced_or_missing_root_stops_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pair"
            root.mkdir()
            (root / "file").write_text("x")
            dev, ino = root_identity(str(root))
            pair = Pair("p", str(root), "/x", root_dev=dev, root_ino=ino)
            self.assertEqual(check_root_identity(pair), [])
            # an unmounted drive looks like an empty folder at the same path:
            # here, the folder deleted and created again
            (root / "file").unlink()
            root.rmdir()
            self.assertEqual(codes(check_root_identity(pair)), ["root-missing"])
            root.mkdir()
            self.assertEqual(codes(check_root_identity(pair)), ["root-replaced"])


class NameScanTest(unittest.TestCase):
    def test_names_koofr_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "Report.pdf").write_text("a")
            (t / "report.PDF").write_text("b")
            (t / "foto \U0001F600.jpg").write_text("c")
            (t / ("x" * 200)).write_text("d")   # fine here, too long under a deep Koofr folder
            (t / "ok àèì.txt").write_text("e")
            (t / "sub").mkdir()
            (t / "sub" / "Report.pdf").write_text("f")   # same name, other folder: fine
            self.assertEqual(sorted(codes(scan_names(tmp))), ["case-collision", "emoji"])
            deep = "/" + "/".join(["cartella"] * 95)     # 855 characters
            self.assertEqual(sorted(codes(scan_names(tmp, remote_root=deep))),
                             ["case-collision", "emoji", "long-path"])

    def test_skip_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SyncTrash").mkdir()
            (Path(tmp) / "SyncTrash" / "a\U0001F600").write_text("x")
            self.assertEqual(scan_names(tmp, skip=lambda rel: rel.startswith("SyncTrash")), [])


if __name__ == "__main__":
    unittest.main()
