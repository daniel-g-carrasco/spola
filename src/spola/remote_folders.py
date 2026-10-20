"""Browsing the folders on Koofr, for choosing the Koofr side of a new pair:
one level at a time, with suggestions and with the folders that cannot be
chosen (already part of a pair) marked as such.

Two backends: Koofr's API (read-only listing, plus creating a folder when
the user asks), and a local folder standing in for Koofr in development
(SPOLA_DEV_REMOTE), so the dialog can be tried and tested without an account.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from . import koofr_api, places
from .config import normalize_remote
from .i18n import _
from .places import remote_within as _within


def join(parent: str, name: str) -> str:
    return normalize_remote(parent.rstrip("/") + "/" + name)


def _parts(path: str) -> list[str]:
    return [p for p in normalize_remote(path).split("/") if p]


def resolve(list_folders, path: str) -> str | None:
    """`path` as Koofr spells it (Koofr ignores case), or None if a level
    is missing. `list_folders(path)` gives the folder names inside path."""
    current = "/"
    for part in _parts(path):
        match = next((n for n in list_folders(current) if n.casefold() == part.casefold()), None)
        if match is None:
            return None
        current = join(current, match)
    return current


def ensure(folders, path: str) -> str:
    """Create the levels of `path` that are missing on Koofr (bisync needs
    both folders to exist); returns the path as Koofr spells it."""
    current = "/"
    for part in _parts(path):
        match = next((n for n in folders.list(current) if n.casefold() == part.casefold()), None)
        if match is None:
            folders.create(current, part)
            match = part
        current = join(current, match)
    return current


class KoofrFolders:
    def __init__(self, user: str, password: str, mount_id: str):
        self.user, self.password, self.mount_id = user, password, mount_id

    def list(self, path: str) -> list[str]:
        if not self.mount_id:
            self.mount_id = koofr_api.primary_mount_id(self.user, self.password)
        return koofr_api.list_folders(self.user, self.password, self.mount_id, path)

    def create(self, parent: str, name: str) -> None:
        koofr_api.create_folder(self.user, self.password, self.mount_id, parent, name)


class LocalFolders:
    """A local folder standing in for Koofr (development and tests)."""

    def __init__(self, root: str):
        self.root = root

    def _real(self, path: str) -> str:
        return os.path.join(self.root, normalize_remote(path).lstrip("/"))

    def list(self, path: str) -> list[str]:
        try:
            return sorted((e.name for e in os.scandir(self._real(path)) if e.is_dir()), key=str.casefold)
        except FileNotFoundError:
            return []

    def create(self, parent: str, name: str) -> None:
        os.makedirs(os.path.join(self._real(parent), name), exist_ok=False)


class CachedFolders:
    """A backend that lists each folder once (going back and forth in the
    browser costs no request); creating a folder forgets its parent."""

    def __init__(self, backend):
        self.backend = backend
        self.listed: dict[str, list[str]] = {}

    def list(self, path: str) -> list[str]:
        path = normalize_remote(path)
        if path not in self.listed:
            self.listed[path] = self.backend.list(path)
        return self.listed[path]

    def create(self, parent: str, name: str) -> None:
        self.backend.create(normalize_remote(parent), name)
        self.listed.pop(normalize_remote(parent), None)


@dataclass(frozen=True)
class Suggestion:
    path: str
    reason: str
    exists: bool = True     # False: the same place as here, still to be created on Koofr


def blocked(remote: str, pairs: Iterable) -> tuple[str, str]:
    """(kind, reason) when this Koofr folder cannot be the Koofr side of a
    new pair, else ("", ""). Kinds: "root"; "synced" (it is, or is inside,
    the Koofr folder of a pair: nothing below can be chosen either);
    "contains" (a pair's Koofr folder is below: other subfolders can be)."""
    remote = normalize_remote(remote)
    if remote == "/":
        return "root", _("The whole Koofr storage cannot be synced: choose a folder.")
    for pair in pairs:
        if pair.remote.casefold() == remote.casefold():
            return "synced", _("Already synced with {folder}").format(folder=_home_short(pair.local))
        if _within(remote, pair.remote):
            return "synced", _("Inside a folder synced with {folder}").format(folder=_home_short(pair.local))
    for pair in pairs:
        if _within(pair.remote, remote):
            return "contains", _("Contains a folder synced with {folder}").format(folder=_home_short(pair.local))
    return "", ""


def blocked_reason(remote: str, pairs: Iterable) -> str:
    return blocked(remote, pairs)[1]


def _home_short(path: str) -> str:
    return places.home_relative(path, os.path.realpath(os.path.expanduser("~")))


def suggestions(folders, local: str, pairs: Iterable, home: str | None = None,
                limit: int = 4) -> list[Suggestion]:
    """Koofr folders that look like the match of `local`:
      1. the same path relative to the home folder (~/data/work -> /data/work),
         offered for creation when it is not there yet;
      2. a folder with the same name in the first two levels of Koofr.
    Folders already part of a pair are left out."""
    pairs = list(pairs)
    home = os.path.realpath(home or os.path.expanduser("~"))
    local = os.path.realpath(local)
    name = os.path.basename(local).casefold()
    found: list[Suggestion] = []
    listed: dict[str, list[str]] = {}

    def children(path: str) -> list[str]:
        # errors go up: offering to create a folder Koofr could not list would mislead
        if path not in listed:
            listed[path] = folders.list(path)
        return listed[path]

    def add(path: str, reason: str, exists: bool = True) -> None:
        if path.casefold() not in {s.path.casefold() for s in found} and not blocked_reason(path, pairs):
            found.append(Suggestion(path, reason, exists))

    in_home = places.relative(local, home)
    if in_home:
        same = normalize_remote(in_home)
        spelled = resolve(children, same)
        if spelled:
            add(spelled, _("Same place as on this computer"))
        else:
            add(same, _("Same place as on this computer, to be created on Koofr"), exists=False)
    for first in children("/"):
        if first.casefold() == name:
            add(join("/", first), _("Same name"))
        for second in children(join("/", first)):
            if second.casefold() == name:
                add(join(join("/", first), second), _("Same name"))
        if len(found) >= limit:
            break
    return found[:limit]
