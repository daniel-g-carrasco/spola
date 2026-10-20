"""The paths of a preview as a tree of folders, for the review of a large
alignment or deletion: thousands of full paths one under the other say
little, the folders they are in with how many files and how much each
holds say what is going on. No GTK here (the window shows it).

A path is a folder when the preview said so (a folder to remove or make)
or when other paths are inside it. A chain of folders that only hold one
folder each is one node ("electrical-design / 10-clients / mcm"), as
other file managers show it, so that what matters is not three levels
down."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from .i18n import _

CHAIN = " / "


@dataclass
class Node:
    name: str                   # the name shown: one name, or a chain of folders
    path: str                   # relative to the folder pair's root
    folder: bool
    files: int = 0              # files at any depth below (1 for a file)
    size: int = 0               # their bytes, as far as the preview knew them
    note: str = ""              # a file's line under its name ("From …" for a move)
    show: str = ""              # what a click shows: the file where it is now
    total: int = 0              # a folder's files before the run, with the new ones (0: unknown)
    children: list[Node] = field(default_factory=list)


def folder_counts(files: Iterable[str]) -> Counter:
    """How many of these files each folder holds, at any depth ("" is the
    root). For a side's last listing, tens of thousands of files: counted
    once, off the main loop, and given to build for every list."""
    inside = Counter()
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            inside["/".join(parts[:i])] += 1
        inside[""] += 1
    return inside


def _as_set(files):
    return files if isinstance(files, (set, frozenset, dict)) else set(files)


def build(entries: Iterable[tuple[str, str, str]], sizes: dict[str, int] | None = None,
          folders: Iterable[str] = (), base: Iterable[str] | None = None,
          base_counts: Counter | None = None) -> list[Node]:
    """`entries`: (path, note, path to show). The top nodes, folders first.
    `base`: the files of that side at the last run, for each folder's total
    (what share of it the change touches); `base_counts`: folder_counts of
    them, when already counted."""
    sizes = sizes or {}
    entries = list(entries)
    known_folders = set(folders)
    for path, _note, _show in entries:
        parts = path.split("/")
        for i in range(1, len(parts)):
            known_folders.add("/".join(parts[:i]))
    root = Node("", "", True)
    index: dict[str, Node] = {"": root}

    def node_for(path: str) -> Node:
        if path in index:
            return index[path]
        parent_path, _sep, name = path.rpartition("/")
        parent = node_for(parent_path)
        node = Node(name, path, path in known_folders)
        parent.children.append(node)
        index[path] = node
        return node

    for path, note, show in entries:
        node = node_for(path)
        node.folder = node.folder or path in known_folders
        if not node.folder:
            node.note, node.show, node.size = note, show or path, sizes.get(path, 0)
    _total(root)
    if base is not None:
        # the files of each folder: those of the last run, with the new ones
        known = _as_set(base)
        if base_counts is None:
            base_counts = folder_counts(known)
        new = folder_counts(n.path for n in index.values() if not n.folder and n.path not in known)
        for path, node in index.items():
            if node.folder:
                node.total = base_counts[path] + new[path]
    _tidy(root)
    return root.children


def whole(entries: Iterable[str], base: Iterable[str]) -> int:
    """How many files the whole synced folder has, the new ones included."""
    known = _as_set(base)
    return len(known) + len({path for path in entries if path not in known})


def share(files: int, total: int) -> str:
    """How much of a folder a change touches, in words: "the whole folder",
    "37% of the folder" (never 100% when one file is left, never 0%)."""
    if not total or not files:
        return ""
    if files >= total:
        return _("the whole folder")
    percent = files * 100 // total
    if percent < 1:
        return _("less than 1% of the folder")
    return _("{percent}% of the folder").format(percent=percent)


def _total(node: Node) -> None:
    """Files and bytes of each folder, from the files below it."""
    if not node.folder:
        node.files = 1
        return
    node.files = node.size = 0
    for child in node.children:
        _total(child)
        node.files += child.files
        node.size += child.size


def natural(name: str) -> list:
    """Names in the order people expect, as file managers do: "old-2"
    before "old-10", case aside."""
    return [int(part) if i % 2 else part.casefold() for i, part in enumerate(re.split(r"(\d+)", name))]


def _tidy(node: Node) -> None:
    node.children.sort(key=lambda n: (not n.folder, natural(n.name)))
    for child in node.children:
        _tidy(child)
    merged = []
    for child in node.children:
        # a folder holding one folder only (and no file): one line for both
        while child.folder and len(child.children) == 1 and child.children[0].folder:
            only = child.children[0]
            # the row shows the last folder's content: its share is of that folder
            child = Node(child.name + CHAIN + only.name, only.path, True, only.files, only.size,
                         total=only.total, children=only.children)
        merged.append(child)
    node.children = merged


def totals(nodes: list[Node]) -> tuple[int, int]:
    """(files, bytes) of a list of top nodes."""
    return sum(n.files for n in nodes), sum(n.size for n in nodes)
