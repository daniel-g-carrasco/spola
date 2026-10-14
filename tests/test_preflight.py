#!/usr/bin/env python3
"""Preflight checks: which folders may be synced, overlaps, Koofr Desktop,
names Koofr refuses, and the root identity guard (unmounted drive)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.config import Config, Pair
from spola.preflight import (
    CaseRename, case_renames, check_koofr_desktop, check_local_root, check_overlap, check_root_identity,
    Refused, refused_paths, refused_reason, rename_plan, root_identity, scan_names,
)
from spola.rclone import exclude_lines, filter_escape


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
            self.assertEqual(codes(check_koofr_desktop(f"{tmp}/data/work", conf, active=True)), ["koofr-desktop"])
            self.assertEqual(codes(check_koofr_desktop(f"{tmp}/data", conf, active=True)), ["koofr-desktop"])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/imports", conf, active=True), [])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/other", conf, active=True), [])
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/work", Path(tmp) / "missing.json", active=True), [])
            # Koofr Desktop stopped and not starting at login: its folders are free
            self.assertEqual(check_koofr_desktop(f"{tmp}/data/work", conf, active=False), [])

    def test_koofr_desktop_autostart_counts_as_active(self):
        from spola.preflight import koofr_desktop_active
        entry = "[Desktop Entry]\nName=Koofr\nExec=/home/u/.koofr-dist/storagegui -silent\n"
        with tempfile.TemporaryDirectory() as tmp:
            auto, logs = Path(tmp) / "autostart", Path(tmp) / "log"
            auto.mkdir()
            logs.mkdir()
            running = koofr_desktop_active(auto, logs)       # its processes: depends on this machine
            self.assertIsInstance(running, bool)
            if running:
                self.skipTest("Koofr Desktop runs on this machine")
            self.assertFalse(koofr_desktop_active(auto, logs))
            (auto / "koofr.desktop").write_text(entry)
            self.assertTrue(koofr_desktop_active(auto, logs))
            # renamed in place: systemd's xdg-autostart generator still runs it
            (auto / "koofr.desktop").rename(auto / "koofr.desktop.disabled-by-spola")
            self.assertTrue(koofr_desktop_active(auto, logs))
            # Hidden=true is how the specification turns an entry off
            (auto / "koofr.desktop.disabled-by-spola").write_text(entry + "Hidden=true\n")
            self.assertFalse(koofr_desktop_active(auto, logs))
            (auto / "koofr.desktop.disabled-by-spola").unlink()
            (auto / "kooky.desktop").write_text("[Desktop Entry]\nExec=kooky\n")
            self.assertFalse(koofr_desktop_active(auto, logs))

    def test_koofr_desktop_logs_written_lately_mean_it_runs(self):
        # inside the Flatpak the host's processes are not visible: its logs are
        from spola.preflight import KOOFR_DESKTOP_LOG_FRESH_S, koofr_desktop_active
        with tempfile.TemporaryDirectory() as tmp:
            auto, logs = Path(tmp) / "autostart", Path(tmp) / "log"
            auto.mkdir()
            logs.mkdir()
            if koofr_desktop_active(auto, logs):
                self.skipTest("Koofr Desktop runs on this machine")
            log = logs / "sync_abc.log"
            log.write_text("x")
            self.assertTrue(koofr_desktop_active(auto, logs))
            old = time.time() - KOOFR_DESKTOP_LOG_FRESH_S - 60
            os.utime(log, (old, old))
            self.assertFalse(koofr_desktop_active(auto, logs))

    def test_host_config_dir_inside_the_flatpak(self):
        from spola import preflight
        saved = {k: os.environ.get(k) for k in ("FLATPAK_ID", "XDG_CONFIG_HOME", "HOST_XDG_CONFIG_HOME")}
        try:
            os.environ["FLATPAK_ID"] = "io.example"
            os.environ["XDG_CONFIG_HOME"] = "/home/u/.var/app/io.example/config"
            os.environ.pop("HOST_XDG_CONFIG_HOME", None)
            self.assertEqual(preflight._host_config_dir(), Path.home() / ".config")
            os.environ["HOST_XDG_CONFIG_HOME"] = "/home/u/cfg"
            self.assertEqual(preflight._host_config_dir(), Path("/home/u/cfg"))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class CaseRenameTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="spola-case-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make(self, *paths):
        for rel in paths:
            path = self.root / rel
            if rel.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rel)

    def test_a_folder_renamed_by_case_alone_is_found_once(self):
        before = ["Screencasts", "Screencasts/a.mp4", "Screencasts/Old.mp4", "00. Screen", "00. Screen/b.mp4",
                  "File.txt", "keep.txt"]
        # the folder renamed, a file in it renamed too, another moved in, a file renamed
        self.make("screencasts/a.mp4", "screencasts/old.mp4", "screencasts/b.mp4", "file.txt", "keep.txt")
        self.assertEqual(case_renames(str(self.root), before, before),
                         [CaseRename("File.txt", "file.txt", "File.txt"),
                          CaseRename("Screencasts", "screencasts", "Screencasts"),
                          # in the folder, after it: Koofr's name by then
                          CaseRename("Screencasts/Old.mp4", "screencasts/old.mp4", "screencasts/Old.mp4")])

    def test_what_is_not_a_rename_by_case(self):
        root = str(self.root)
        self.make("a.txt", "Two.txt", "TWO.txt", "done.txt", "Sub/x.txt")
        # both names were there before; gone; two names to choose from
        self.assertEqual(case_renames(root, ["A.txt", "a.txt", "gone.txt", "two.txt"],
                                      ["A.txt", "a.txt", "gone.txt", "two.txt"]), [])
        # Koofr has the new name already, or not the old one
        self.assertEqual(case_renames(root, ["Done.txt"], ["done.txt"]), [])
        self.assertEqual(case_renames(root, ["Done.txt"], []), [])
        # Koofr's own case of the parent is kept
        self.assertEqual(case_renames(root, ["Sub", "Sub/X.txt"], ["SUB", "SUB/X.txt"]),
                         [CaseRename("Sub/X.txt", "Sub/x.txt", "SUB/X.txt")])


class RootIdentityTest(unittest.TestCase):
    def test_replaced_or_missing_root_stops_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pair"
            root.mkdir()
            (root / "file").write_text("x")
            dev, ino = root_identity(str(root))
            pair = Pair("p", str(root), "/x", root_dev=dev, root_ino=ino)
            self.assertEqual(check_root_identity(pair), [])
            self.assertEqual((root / ".spola-root").read_text().strip(), "p")   # written on first check
            # an unmounted drive looks like an empty folder at the same path:
            # here, the folder deleted and created again (ext4 may even reuse the inode)
            shutil.rmtree(root)
            self.assertEqual(codes(check_root_identity(pair)), ["root-missing"])
            root.mkdir()
            # the runs always pass the last listing: an empty folder is not the paired one,
            # whatever inode number the filesystem gives it (ext4 reuses them at once)
            self.assertEqual(codes(check_root_identity(pair, known_entries=["file"])), ["root-replaced"])
            # a pair from before markers, recreated empty with the inode reused (ext4):
            # the last listing says what should be there
            (root / ".spola-root").unlink(missing_ok=True)
            fresh = Pair("p", str(root), "/x", *(), root_dev=None, root_ino=None)
            self.assertEqual(codes(check_root_identity(fresh, known_entries=["file", "sub/x"])), ["root-replaced"])
            (root / "file").write_text("back")
            self.assertEqual(check_root_identity(fresh, known_entries=["file", "sub/x"]), [])
            self.assertTrue((root / ".spola-root").exists())
            # another pair's folder is not this pair's folder either
            (root / ".spola-root").write_text("other\n")
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


class RefusedPathsTest(unittest.TestCase):
    def test_what_a_run_leaves_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "ok.txt").write_text("a")
            (t / "foto \U0001F600.jpg").write_text("b")
            (t / "Vacanze \U0001F334").mkdir()
            (t / "Vacanze \U0001F334" / "dentro.jpg").write_text("c")     # not listed: the folder is out
            (t / "Report.pdf").write_text("d")
            (t / "report.PDF").write_text("e")
            (t / "SyncTrash").mkdir()
            (t / "SyncTrash" / "x\U0001F600").write_text("f")
            got = {(r.path, r.is_dir, r.code) for r in refused_paths(
                tmp, "/x", skip=lambda rel: rel.startswith("SyncTrash/"))}
            self.assertEqual(got, {("foto \U0001F600.jpg", False, "emoji"),
                                   ("Vacanze \U0001F334", True, "emoji"),
                                   ("report.PDF", False, "case-collision")})
            # the name already synced wins the case collision
            got = {r.path for r in refused_paths(tmp, "/x", known=frozenset({"report.PDF"}),
                                                 skip=lambda rel: rel.startswith("SyncTrash/"))}
            self.assertIn("Report.pdf", got)
            self.assertNotIn("report.PDF", got)
            # the name that stays is told, so the user sees why a normal-looking name is left out
            (left,) = [r for r in refused_paths(tmp, "/x", skip=lambda rel: rel.startswith("SyncTrash/"))
                       if r.code == "case-collision"]
            self.assertEqual(left.other, "Report.pdf")
            self.assertIn("Report.pdf", refused_reason(left.code, left.other))

    def test_names_koofr_accepts_are_suggested(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for name in ("foto \U0001F600.jpg", "Report.pdf", "report.PDF", "report (2).pdf", "Foto.jpg",
                         "\U0001F600.txt", "ok \U0001F44D\ufe0f.txt"):
                (t / name).write_text("x")
            (t / "Vacanze \U0001F334").mkdir()
            refused = refused_paths(tmp, "/x")
            refused.append(Refused("deep/" + "a" * 50, False, "long-path"))
            got = {item.path: new for item, new in rename_plan(tmp, refused, "/x")}
            self.assertEqual(got, {
                "foto \U0001F600.jpg": "foto (2).jpg",          # "foto.jpg" would be Foto.jpg on Koofr
                "Vacanze \U0001F334": "Vacanze",                 # a folder: no extension to keep
                "\U0001F600.txt": "Untitled.txt",
                "ok \U0001F44D\ufe0f.txt": "ok.txt",              # the emoji's selector goes too
                "report.PDF": "report (3).PDF",                  # (2) is taken, upper and lower case aside
                "deep/" + "a" * 50: None,                         # only the user can shorten a path
            })
            # the suggestions are accepted, and do not collide with each other
            self.assertTrue(all(ord(ch) <= 0xFFFF for new in got.values() if new for ch in new))
            self.assertEqual(len({new.casefold() for new in got.values() if new}), 5)
        # a path that would grow too long has no suggestion
        long_root = "/" + "r" * 1015          # "a.txt" fits, "a (2).txt" does not
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.txt").write_text("x")
            (Path(tmp) / "A.txt").write_text("x")
            (item,) = refused_paths(tmp, long_root)
            self.assertEqual(rename_plan(tmp, [item], long_root), [(item, None)])

    def test_exclude_patterns_are_literal(self):
        self.assertEqual(filter_escape("a[1]*?{x}.txt"), "/a\\[1\\]\\*\\?\\{x\\}.txt")
        from spola.preflight import Refused
        self.assertEqual(exclude_lines([Refused("d \U0001F334", True, "emoji"), Refused("f.txt", False, "emoji")]),
                         "/d \U0001F334/**\n/f.txt\n")
        self.assertEqual(exclude_lines([]), "")


if __name__ == "__main__":
    unittest.main()
