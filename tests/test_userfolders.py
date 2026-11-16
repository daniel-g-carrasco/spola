# SPDX-License-Identifier: GPL-3.0-or-later
"""The guard of the user's own folders (_userfolders.py): a file new,
changed or gone is seen, the folders watched are the user's and not the
tests' temporary ones, the tests get four temporary folders of their own,
a run that writes in the user's ends with an error code even when its
tests pass, and in an app the guard is on.

Everything is tried on folders made for the purpose: these tests never
write in the user's own folders, and never ask check() in this process
(that would turn the guard of this very run off)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup
import _userfolders


def fake_environ(base: Path, **more) -> dict:
    """An environment whose user lives under `base`."""
    return {"APPDATA": str(base / "Roaming"), "LOCALAPPDATA": str(base / "Local"), "HOME": str(base / "home"),
            "USERPROFILE": str(base / "home"), **more}


class TheSnapshot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "MyApp"
        (self.root / "cache").mkdir(parents=True)
        for name in ("kept.txt", "changed.txt", "gone.txt"):
            (self.root / name).write_text("a", encoding="utf-8")

    def test_nothing_done_is_nothing_seen(self):
        before = _userfolders.snapshot([self.root])
        self.assertEqual(_userfolders.changes(before, _userfolders.snapshot([self.root])), [])

    def test_a_new_a_changed_and_a_gone_file_are_seen(self):
        before = _userfolders.snapshot([self.root])
        (self.root / "cache" / "model.bin").write_bytes(b"x")
        (self.root / "changed.txt").write_text("ab", encoding="utf-8")
        (self.root / "gone.txt").unlink()
        (self.root / "window").mkdir()
        found = _userfolders.changes(before, _userfolders.snapshot([self.root]))
        self.assertEqual([line.split(" ", 1)[0] for line in found], ["new", "changed", "gone", "new"], found)
        self.assertTrue(found[0].endswith("model.bin"), found)
        self.assertTrue(found[3].endswith("window"), found)        # an empty folder made is a write too

    def test_a_folder_made_where_there_was_none_is_seen(self):
        missing = self.root.parent / "Other"
        before = _userfolders.snapshot([missing])
        self.assertEqual(before, {})
        missing.mkdir()
        self.assertEqual(_userfolders.changes(before, _userfolders.snapshot([missing])), [f"new {missing}"])

    def test_the_content_is_never_read_and_a_file_that_cannot_be_read_is_left_out(self):
        real_scandir = os.scandir

        class Unreadable:
            name = "locked.db"
            path = str(self.root / "locked.db")

            def is_dir(self, follow_symlinks=True):
                return False

            def stat(self, follow_symlinks=True):
                raise PermissionError(13, "locked")

        class Listing:
            def __init__(self, folder):
                self.inner = real_scandir(folder)
                self.folder = folder

            def __enter__(self):
                entries = list(self.inner)
                if os.path.samefile(self.folder, self.root):
                    entries.append(Unreadable())
                return iter(entries)

            def __exit__(self, *exc):
                self.inner.close()

        Listing.root = self.root
        with mock.patch.object(_userfolders.os, "scandir", Listing), \
                mock.patch("builtins.open", side_effect=AssertionError("a file was opened")):
            found = _userfolders.snapshot([self.root])
        self.assertIn(str(self.root / "kept.txt"), found)
        self.assertNotIn(str(self.root / "locked.db"), found)


class TheFoldersWatched(unittest.TestCase):
    def test_they_are_the_users_of_this_app(self):
        base = Path(r"C:\U\me") if os.name == "nt" else Path("/home/me")
        roots = _userfolders.roots("my-app", "MyApp", fake_environ(base, XDG_CONFIG_HOME=str(base / "tmp-config")))
        if os.name == "nt":
            # the XDG variables are not those of the tests: the Windows folders, and the XDG ones under the home
            self.assertEqual(roots, sorted([base / "Local" / "MyApp", base / "Roaming" / "MyApp",
                                            base / "home" / ".cache" / "my-app",
                                            base / "home" / ".local" / "share" / "my-app",
                                            base / "home" / ".local" / "state" / "my-app",
                                            base / "tmp-config" / "my-app"]))
        else:
            self.assertEqual(roots, sorted([base / "tmp-config" / "my-app", base / "home" / ".cache" / "my-app",
                                            base / "home" / ".local" / "share" / "my-app",
                                            base / "home" / ".local" / "state" / "my-app"]))
        self.assertFalse(any(o != r and o in r.parents for o in roots for r in roots), roots)

    def test_the_switch_is_named_after_the_slug(self):
        self.assertEqual(_userfolders.switch_name("my-app"), "MY_APP_TESTS_USER_FOLDERS")


class TheTestsOwnFolders(unittest.TestCase):
    def test_all_four_go_in_one_temporary_folder_whatever_the_user_had_set(self):
        base = Path(r"C:\U\me") if os.name == "nt" else Path("/home/me")
        environ = fake_environ(base, XDG_STATE_HOME=str(base / "my-state"))
        folder = _userfolders.isolate(environ)
        self.assertIn(Path(tempfile.gettempdir()).resolve(), folder.resolve().parents)
        self.assertTrue(folder.is_dir())
        paths = _setup.shared("app.paths").Paths.from_env("my-app", "MyApp", environ)
        self.assertEqual([paths.config, paths.state, paths.data, paths.cache],
                         [folder / name / "my-app" for name in ("config", "state", "data", "cache")])
        self.assertEqual(environ["HOME"], str(base / "home"), "nothing else is changed")

    def test_a_second_call_gives_the_same_folder(self):
        self.assertEqual(_userfolders.isolate({}), _userfolders.isolate({}))


FAKE_SETUP = '''\
import importlib.util
import json
import os
import sys

_spec = importlib.util.spec_from_file_location("fake_paths", {paths!r})
_paths = importlib.util.module_from_spec(_spec)
sys.modules["fake_paths"] = _paths
_spec.loader.exec_module(_paths)


def shared(name):
    return _paths


import _userfolders  # noqa: E402

_userfolders.watch("my-app", "MyApp", json.loads(os.environ["FAKE_USER_ENVIRON"]))
'''

FAKE_TEST = '''\
import os
import unittest

import _setup  # noqa: F401
import _userfolders


class Fake(unittest.TestCase):
    def test_passes(self):
        if os.environ.get("FAKE_WRITE"):
            root = _userfolders.watching()[-1]
            root.mkdir(parents=True, exist_ok=True)
            (root / "settings.json").write_text("{}", encoding="utf-8")
'''


class TheCheckAtTheExit(unittest.TestCase):
    """A small suite of passing tests run in a process of its own, whose
    user lives in a temporary folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.suite = self.base / "suite"
        tests = self.suite / "tests"
        tests.mkdir(parents=True)
        shutil.copyfile(_userfolders.__file__, tests / "_userfolders.py")
        paths = _setup.shared("app.paths").__file__
        (tests / "_setup.py").write_text(FAKE_SETUP.format(paths=paths), encoding="utf-8")
        (tests / "test_fake.py").write_text(FAKE_TEST, encoding="utf-8")

    def run_suite(self, cwd: Path, args: list[str], write: bool, **environ) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.endswith(_userfolders.ENV_SUFFIX)}
        env.update(FAKE_USER_ENVIRON=json.dumps(fake_environ(self.base / "user", **environ)),
                   PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.pop("FAKE_WRITE", None)
        if write:
            env["FAKE_WRITE"] = "1"
        return subprocess.run([sys.executable, "-m", "unittest", "discover", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_a_run_that_writes_there_fails_although_its_tests_pass(self):
        done = self.run_suite(self.suite, ["-s", "tests"], write=True)       # from the root
        self.assertIn("OK", done.stderr)
        self.assertNotEqual(done.returncode, 0, done.stderr)
        self.assertIn("FAILED: the tests wrote in the user's own folders:", done.stderr)
        self.assertIn("settings.json", done.stderr)
        written = [p for p in (self.base / "user").rglob("settings.json")]
        self.assertEqual(len(written), 1, "the guard deletes nothing")

    def test_a_clean_run_passes_and_says_nothing(self):
        done = self.run_suite(self.suite / "tests", [], write=False)        # from the tests' folder
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("user's own folders", done.stderr)

    def test_with_the_switch_the_writes_are_told_without_failing(self):
        done = self.run_suite(self.suite / "tests", [], write=True, MY_APP_TESTS_USER_FOLDERS="warn")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WARNING: the tests wrote in the user's own folders", done.stderr)
        self.assertIn("settings.json", done.stderr)


@unittest.skipIf(_setup.PACKAGE == "comune", "the common folder is no app: it has no folders of its own to "
                                             "watch, and its _setup.py does not call _userfolders.watch")
class InThisApp(unittest.TestCase):
    def test_the_guard_is_on(self):
        watched = _userfolders.watching()
        self.assertIsNotNone(watched, "tests/_setup.py must call _userfolders.watch(SLUG, EXE_NAME) at its top, "
                                      "before the environment is changed")
        self.assertTrue(watched, "no folder watched")

    def test_the_folders_watched_are_not_the_tests_own(self):
        temporary = Path(tempfile.gettempdir()).resolve()
        changed = [Path(os.environ[v]).resolve() for v, _name in _userfolders.XDG
                   if os.environ.get(v)]
        for root in _userfolders.watching() or ():
            resolved = root.resolve()
            self.assertNotIn(temporary, [resolved, *resolved.parents], f"{root}: a temporary folder is watched")
            for folder in changed:
                self.assertNotIn(folder, [resolved, *resolved.parents],
                                 f"{root}: watch() was called after the tests changed the environment")

    def test_the_folders_the_tests_use_are_all_temporary(self):
        temporary = Path(tempfile.gettempdir()).resolve()
        used = _userfolders.in_use()
        self.assertEqual(len(used), 4, used)
        for folder in used:
            self.assertIn(temporary, folder.resolve().parents,
                          f"{folder}: the tests would write in the user's own folder: tests/_setup.py must call "
                          "_userfolders.isolate() right after watch()")


if __name__ == "__main__":
    unittest.main()
