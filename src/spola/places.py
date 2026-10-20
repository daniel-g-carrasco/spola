"""Where things are: a path inside another, on this computer and on Koofr;
the pair a local path belongs to, and the path inside it; a path in the
home folder as "~/…"; a free name next to a file. One place for what
grouping, remote_folders, preflight, explorer, dbus_service, ui, versions
and conflicts each did their own way, some with a fixed "/" that on
Windows found nothing (no "~", no "Same place as on this computer",
groups of the window never merged).

On Windows a local path is the same whatever the case of its letters and
its separators (C:\\Users\\me and c:/users/ME), as Windows holds; the case
is folded with lower(), as NTFS does it letter by letter (casefold() would
make "ß" and "ss" one). On Koofr a path is the same whatever its case.
No GTK here.
"""

from __future__ import annotations

import os

WINDOWS = os.name == "nt"


def local_key(path: str) -> str:
    """A local path as compared: normalized, "/" between names and none at
    the end, and on Windows its letters in lower case."""
    path = os.path.normpath(path)
    if WINDOWS:
        path = path.replace("\\", "/").lower()
    return path.rstrip("/") or "/"


def within(child: str, parent: str) -> bool:
    """A local path inside another, or the same one."""
    child, parent = local_key(child), local_key(parent)
    return child == parent or parent == "/" or child.startswith(parent + "/")


def relative(path: str, root: str) -> str | None:
    """`path` inside `root`, relative to it with "/" ("" for the root
    itself), or None when it is not inside it."""
    if not within(path, root):
        return None
    if local_key(path) == local_key(root):
        return ""
    rel = os.path.relpath(path, root)       # on Windows, whatever the case
    return rel.replace("\\", "/") if WINDOWS else rel


def home_relative(path: str, home: str) -> str:
    """"~/data/work" for a path in the home folder (with the path's own
    separator), the path itself anywhere else."""
    rel = relative(path, home)
    if rel is None:
        return path
    if not rel:
        return "~"
    sep = "\\" if WINDOWS and "\\" in path else "/"
    return "~" + sep + rel.replace("/", sep)


def locate(pairs, path: str):
    """(pair, path relative to its root, with "/") of a local path inside
    one of the pairs, or None."""
    for pair in pairs:
        rel = relative(path, pair.local)
        if rel is not None:
            return pair, rel
    return None


def remote_within(child: str, parent: str) -> bool:
    """A Koofr path inside another, or the same one: Koofr ignores case,
    /Data/Work is inside /data."""
    child, parent = (child.rstrip("/") or "/").casefold(), (parent.rstrip("/") or "/").casefold()
    return child == parent or parent == "/" or child.startswith(parent + "/")


def free_name(root: str, rel: str) -> str:
    """`rel` if nothing is there, else "name (2).ext", "name (3).ext"...;
    FileExistsError when a thousand are taken."""
    if not os.path.lexists(os.path.join(root, rel)):
        return rel
    stem, ext = os.path.splitext(rel)
    for i in range(2, 1000):
        candidate = f"{stem} ({i}){ext}"
        if not os.path.lexists(os.path.join(root, candidate)):
            return candidate
    raise FileExistsError(os.path.join(root, rel))
