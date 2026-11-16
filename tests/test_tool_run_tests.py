#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/run_tests.py: the suites it finds, the choice from the changed
files, one group in this process with its result and its exit code, a test
that never ends, a run that writes in the user's own folders, several
suites as processes with their table, and the app's own configuration.

Everything on a made-up app in a temporary folder, with a copy of the
tool: the tool finds its app from its own place."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _setup
import _userfolders

TOOL = _setup.ROOT / "tools" / "run_tests.py"

PASSES = '''\
import os
import unittest


class Passes(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    @unittest.skipUnless(os.environ.get("FAKE_NEVER"), "not asked")
    def test_skipped(self):
        pass
'''

MAY_FAIL = '''\
import os
import time
import unittest


class MayFail(unittest.TestCase):
    def test_red_when_asked(self):
        self.assertFalse(os.environ.get("FAKE_FAIL"))

    def test_slow_when_asked(self):
        if os.environ.get("FAKE_SLOW"):
            time.sleep(30)
'''

WRITES = '''\
import os
import unittest

import _setup  # noqa: F401
import _userfolders


class Writes(unittest.TestCase):
    def test_passes(self):
        if os.environ.get("FAKE_WRITE"):
            root = _userfolders.watching()[-1]
            root.mkdir(parents=True, exist_ok=True)
            (root / "settings.json").write_text("{}", encoding="utf-8")
'''

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

CONFIG = '''\
import os

LIMIT_VARIABLE = "MY_APP_TEST_LIMIT"
SELECTION = [
    ("tests/", None, "its own suite"),
    ("docs/", (), "documents"),
    ("src/gui/", ("group", "test_w*"), "the windows"),
]


def checks():
    return ["a check of the app's own failed"] if os.environ.get("FAKE_CHECK") else []
'''


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"run_tests_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AMadeUpApp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.app = self.base / "app"
        tests = self.app / "tests"
        (tests / "group").mkdir(parents=True)
        (self.app / "tools").mkdir()
        self.tool = self.app / "tools" / "run_tests.py"
        shutil.copyfile(TOOL, self.tool)
        shutil.copyfile(_userfolders.__file__, tests / "_userfolders.py")
        (tests / "_setup.py").write_text(FAKE_SETUP.format(paths=_setup.shared("app.paths").__file__), encoding="utf-8")
        (tests / "test_passes.py").write_text(PASSES, encoding="utf-8")
        (tests / "test_may_fail.py").write_text(MAY_FAIL, encoding="utf-8")
        (tests / "test_writes.py").write_text(WRITES, encoding="utf-8")
        (tests / "group" / "__init__.py").write_text("", encoding="utf-8")
        (tests / "group" / "test_inside.py").write_text(PASSES, encoding="utf-8")

    def configure(self):
        (self.app / "tools" / "run_tests_config.py").write_text(CONFIG, encoding="utf-8")

    def run_tool(self, *args, **environ) -> subprocess.CompletedProcess:
        user = {"APPDATA": str(self.base / "user" / "Roaming"), "LOCALAPPDATA": str(self.base / "user" / "Local"),
                "HOME": str(self.base / "user" / "home"), "USERPROFILE": str(self.base / "user" / "home")}
        # the made-up app's guard reads its switch in the made-up user's environment
        user.update({k: v for k, v in environ.items() if k.endswith(_userfolders.ENV_SUFFIX)})
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("FAKE_") and not k.endswith(("_TEST_LIMIT", "_TESTS_LIMIT", _userfolders.ENV_SUFFIX))}
        env.update(FAKE_USER_ENVIRON=json.dumps(user), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        env.update(environ)
        return subprocess.run([sys.executable, str(self.tool), *args], cwd=self.base, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)


class TheSuitesAndTheChoice(AMadeUpApp):
    def test_a_folder_with_tests_and_a_module_at_the_root_are_suites(self):
        tool = load(self.tool)
        self.assertEqual(tool.suites(None), {"group": ["tests/group"], "test_may_fail": ["--names", "test_may_fail"],
                                             "test_passes": ["--names", "test_passes"],
                                             "test_writes": ["--names", "test_writes"]})

    def test_a_folder_of_tests_without_init_is_refused_with_its_name(self):
        (self.app / "tests" / "group" / "__init__.py").unlink()
        with self.assertRaises(SystemExit) as refused:
            load(self.tool).suites(None)
        self.assertIn("__init__.py", str(refused.exception))

    def test_without_a_table_a_test_runs_its_suite_and_anything_else_runs_everything(self):
        tool = load(self.tool)
        known = tool.suites(None)
        chosen, nothing = tool.choose(["tests/test_passes.py", "tests/group/test_inside.py"], known)
        self.assertEqual(sorted(chosen), ["group", "test_passes"])
        self.assertEqual(nothing, [])
        chosen, _nothing = tool.choose(["tests/_setup.py"], known)          # a helper: everything
        self.assertEqual(sorted(chosen), sorted(known))
        chosen, _nothing = tool.choose(["README.md"], known)                # no row names it: everything
        self.assertEqual(sorted(chosen), sorted(known))
        self.assertIn("README.md: no row names it: everything", chosen["group"])

    def test_the_apps_table_narrows_the_choice_and_the_first_row_decides(self):
        self.configure()
        tool = load(self.tool)
        config, known = tool.load_config(), tool.suites(None)
        chosen, nothing = tool.choose(["docs/guide.md", "src/gui/window.py"], known, config)
        self.assertEqual(nothing, ["docs/guide.md"])
        self.assertEqual(sorted(chosen), ["group", "test_writes"])          # a name, and a pattern
        self.assertEqual(chosen["group"], ["src/gui/window.py: the windows"])
        chosen, _nothing = tool.choose(["tests/test_passes.py"], known, config)
        self.assertEqual(sorted(chosen), ["test_passes"])

    def test_the_changed_files_are_the_committed_and_the_new_ones(self):
        if shutil.which("git") is None:
            self.skipTest("git")

        def git(*args):
            done = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.org", "-c",
                                   "commit.gpgsign=false", *args], cwd=self.app, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-q", "-m", "start")
        git("checkout", "-q", "-b", "work")
        (self.app / "tests" / "test_passes.py").write_text(PASSES + "\n", encoding="utf-8")
        git("commit", "-q", "-am", "a change")
        (self.app / "notes.txt").write_text("new", encoding="utf-8")
        # what Python and the runner leave behind is never a changed file, also without a .gitignore:
        # the bytecode of the tool loaded here (when Python writes it), of a module, the last run's files
        for left in ("tools/__pycache__/run_tests.cpython-399.pyc", "tests/__pycache__/test_passes.cpython-399.pyc",
                     "tests/old.pyc", "build/test-runs/test_passes.log", "build/test-runs/test_passes.json"):
            (self.app / left).parent.mkdir(parents=True, exist_ok=True)
            (self.app / left).write_bytes(b"left by a run")
        self.assertEqual(load(self.tool).changed_files("main"), ["notes.txt", "tests/test_passes.py"])
        done = self.run_tool("--changed", "--list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("2 files changed from main and not committed; 0 need no test", done.stdout)
        self.assertIn("4 suites:", done.stdout)                 # notes.txt: no row names it


class OneGroupInThisProcess(AMadeUpApp):
    def test_the_whole_folder_passes_and_the_result_is_written(self):
        report = self.base / "result.json"
        done = self.run_tool("tests", "--report", str(report), "--durations", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Ran 7 tests", done.stderr)
        self.assertIn("The 2 slowest tests:", done.stderr)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((result["run"], result["skipped"], result["code"], result["failures"]), (7, 2, 0, []))
        self.assertEqual(result["wrote_in_user_folders"], [])

    def test_a_red_test_is_a_red_run_and_is_named_in_the_result(self):
        report = self.base / "result.json"
        done = self.run_tool("--names", "test_may_fail", "--report", str(report), FAKE_FAIL="1")
        self.assertEqual(done.returncode, 1, done.stderr)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(result["failures"], ["test_may_fail.MayFail.test_red_when_asked"])

    def test_one_folder_and_one_module_given_as_a_path(self):
        done = self.run_tool("tests/group")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Ran 2 tests", done.stderr)
        done = self.run_tool("tests/test_passes.py")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Ran 2 tests", done.stderr)

    def test_a_test_that_never_ends_is_stopped_with_the_stacks(self):
        done = self.run_tool("--names", "test_may_fail", FAKE_SLOW="1", RUN_TESTS_LIMIT="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Timeout (0:00:01)!", done.stderr)
        self.assertIn("test_slow_when_asked", done.stderr)

    def test_a_run_that_writes_in_the_users_folders_is_red_although_its_tests_pass(self):
        report = self.base / "result.json"
        done = self.run_tool("--names", "test_writes", "--report", str(report), FAKE_WRITE="1")
        self.assertIn("OK", done.stderr)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("FAILED: the tests wrote in the user's own folders:", done.stderr)
        self.assertEqual(done.stderr.count("FAILED: the tests wrote"), 1, "the guard's own check at the exit is silent")
        written = json.loads(report.read_text(encoding="utf-8"))["wrote_in_user_folders"]
        self.assertTrue(any(line.endswith("settings.json") for line in written), written)

    def test_with_the_switch_the_write_is_told_without_failing(self):
        done = self.run_tool("--names", "test_writes", FAKE_WRITE="1", MY_APP_TESTS_USER_FOLDERS="warn")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WARNING: the tests wrote in the user's own folders", done.stderr)

    def test_the_apps_own_limit_variable_and_checks(self):
        self.configure()
        done = self.run_tool("--names", "test_may_fail", FAKE_SLOW="1", MY_APP_TEST_LIMIT="1")
        self.assertIn("Timeout (0:00:01)!", done.stderr)
        done = self.run_tool("--names", "test_passes", FAKE_CHECK="1")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("FAILED: a check of the app's own failed", done.stderr)


class SeveralSuitesAsProcesses(AMadeUpApp):
    def test_every_suite_runs_and_the_table_adds_them_up(self):
        done = self.run_tool("--all", "--parallel", "2")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("4 suites: group, test_may_fail, test_passes, test_writes", done.stdout)
        total = [line for line in done.stdout.splitlines() if line.startswith("total")][0].split()
        self.assertEqual(total[1:4], ["7", "0", "2"])           # tests, red, skipped
        runs = self.app / "build" / "test-runs"
        self.assertEqual(sorted(p.name for p in runs.glob("*.json")),
                         ["group.json", "test_may_fail.json", "test_passes.json", "test_writes.json"])
        self.assertIn("Ran 2 tests", (runs / "group.log").read_text(encoding="utf-8"))

    def test_a_red_suite_is_a_red_run_and_its_test_is_named(self):
        done = self.run_tool("test_may_fail", "test_passes", FAKE_FAIL="1")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("Red:", done.stdout)
        self.assertIn("test_may_fail  test_may_fail.MayFail.test_red_when_asked", done.stdout)

    def test_a_suite_that_wrote_in_the_users_folders_is_red_in_the_table(self):
        done = self.run_tool("test_writes", "test_passes", FAKE_WRITE="1")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("test_writes  wrote in the user's own folders: new", done.stdout)

    def test_a_name_that_is_no_suite_is_refused(self):
        done = self.run_tool("test_passes", "nothing")
        self.assertEqual(done.returncode, 2)
        self.assertIn("nothing: not a suite", done.stderr)


if __name__ == "__main__":
    unittest.main()
