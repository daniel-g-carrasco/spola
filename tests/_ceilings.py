# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine of the ceilings: what is outside a house rule is counted, and
the count may only go down ("never more than today").

An app writes its guards in a test of its own (tests/test_census.py): a
name, the rule in words, what to use instead, and a function that gives
the lines outside the rule today, each as "path:line  what to use" (the
path from the root of the repository). Next to them CEILINGS, a number
for each guard. The test is red when
- a guard counts more than its ceiling: the message names the lines that
  are new (the ones the base file does not know), with what to use;
- a guard counts less than its ceiling: "lower the ceiling to N". So a
  piece of work that pays a debt writes down what it gained, and the
  next one cannot spend it again.
A ceiling is lowered by hand, in the test's source, with the reason in a
comment next to it; raising one shows in the diff, and is never done to
make the test pass.

The base file (JSON, next to the test) holds the lines known today by
path and content, not by line number, which moves. It only serves the
message of a red guard: without it every line would look new.

    census = _ceilings.Census(ROOT, base_file, GUARDS, CEILINGS, "python tests/test_census.py")
    census.problem(key)        the message of a red guard, or None: what the test asserts
    census.main(argv)          the command line of the test's file:
        (nothing)              every guard with today's count, its ceiling and what to do
        --update               rewrite the base file with the lines of today
        --lines KEY            every line of one guard

Ready-made counts for the rules every app has: scan() (a pattern in
files), swallowed_errors() (an except that takes everything and does
nothing), long_modules() (files over a number of lines); own() leaves out
the copies of the model's files, which an app does not change.
"""

from __future__ import annotations

import ast
import collections
import json
import re
from pathlib import Path


class Guard:
    """A rule and its count: `compute()` gives the lines outside the rule,
    "path:line  detail"; `use` says what to write instead."""

    def __init__(self, title: str, use: str, compute, rule: str = ""):
        self.title, self.use, self.compute, self.rule = title, use, compute, rule


def python_files(folder: Path) -> list[Path]:
    return sorted(p for p in Path(folder).rglob("*.py") if "__pycache__" not in p.parts)


def own(root: Path, files) -> list[Path]:
    """The files that are the app's own: not the copies of the model's
    files (the targets of impronte/*.json), which are changed in the model
    and counted there."""
    copies = set()
    for manifest in sorted((Path(root) / "impronte").glob("*.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            copies |= {item["target"] for item in data.get("files", []) if isinstance(item, dict) and "target" in item}
    return [p for p in files if Path(p).relative_to(root).as_posix() not in copies]


def scan(root: Path, files, pattern: str, use: str = "") -> list[str]:
    """Every line of `files` where `pattern` is found, as "path:line" (and
    "  -> use" when given)."""
    rx = re.compile(pattern)
    tail = f"  -> {use}" if use else ""
    return [f"{Path(p).relative_to(root).as_posix()}:{n}{tail}" for p in files
            for n, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]


def swallowed_errors(root: Path, files, use: str = "say it: the log, a message, a narrower except") -> list[str]:
    """Every `except:`, `except Exception:` or `except BaseException:`
    whose body does nothing (pass, ...): a failure nobody hears of."""
    found = []
    for path in files:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            wide = node.type is None or (isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"))
            idle = all(isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                       for s in node.body)
            if wide and idle:
                found.append(f"{Path(path).relative_to(root).as_posix()}:{node.lineno}  -> {use}")
    return sorted(found)


def long_modules(root: Path, files, limit: int, use: str = "one module for each subject") -> list[str]:
    """Every file with more than `limit` lines, as "path  N lines"."""
    found = []
    for path in files:
        lines = len(Path(path).read_text(encoding="utf-8").splitlines())
        if lines > limit:
            found.append(f"{Path(path).relative_to(root).as_posix()}  {lines} lines, over {limit} -> {use}")
    return found


class Census:
    """The guards of an app with their ceilings; every count is made once."""

    def __init__(self, root: Path, base_file: Path, guards: dict[str, Guard], ceilings: dict[str, int],
                 command: str = ""):
        if set(guards) != set(ceilings):
            raise ValueError("guards and ceilings name different rules: "
                             + ", ".join(sorted(set(guards) ^ set(ceilings))))
        self.root, self.base_file, self.guards, self.ceilings = Path(root), Path(base_file), guards, ceilings
        self.command = command or "python tests/test_census.py"
        self._lines: dict[str, list[str]] = {}
        self._files: dict[str, list[str]] = {}

    def lines(self, key: str) -> list[str]:
        if key not in self._lines:
            self._lines[key] = list(self.guards[key].compute())
        return self._lines[key]

    def identity(self, line: str) -> str:
        """A line as the base knows it: path and content, not its number."""
        ref = line.split("  ")[0]
        match = re.match(r"^(.*?):(\d+)$", ref)
        if not match:
            return ref
        path, number = match.group(1), int(match.group(2))
        if path not in self._files:
            try:
                self._files[path] = (self.root / path).read_text(encoding="utf-8").splitlines()
            except OSError:
                self._files[path] = []
        text = self._files[path][number - 1].strip() if 0 < number <= len(self._files[path]) else ""
        return f"{path}|{text}"

    def base(self) -> dict:
        try:
            return json.loads(self.base_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def today(self) -> dict:
        return {key: sorted({self.identity(x) for x in self.lines(key)}) for key in self.guards}

    def write_base(self) -> None:
        self.base_file.write_text(json.dumps(self.today(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8",
                                  newline="\n")

    def base_problems(self) -> list[str]:
        """What is wrong with the base file itself: a guard it does not
        know, one that is gone."""
        base = self.base()
        if set(base) != set(self.guards):
            return [f"{self.base_file.name} is missing or old ({', '.join(sorted(set(base) ^ set(self.guards)))}): "
                    f"{self.command} --update"]
        return []

    def report(self, key: str) -> str:
        guard, ceiling, lines = self.guards[key], self.ceilings[key], self.lines(key)
        known = set(self.base().get(key, []))
        new = [x for x in lines if self.identity(x) not in known]
        per_file = collections.Counter(x.split("  ")[0].rsplit(":", 1)[0] for x in lines)
        rule = f"{guard.rule}, " if guard.rule else ""
        text = [f"{key}: {rule}{guard.title}: {len(lines)} today, ceiling {ceiling} ({len(lines) - ceiling} too many).",
                f"  To use: {guard.use}."]
        if new:
            text.append(f"  New lines (not in {self.base_file.name}):")
            text += [f"    {x}" for x in new]
        else:
            text.append(f"  No line is new for {self.base_file.name} (an old base? {self.command} --update).")
        text.append("  By file: " + ", ".join(f"{Path(f).name} {n}" for f, n in per_file.most_common()))
        text.append(f"  Every line: {self.command} --lines {key}")
        return "\n".join(text)

    def problem(self, key: str) -> str | None:
        """Why the guard is red, or None: above its ceiling (with the new
        lines), or below it (the ceiling to write)."""
        count, ceiling = len(self.lines(key)), self.ceilings[key]
        if count > ceiling:
            return self.report(key)
        if count < ceiling:
            return (f"{key}: {count} today, ceiling {ceiling}: lower the ceiling to {count} in CEILINGS and write the "
                    f"base again ({self.command} --update)")
        return None

    def main(self, argv: list[str], out=print) -> int:
        if "--update" in argv:
            self.write_base()
            out(f"{self.base_file} written")
            return 0
        if "--lines" in argv:
            key = argv[argv.index("--lines") + 1] if argv.index("--lines") + 1 < len(argv) else ""
            if key not in self.guards:
                out(f"--lines needs a guard: {', '.join(self.guards)}")
                return 2
            out("\n".join(self.lines(key)) or "none")
            return 0
        red = 0
        width = max(len(key) for key in self.guards)
        for key, ceiling in self.ceilings.items():
            count = len(self.lines(key))
            state = ("ok" if count == ceiling else f"lower the ceiling to {count}" if count < ceiling
                     else f"{count - ceiling} TOO MANY")
            red += count != ceiling
            out(f"  {key:<{width}} {count:5}  ceiling {ceiling:5}  {state}")
        for key in self.ceilings:
            if len(self.lines(key)) > self.ceilings[key]:
                out("\n" + self.report(key))
        for line in self.base_problems():
            out("\n" + line)
            red += 1
        return 1 if red else 0
