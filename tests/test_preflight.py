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

import _setup

from spola.config import Config, Pair
from spola.preflight import (
    CaseRename, case_renames, check_koofr_desktop, check_local_root, check_overlap, check_root_identity,
    Refused, refused_paths, refused_reason, rename_plan, root_identity, scan_names,
)
from spola.rclone import exclude_lines, filter_escape


def codes(problems):
    return [p.code for p in problems]


@unittest.skipIf(_setup.WINDOWS, "Linux folders (Windows: WindowsRootTest)")
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


@unittest.skipUnless(_setup.WINDOWS, "Windows only")
class WindowsRootTest(unittest.TestCase):
    def test_the_profile_but_not_its_app_data_or_the_system(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory(dir=home) as tmp:
            self.assertEqual(check_local_root(tmp, home), [])
            self.assertEqual(check_local_root(tmp.replace("\\", "/").upper(), home), [])
        self.assertEqual(codes(check_local_root(os.path.dirname(home), home)), ["too-broad"])
        appdata = os.path.join(home, "AppData", "Local")
        if os.path.isdir(appdata):
            self.assertEqual(codes(check_local_root(appdata, home)), ["protected"])
        windows = os.environ.get("SystemRoot", "C:\\Windows")
        self.assertEqual(codes(check_local_root(windows, home)), ["outside-home"])

    def test_home_is_the_profile_whatever_home_says(self):
        from spola.preflight import home_dir
        profile = os.path.expanduser("~")
        environ = {"USERPROFILE": profile, "HOME": "C:\\msys64\\home\\someone"}
        self.assertEqual(os.path.normcase(home_dir(environ)), os.path.normcase(os.path.realpath(profile)))


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

    @unittest.skipIf(_setup.WINDOWS, "the .desktop autostart is Linux only (Windows: test_windows_processes)")
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

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_processes_and_login_commands(self):
        from spola import win32
        names = win32.process_names()
        self.assertTrue(any(name.startswith("python") for name in names), sorted(names)[:20])
        self.assertIsInstance(win32.login_commands(), list)

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_startup_folder(self):
        # Koofr Desktop's installer puts Koofr.lnk in the Startup folder; a
        # shortcut renamed still points to storagegui.exe, and one turned off
        # in Task Manager (StartupApproved, first byte odd) starts nothing
        from spola import win32
        target = r"C:\Users\u\AppData\Local\koofr\storagegui.exe"
        lnk = b"L\x00\x00\x00" + b"\x00" * 72 + target.encode("utf-16-le") + b"\x00\x00"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Avvio.lnk").write_bytes(lnk)
            (Path(tmp) / "desktop.ini").write_text("[.ShellClassInfo]")
            entries = win32.startup_folder_entries(tmp, {})
            self.assertEqual(len(entries), 1)
            self.assertIn("storagegui", entries[0].casefold())
            on, off = b"\x02" + b"\x00" * 11, b"\x03" + b"\x00" * 11
            self.assertEqual(len(win32.startup_folder_entries(tmp, {"avvio.LNK": on})), 1)
            self.assertEqual(win32.startup_folder_entries(tmp, {"avvio.LNK": off}), [])
        self.assertTrue(win32.approved(None))
        self.assertTrue(win32.approved(b"\x06" + b"\x00" * 11))
        self.assertFalse(win32.approved(b"\x07" + b"\x00" * 11))

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_windows_turn_off_as_task_manager_does(self):
        # a value of our own in the user's StartupApproved\Run, removed after
        import winreg
        from spola import win32
        name = f"spola-test-{os.getpid()}"
        entry = win32.LoginEntry("x", winreg.HKEY_CURRENT_USER, "Run", name)
        path = win32.APPROVED_KEY + "\\Run"
        try:
            win32.turn_off(entry, now=0)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                data, kind = winreg.QueryValueEx(key, name)
            self.assertEqual(kind, winreg.REG_BINARY)
            self.assertEqual(len(data), 12)
            self.assertFalse(win32.approved(data))
        finally:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)

    def test_koofr_desktop_state(self):
        from spola.preflight import KoofrDesktopState
        self.assertFalse(KoofrDesktopState(False, False).active)
        self.assertTrue(KoofrDesktopState(True, False).active)
        self.assertTrue(KoofrDesktopState(False, True).active)

    def test_koofr_desktop_config_path(self):
        from spola import preflight
        saved = os.environ.get("APPDATA")
        try:
            os.environ["APPDATA"] = os.path.join(tempfile.gettempdir(), "Roaming")
            if _setup.WINDOWS:
                expected = Path(os.environ["APPDATA"]) / "koofr" / "config.json"
            else:
                expected = Path.home() / ".koofr" / "config.json"
            self.assertEqual(preflight.koofr_desktop_config(), expected)
        finally:
            if saved is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = saved

    @unittest.skipIf(_setup.WINDOWS, "on Windows its processes are in sight: the logs do not count")
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

    @unittest.skipIf(_setup.WINDOWS, "the Flatpak is Linux only")
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
        self.make("a.txt", "done.txt", "Sub/x.txt")
        # both names were there before; gone
        self.assertEqual(case_renames(root, ["A.txt", "a.txt", "gone.txt"], ["A.txt", "a.txt", "gone.txt"]), [])
        if _setup.case_sensitive(root):
            # two names to choose from (not on NTFS, where they are one file)
            self.make("Two.txt", "TWO.txt")
            self.assertEqual(case_renames(root, ["two.txt"], ["two.txt"]), [])
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
            both = ["case-collision"] if _setup.case_sensitive(tmp) else []      # one file on NTFS
            self.assertEqual(sorted(codes(scan_names(tmp))), both + ["emoji"])
            deep = "/" + "/".join(["cartella"] * 95)     # 855 characters
            self.assertEqual(sorted(codes(scan_names(tmp, remote_root=deep))),
                             both + ["emoji", "long-path"])

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
                                   ("Vacanze \U0001F334", True, "emoji")}
                             | ({("report.PDF", False, "case-collision")} if _setup.case_sensitive(tmp) else set()))
            if not _setup.case_sensitive(tmp):
                return          # NTFS: Report.pdf and report.PDF are one file, as on Koofr
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
            collision = _setup.case_sensitive(tmp)          # one file on NTFS
            self.assertEqual(got, {
                "foto \U0001F600.jpg": "foto (2).jpg",          # "foto.jpg" would be Foto.jpg on Koofr
                "Vacanze \U0001F334": "Vacanze",                 # a folder: no extension to keep
                "\U0001F600.txt": "Untitled.txt",
                "ok \U0001F44D\ufe0f.txt": "ok.txt",              # the emoji's selector goes too
                **({"report.PDF": "report (3).PDF"} if collision else {}),   # (2) is taken, case aside
                "deep/" + "a" * 50: None,                         # only the user can shorten a path
            })
            # the suggestions are accepted, and do not collide with each other
            self.assertTrue(all(ord(ch) <= 0xFFFF for new in got.values() if new for ch in new))
            self.assertEqual(len({new.casefold() for new in got.values() if new}), 5 if collision else 4)
        # a path that would grow too long has no suggestion
        long_root = "/" + "r" * 1015          # "a.txt" fits, "a (2).txt" does not
        with tempfile.TemporaryDirectory() as tmp:
            if not _setup.case_sensitive(tmp):
                return          # NTFS: a.txt and A.txt are one file
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
