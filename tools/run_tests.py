#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the app's tests: one group in this process, or several as processes.

    python tools/run_tests.py tests                  the whole folder in this process, as
                                                     "python -m unittest discover -s tests" does
    python tools/run_tests.py tests/<folder>         one folder of tests/ (a package) in this process
    python tools/run_tests.py --names test_a test_b  whole test modules by name, in this process
    python tools/run_tests.py --all [--parallel N]   every suite, each a process of its own
    python tools/run_tests.py <suite> <suite> [--parallel N]
    python tools/run_tests.py --changed [BASE] [--parallel N] [--list]
                                                     the suites that the changed files can touch
    ... --durations 15                               the slowest tests at the end

What it adds to unittest:
- a test that has not ended after the limit (600 seconds; the variable
  RUN_TESTS_LIMIT, or the one the app names; 0 switches it off) prints the
  stacks of every thread and ends the process: a run that stops half way
  says where. A crash prints the stacks too (faulthandler). The limit
  counts from the start of each test, so a class set-up is counted with
  the test before it;
- the guard of the user's own folders (tests/_userfolders.py) is asked at
  the end: a run that wrote there is red, with what it wrote;
- the time of every test (--durations N);
- several suites at once. A suite is a folder of tests/ with tests in it
  (a package) or a test module at the root of tests/. Each runs as a
  process of its own, its output in build/test-runs/<suite>.log and its
  result in <suite>.json; the end is a table: suite, tests, red, skipped,
  seconds. The slowest of the last run start first. A process that does
  not end is killed with its children. With a flat tests/ folder the
  whole of it in one process is quicker than --all one at a time: --all
  pays with --parallel;
- --changed: the files that differ from BASE (main by default: from its
  merge base with HEAD) plus what is not committed, and the suites they
  can touch. The table is the app's (SELECTION below); a file that no row
  names runs everything.

The exit code is not zero if a test or a suite is red, a suite did not
end, or the tests wrote in the user's own folders.

What is the app's own goes in tools/run_tests_config.py, next to this file
(it is optional, and never a copy of the model's). Every name is optional:
    LIMIT_VARIABLE = "MY_APP_TEST_LIMIT"    the environment variable of the limit
    SELECTION = [(prefix, suites, why), ...]
        the first row whose prefix starts the path decides. suites: a
        tuple of suite names or patterns ("test_tool_*"; ALL is "*"), an
        empty tuple (the file needs no test), or OWN (a file of tests/:
        its own suite, everything for a helper). When given it replaces
        the default, which is the one row ("tests/", OWN, ...).
    def suites(root, found): ...            the suites to use, given the ones found
    def suite_of_test(path, known): ...     (suites, why) for a file of tests/, or None for the default
    def checks(): ...                       lines that fail the run, asked after the tests (in this process)
    def at_end(): ...                       called after the summary, before the process ends
    HARD_EXIT = True                        end with os._exit (a toolkit that crashes while it is taken apart)
"""

from __future__ import annotations

import argparse
import faulthandler
import fnmatch
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
RUNS = ROOT / "build" / "test-runs"            # the log and the result of every suite of the last run
ALL, OWN = "*", None
DEFAULT_SELECTION = [("tests/", OWN, "its own suite")]
DEFAULT_LIMIT_VARIABLE = "RUN_TESTS_LIMIT"


def load_config():
    """The app's tools/run_tests_config.py as a module, or None."""
    path = Path(__file__).with_name("run_tests_config.py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("run_tests_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def limit_of(config, environ=None) -> float:
    environ = os.environ if environ is None else environ
    variable = getattr(config, "LIMIT_VARIABLE", DEFAULT_LIMIT_VARIABLE)
    try:
        return float(environ.get(variable, "600"))
    except ValueError:
        raise SystemExit(f"{variable}: a number of seconds") from None


# ---- one group, in this process -------------------------------------------

class WatchedResult(unittest.TextTestResult):
    """The text result, with faulthandler's alarm armed again at the start
    of every test, and the time of each test."""

    limit = 0.0
    times: dict = {}        # test id -> seconds (the set-ups of a class and a module are not in it)

    def startTest(self, test):
        if self.limit > 0:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(self.limit, exit=True)
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        WatchedResult.times[test.id()] = time.perf_counter() - self._started


def slowest(count: int) -> list[tuple[str, float]]:
    return sorted(WatchedResult.times.items(), key=lambda item: -item[1])[:count]


def run_here(start: str, names: list[str] | None, durations: int, report: str | None, config) -> int:
    """One folder of tests (or whole modules by name) in this process; the
    exit code. The result goes to `report` as JSON when asked."""
    faulthandler.enable()
    WatchedResult.limit = limit_of(config)
    runner = unittest.TextTestRunner(resultclass=WatchedResult)
    began = time.perf_counter()
    if names:               # whole modules by name, as "python -m unittest test_a" from tests/
        sys.path.insert(0, str(TESTS))
        argv = ["unittest", *names]
    else:
        folder = Path(start) if Path(start).is_absolute() else ROOT / start
        argv = ["unittest", "discover", "-s", str(folder), "-t", str(TESTS)]
    program = unittest.main(module=None, argv=argv, exit=False, testRunner=runner)
    faulthandler.cancel_dump_traceback_later()
    seconds = time.perf_counter() - began
    result = program.result
    code = 0 if result.wasSuccessful() else 1
    # the guard of the user's own folders: this runner sets the exit code, so it asks the guard itself
    guard = sys.modules.get("_userfolders")
    written = guard.check() if guard is not None else []
    if written:
        sys.stderr.write(guard.message(written))
        if guard.failing():
            code = 1
    failed_checks = list(config.checks()) if hasattr(config, "checks") else []
    if failed_checks:
        print("FAILED: " + "\n".join(failed_checks), file=sys.stderr)
        code = 1
    if durations:
        print(f"\nThe {durations} slowest tests:", file=sys.stderr)
        for name, taken in slowest(durations):
            print(f"  {taken:7.2f}s  {name}", file=sys.stderr)
    if report:
        Path(report).write_text(json.dumps({
            "run": result.testsRun, "seconds": round(seconds, 2), "skipped": len(result.skipped), "code": code,
            "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
            "wrote_in_user_folders": written, "checks": failed_checks,
            "slowest": [[n, round(t, 2)] for n, t in slowest(max(durations, 30))]}), encoding="utf-8")
    return code


# ---- the suites, and the choice from the changed files --------------------

def found_suites(root: Path = ROOT) -> dict[str, list[str]]:
    """Every suite of tests/, by name, with the arguments of its one run: a
    folder with tests in it (a package), a test module at the root."""
    tests = root / "tests"
    out: dict[str, list[str]] = {}
    for path in sorted(tests.iterdir()):
        if path.is_dir() and path.name != "__pycache__" and any(path.rglob("test_*.py")):
            if not (path / "__init__.py").is_file():
                raise SystemExit(f"{path}: a folder of tests is a package: it needs an __init__.py")
            out[path.name] = [f"tests/{path.name}"]
    for path in sorted(tests.glob("test_*.py")):
        if path.stem in out:
            raise SystemExit(f"{path.stem}: a folder and a module of tests with the same name")
        out[path.stem] = ["--names", path.stem]
    return out


def suites(config, root: Path = ROOT) -> dict[str, list[str]]:
    found = found_suites(root)
    return config.suites(root, found) if hasattr(config, "suites") else found


def suite_of_test(path: str, known: dict[str, list[str]], config=None) -> tuple[tuple, str]:
    """The suites a changed file of tests/ runs."""
    if hasattr(config, "suite_of_test"):
        chosen = config.suite_of_test(path, known)
        if chosen is not None:
            return chosen
    parts = path.split("/")
    if len(parts) > 2 and parts[1] in known:
        return (parts[1],), "its own suite"
    if len(parts) == 2 and parts[1].endswith(".py") and parts[1][:-3] in known:
        return (parts[1][:-3],), "its own module"
    return (ALL,), "a helper or a file shared by the tests"


def choose(changed: list[str], known: dict[str, list[str]], config=None) -> tuple[dict[str, list[str]], list[str]]:
    """The suites to run for the changed files, each with its reasons (the
    file and the row), and the files that need no test. A pattern is
    expanded; a path that no row names runs everything."""
    selection = getattr(config, "SELECTION", DEFAULT_SELECTION)
    chosen: dict[str, list[str]] = {}
    nothing: list[str] = []
    for path in changed:
        group, why = (ALL,), "no row names it: everything"
        for prefix, rows, reason in selection:
            if path.startswith(prefix):
                group, why = (rows, reason) if rows is not OWN else suite_of_test(path, known, config)
                break
        if not group:
            nothing.append(path)
        for item in group:
            for name in known:
                if fnmatch.fnmatchcase(name, item):
                    chosen.setdefault(name, []).append(f"{path}: {why}")
    return chosen, nothing


def changed_files(base: str, root: Path = ROOT) -> list[str]:
    """What differs from `base` (from its merge base with HEAD) plus what
    is not committed (changed, staged, new), as paths from the root of
    this app (it may be a folder of a bigger repository)."""
    def git(*args):
        done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
        if done.returncode:
            raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
        return [line for line in done.stdout.splitlines() if line.strip()]
    files = set(git("diff", "--name-only", "--relative", f"{base}...HEAD"))
    files |= set(git("diff", "--name-only", "--relative", "HEAD"))
    files |= set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(name for name in files if not left_by_a_run(name))


def left_by_a_run(name: str) -> bool:
    """What Python and this tool leave behind (the bytecode caches, the
    logs and results of the suites): never a changed file that chooses
    tests, also in a repository whose .gitignore does not name them."""
    parts = name.split("/")
    return ("__pycache__" in parts or name.endswith((".pyc", ".pyo"))
            or parts[:2] == list(RUNS.relative_to(ROOT).parts))


# ---- several suites, as processes -----------------------------------------

def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()


def _file_name(suite: str) -> str:
    return suite.replace("/", "-").replace("\\", "-")


def run_one(name: str, args: list[str], durations: int, suite_limit: float) -> dict:
    """One suite as a process of its own, its output in a file; its result
    read from the file it writes."""
    RUNS.mkdir(parents=True, exist_ok=True)
    log, result = RUNS / f"{_file_name(name)}.log", RUNS / f"{_file_name(name)}.json"
    result.unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), *args, "--report", str(result),
               "--durations", str(durations)]
    began, reported, status = time.perf_counter(), None, ""
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        process = subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
        while process.poll() is None:
            time.sleep(0.2)
            now = time.perf_counter()
            if reported is None and result.exists():
                reported = now
            if reported is not None and now - reported > 60:      # the result is out and the process does not end
                status = "killed after its result"
                _kill_tree(process)
            elif now - began > suite_limit:
                status = f"killed after {suite_limit:.0f}s"
                _kill_tree(process)
        process.wait()
    row = {"suite": name, "wall": time.perf_counter() - began, "code": process.returncode, "status": status,
           "log": str(log)}
    try:
        row.update(json.loads(result.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        row["status"] = status or "no result (crashed?)"
    return row


def run_suites(names: list[str], known: dict[str, list[str]], parallel: int, durations: int, suite_limit: float) -> int:
    def last(name):         # the slowest of the last run first, so that a long suite does not start last
        try:
            return json.loads((RUNS / f"{_file_name(name)}.json").read_text(encoding="utf-8"))["seconds"]
        except (OSError, ValueError, KeyError):
            return 0
    pending, rows, lock = sorted(names, key=lambda n: -last(n)), {}, threading.Lock()

    def worker():
        while True:
            with lock:
                if not pending:
                    return
                name = pending.pop(0)
            row = run_one(name, known[name], durations, suite_limit)
            with lock:
                rows[name] = row
                red = len(row.get("failures", [])) + len(row.get("errors", []))
                print(f"  {name}: {row.get('run', '?')} tests, {red} red, {row['wall']:.0f}s", flush=True)

    began = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(max(1, min(parallel, len(names))))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print_table([rows[n] for n in names], time.perf_counter() - began, parallel, durations)
    return 1 if any(r["code"] or "run" not in r for r in rows.values()) else 0


def print_table(rows: list[dict], total: float, parallel: int, durations: int, out=print) -> None:
    width = max(len(r["suite"]) for r in rows) + 2
    out(f"\n{'suite':<{width}}{'tests':>7}{'red':>5}{'skipped':>9}{'seconds':>9}")
    red_all, slow = [], []
    for r in rows:
        red = len(r.get("failures", [])) + len(r.get("errors", []))
        note = "  " + r["status"] if r.get("status") else (f"  exit code {r['code']}" if r["code"] and not red else "")
        out(f"{r['suite']:<{width}}{r.get('run', '-'):>7}{red:>5}{r.get('skipped', '-'):>9}{r['wall']:>9.0f}{note}")
        red_all += [(r["suite"], t) for t in r.get("failures", []) + r.get("errors", [])]
        red_all += [(r["suite"], "wrote in the user's own folders: " + w) for w in r.get("wrote_in_user_folders", [])]
        red_all += [(r["suite"], line) for line in r.get("checks", [])]
        slow += [(t, n, r["suite"]) for n, t in r.get("slowest", [])]
    out(f"{'total':<{width}}{sum(r.get('run', 0) for r in rows):>7}{len(red_all):>5}"
        f"{sum(r.get('skipped', 0) for r in rows):>9}{total:>9.0f}"
        f"   ({parallel} at a time; the suites added up: {sum(r['wall'] for r in rows):.0f}s)")
    if red_all:
        out("\nRed:")
        for suite, test in red_all:
            out(f"  {suite}  {test}")
    for r in rows:
        if "run" not in r:
            out(f"\n{r['suite']}: {r['status']}; its output: {r['log']}")
    if durations:
        out(f"\nThe {durations} slowest tests of all:")
        for taken, name, _suite in sorted(slow, reverse=True)[:durations]:
            out(f"  {taken:7.2f}s  {name}")


# ---- the command line -----------------------------------------------------

def cli(argv: list[str] | None = None) -> tuple[int, bool, object]:
    """The exit code, whether the tests ran in this process, and the app's
    configuration."""
    parser = argparse.ArgumentParser(description="Run one group of tests in this process, or several suites as processes.")
    parser.add_argument("paths", nargs="*", help="tests or tests/<folder>: one runs in this process; "
                                                 "several suites by name run as processes")
    parser.add_argument("--names", nargs="+", help="whole test modules by name (test_paths), in this process")
    parser.add_argument("--all", action="store_true", help="every suite")
    parser.add_argument("--changed", nargs="?", const="main", metavar="BASE",
                        help="the suites that the files changed from BASE (main) and not committed can touch")
    parser.add_argument("--parallel", nargs="?", const=4, type=int, metavar="N",
                        help="N suites at a time (4 without N); several suites without it go one at a time")
    parser.add_argument("--list", action="store_true", help="with --changed or --all: say the suites, do not run")
    parser.add_argument("--durations", type=int, default=0, metavar="N", help="the N slowest tests at the end")
    parser.add_argument("--report", help="(one group) the file where its result is written, as JSON")
    parser.add_argument("--suite-limit", type=float, default=2400, help="seconds after which a suite is killed")
    args = parser.parse_args(argv)
    config = load_config()
    if args.names:
        return run_here("", args.names, args.durations, args.report, config), True, config
    if not (args.all or args.changed or len(args.paths) > 1 or args.parallel):
        if len(args.paths) != 1:
            parser.error("a folder of tests, --names, --all or --changed")
        path = args.paths[0]
        if not (Path(path) if Path(path).is_absolute() else ROOT / path).is_dir():
            # a test module given as a suite (test_paths, tests/test_paths.py): by name
            name = path.replace("\\", "/").rstrip("/").removeprefix("tests/").removesuffix(".py")
            if not (TESTS / f"{name}.py").is_file():
                parser.error(f"{path}: neither a folder of tests nor a test module")
            return run_here("", [name], args.durations, args.report, config), True, config
        return run_here(path, None, args.durations, args.report, config), True, config
    known = suites(config)
    if args.all:
        names = list(known)
    elif args.changed:
        changed = changed_files(args.changed)
        chosen, nothing = choose(changed, known, config)
        print(f"{len(changed)} files changed from {args.changed} and not committed; {len(nothing)} need no test")
        names = [n for n in known if n in chosen]
        for name in names:
            why = sorted(set(chosen[name]))
            print(f"  {name}: {why[0]}" + (f" (+{len(why) - 1} more)" if len(why) > 1 else ""))
        if not names:
            print("nothing to run")
            return 0, False, config
    else:
        names = []
        for path in args.paths:
            key = path.replace("\\", "/").rstrip("/").removeprefix("tests/").removesuffix(".py")
            if key not in known:
                parser.error(f"{path}: not a suite (they are: {', '.join(known)})")
            names.append(key)
    print(f"{len(names)} suites: {', '.join(names)}", flush=True)
    if args.list:
        return 0, False, config
    return run_suites(names, known, args.parallel or 1, args.durations, args.suite_limit), False, config


if __name__ == "__main__":
    code, in_process, app = cli()
    sys.stdout.flush()
    sys.stderr.flush()
    if in_process and hasattr(app, "at_end"):
        app.at_end()
    if getattr(app, "HARD_EXIT", False):
        os._exit(code)
    sys.exit(code)
