# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine of the ceilings (_ceilings.py): a count above its ceiling is
red and names the new lines, a count below it is red too and says the
ceiling to write, the base knows a line by its content and not by its
number, and the ready-made counts find what they are for.

Everything on a made-up repository in a temporary folder."""

import tempfile
import unittest
from pathlib import Path

import _ceilings

SOURCE = '''\
import time


def wait():
    time.sleep(1)


def quiet():
    try:
        wait()
    except Exception:
        pass
    try:
        wait()
    except OSError:
        pass
    try:
        wait()
    except BaseException as error:
        print(error)
    try:
        wait()
    except:  # noqa: E722
        ...
'''


class OnAMadeUpRepository(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "src").mkdir()
        self.module = self.root / "src" / "work.py"
        self.module.write_text(SOURCE, encoding="utf-8")
        self.base = self.root / "tests" / "census_base.json"
        self.base.parent.mkdir()

    def files(self):
        return _ceilings.python_files(self.root / "src")

    def census(self, ceiling: int) -> _ceilings.Census:
        guards = {"sleeps": _ceilings.Guard("fixed waits", "a wait on a condition",
                                            lambda: _ceilings.scan(self.root, self.files(), r"time\.sleep\(", "wait_until"),
                                            rule="rule 10")}
        return _ceilings.Census(self.root, self.base, guards, {"sleeps": ceiling}, "python tests/test_census.py")


class TheReadyMadeCounts(OnAMadeUpRepository):
    def test_scan_gives_path_line_and_what_to_use(self):
        self.assertEqual(_ceilings.scan(self.root, self.files(), r"time\.sleep\(", "wait_until"),
                         ["src/work.py:5  -> wait_until"])
        self.assertEqual(_ceilings.scan(self.root, self.files(), r"^import "), ["src/work.py:1"])

    def test_an_except_that_takes_everything_and_does_nothing_is_found(self):
        found = _ceilings.swallowed_errors(self.root, self.files(), "say it")
        # Exception with pass and the bare except with ...; not the narrow OSError, not the one that prints
        self.assertEqual(found, ["src/work.py:11  -> say it", "src/work.py:23  -> say it"])

    def test_a_module_over_the_limit_is_found_with_its_lines(self):
        self.assertEqual(_ceilings.long_modules(self.root, self.files(), 100), [])
        found = _ceilings.long_modules(self.root, self.files(), 10, "split it")
        self.assertEqual(found, ["src/work.py  24 lines, over 10 -> split it"])

    def test_the_copies_of_the_models_files_are_not_the_apps_own(self):
        copy = self.root / "src" / "shared.py"
        copy.write_text("", encoding="utf-8")
        self.assertEqual(_ceilings.own(self.root, self.files()), [copy, self.module])      # no manifest: all its own
        (self.root / "impronte").mkdir()
        (self.root / "impronte" / "comune.json").write_text(
            '{"format": 1, "files": [{"source": "comune/src/shared.py", "target": "src/shared.py"}]}', encoding="utf-8")
        self.assertEqual(_ceilings.own(self.root, self.files()), [self.module])

    def test_the_caches_are_not_python_files(self):
        (self.root / "src" / "__pycache__").mkdir()
        (self.root / "src" / "__pycache__" / "old.py").write_text("", encoding="utf-8")
        self.assertEqual(self.files(), [self.module])


class TheCeilings(OnAMadeUpRepository):
    def test_a_count_at_its_ceiling_is_green(self):
        census = self.census(1)
        census.write_base()
        self.assertIsNone(census.problem("sleeps"))
        self.assertEqual(census.base_problems(), [])
        self.assertEqual(census.main([], out=lambda _line: None), 0)

    def test_a_count_above_its_ceiling_is_red_and_names_only_the_new_line(self):
        self.census(1).write_base()
        # a line added above moves the known one down: it is still known, by its content
        self.module.write_text(SOURCE.replace("import time\n", "import time\n\nPAUSE = 2\n")
                               + "\n\ndef again():\n    time.sleep(PAUSE)\n", encoding="utf-8")
        problem = self.census(1).problem("sleeps")
        self.assertIn("sleeps: rule 10, fixed waits: 2 today, ceiling 1 (1 too many).", problem)
        self.assertIn("To use: a wait on a condition.", problem)
        new = problem.split("New lines (not in census_base.json):\n")[1].split("\n  By file")[0]
        self.assertEqual(new.strip(), "src/work.py:30  -> wait_until")
        self.assertIn("By file: work.py 2", problem)
        self.assertIn("python tests/test_census.py --lines sleeps", problem)

    def test_a_count_below_its_ceiling_is_red_and_says_the_ceiling_to_write(self):
        problem = self.census(3).problem("sleeps")
        self.assertIn("1 today, ceiling 3: lower the ceiling to 1 in CEILINGS", problem)
        self.assertEqual(self.census(3).main([], out=lambda _line: None), 1)

    def test_without_a_base_every_line_looks_new_and_the_base_is_asked_for(self):
        census = self.census(0)
        self.assertIn("src/work.py:5", census.problem("sleeps"))
        self.assertEqual(len(census.base_problems()), 1)
        self.assertIn("--update", census.base_problems()[0])

    def test_guards_and_ceilings_name_the_same_rules(self):
        with self.assertRaises(ValueError):
            _ceilings.Census(self.root, self.base, {"a": _ceilings.Guard("t", "u", list)}, {"b": 0})


class TheCommandLine(OnAMadeUpRepository):
    def test_update_writes_the_base_and_lines_prints_one_guard(self):
        said = []
        census = self.census(1)
        self.assertEqual(census.main(["--update"], out=said.append), 0)
        self.assertEqual(census.base(), {"sleeps": ["src/work.py|time.sleep(1)"]})
        self.assertEqual(census.main(["--lines", "sleeps"], out=said.append), 0)
        self.assertEqual(said[-1], "src/work.py:5  -> wait_until")
        self.assertEqual(census.main(["--lines", "nothing"], out=said.append), 2)

    def test_the_table_says_what_to_do_for_each_guard(self):
        said = []
        self.census(0).write_base()
        self.assertEqual(self.census(0).main([], out=said.append), 1)
        self.assertIn("1 TOO MANY", said[0])
        self.assertTrue(any("1 today, ceiling 0" in line for line in said[1:]), said)


if __name__ == "__main__":
    unittest.main()
