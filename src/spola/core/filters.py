"""A pair's exclusions, read as rclone reads them: one answer to "is this
path left out of the sync?" for everything that needs it (the names Koofr
refuses, the folder monitor, the states for the file managers, sharing).

The file is rclone's own filter file (`--filter-from`), the default
exclusions and whatever was added by hand. What is read here is the
documented subset of rclone v1.75.2 fs/filter/filter.go and glob.go, held
against rclone's own listings by the tests (test_filters.py):

- "- pattern" and "+ pattern", the first rule that matches wins, a path no
  rule matches is included; comments ("#", ";") and "!" (forget the rules
  so far);
- a pattern that starts with "/" holds from the pair's root, another one
  at any depth; "*" (not across "/"), "**" (across), "?"; a pattern that
  ends with "/" is a folder with all it holds;
- an include rule lets in the folders that lead to it; a folder left out
  takes all it holds with it.

Not read: character classes ("[a-z]"), alternatives ("{a,b}"), escapes
("\\"), regular expressions ("{{...}}"), and lines that are no rule. In
the strict reading (sharing) such a file is refused whole (Unsupported).
In the tolerant one a path is left out only when a rule that is read says
so before any rule that is not: a path whose fate hangs on an unread rule
counts as included, as every path did before this module.
"""

from __future__ import annotations

import re

CACHED_FOLDERS = 50_000         # the folders whose answer is remembered, then the memory starts again


class Unsupported(ValueError):
    """The filters use syntax this module does not read."""


def _simple_glob(pattern: str) -> re.Pattern:
    """The documented name, *, ** and ? subset of rclone path globs.

    Reject advanced syntax rather than accidentally treat a regex, class,
    alternative or escape as a literal. Go's $ requires the absolute end
    of a string, unlike Python's $, hence \\Z below.
    """
    if any(c in pattern for c in "[]{}\\") or "***" in pattern:
        raise Unsupported("unsupported filter pattern")
    prefix = r"\A" if pattern.startswith("/") else r"(?:\A|/)"
    pattern = pattern[1:] if pattern.startswith("/") else pattern
    wildcards = {"*": "[^/]*", "**": ".*", "?": "[^/]"}
    body = "".join(wildcards.get(part, re.escape(part)) for part in re.split(r"(\*\*|\*|\?)", pattern))
    return re.compile(prefix + body + r"\Z")


def _directory_globs(pattern: str):
    """Parent directories that a simple include pattern needs to traverse.

    A ** can cross directories, so its prefix is itself a directory glob.
    An unanchored filename can occur in any directory.
    """
    pattern = re.sub(r"/+", "/", pattern)
    found = False
    while True:
        slash, recursive = pattern.rfind("/"), pattern.rfind("**")
        boundary = max(slash, recursive)
        if boundary < 0:
            if not found:
                yield "/**"
            return
        suffix = "**/" if recursive > slash else "/"
        pattern = pattern[:boundary]
        found = True
        if pattern + suffix != "/":
            yield pattern + suffix


def _merged(rules: list) -> list:
    """Neighbouring rules of the same kind as one expression: the first
    match still wins, with a few searches a path instead of one a rule."""
    merged: list = []
    for include, pattern in rules:
        if pattern is not None and merged and merged[-1][0] == include and merged[-1][1] is not None:
            merged[-1][1].append(pattern.pattern)
        else:
            merged.append([include, None if pattern is None else [pattern.pattern]])
    return [(include, None if parts is None else re.compile("|".join(f"(?:{part})" for part in parts)))
            for include, parts in merged]


def _first(path: str, rules: list) -> bool | None:
    """Included (True) or left out (False) by the first rule that matches;
    included when none does; None when a rule that is not read comes first."""
    for include, pattern in rules:
        if pattern is None:
            return None
        if pattern.search(path):
            return include
    return True


class Filters:
    """The rules of one filter file. `strict`: Unsupported for a file with
    anything that is not read, instead of the tolerant reading."""

    def __init__(self, text: str, strict: bool = False):
        files: list = []
        directories: list = []
        self.unread: list[str] = []         # the lines that are not read, as written
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line == "!":
                files.clear()
                directories.clear()
                continue
            try:
                if not line.startswith(("+ ", "- ")):
                    raise Unsupported("unsupported filter rule")
                include, pattern = line[0] == "+", line[2:]
                directory_rule = pattern.endswith("/")
                if directory_rule and not include:
                    pattern += "**"
                recursive = "**" in pattern
                rule = (include, _simple_glob(pattern))
                parents = ([(include, _simple_glob(glob)) for glob in _directory_globs(pattern)]
                           if (not directory_rule or recursive) and (include or pattern == "*") else [])
            except Unsupported:
                if strict:
                    raise
                # what it would let in or leave out is not known: nor is the
                # fate of any path that reaches it
                self.unread.append(line)
                files.append((False, None))
                directories.append((False, None))
                continue
            if not directory_rule or recursive:
                files.append(rule)
                directories.extend(parents)
            if directory_rule or recursive:
                directories.append(rule)
        self._files, self._directories = _merged(files), _merged(directories)
        self._folders: dict[str, bool | None] = {}

    def _folder(self, path: str) -> bool | None:
        """`path` ("a/b/"), by the folder rules alone: asked for every file
        in it, answered once."""
        try:
            return self._folders[path]
        except KeyError:
            if len(self._folders) >= CACHED_FOLDERS:
                self._folders.clear()
            answer = self._folders[path] = _first(path, self._directories)
            return answer

    def includes(self, relative: str, is_dir: bool) -> bool | None:
        """Would rclone sync this path (relative to the pair's root, with
        "/")? True, False, or None when a rule that is not read decides."""
        parts = relative.split("/")
        unknown = False
        for depth in range(1, len(parts) + (1 if is_dir else 0)):
            answer = self._folder("/".join(parts[:depth]) + "/")
            if answer is False:
                return False        # a folder left out takes all it holds with it
            unknown = unknown or answer is None
        if not is_dir:
            answer = _first(relative, self._files)
            if answer is False:
                return False
            unknown = unknown or answer is None
        return None if unknown else True

    def excluded(self, rel: str) -> bool:
        """Is this path left out for sure? `rel` is relative to the pair's
        root; a folder may end with "/" (without it, it is read as a file:
        "node_modules/**" leaves out the folder's content, and the folder
        with it only when it is said to be one)."""
        is_dir = rel.endswith("/")
        rel = rel.strip("/")
        return bool(rel) and self.includes(rel, is_dir) is False
