"""The sync state of every path of a pair, for the file manager emblems.

The last listing bisync wrote is the memory of what was in sync; on top of
it a few small sets say what is happening now. Folders take the most serious
state found below them, which is found by looking at those small sets only,
so a folder state costs nothing even in a tree of 50,000 entries.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Iterable


class State(enum.IntEnum):
    NONE = 0        # not part of a pair, or unknown
    SYNCED = 1
    SYNCING = 2
    PENDING = 3     # changed here, not yet synced
    ERROR = 4
    CONFLICT = 5
    EXCLUDED = 6

    @classmethod
    def worst(cls, states: Iterable["State"]) -> "State":
        """Most serious first: error, conflict, syncing, pending, synced."""
        order = (cls.ERROR, cls.CONFLICT, cls.SYNCING, cls.PENDING, cls.SYNCED, cls.EXCLUDED, cls.NONE)
        found = set(states)
        return next((s for s in order if s in found), cls.NONE)


def _under(path: str, folder: str) -> bool:
    return folder == "" or path == folder or path.startswith(folder + "/")


class PathStates:
    def __init__(self, excluded=lambda rel: False):
        self.listed: set[str] = set()        # files and folders in the last listing (synced)
        self.pending: set[str] = set()
        self.syncing: set[str] = set()
        self.errors: set[str] = set()
        self.conflicts: set[str] = set()
        self.refused: set[str] = set()
        self.excluded = excluded             # the pair's exclusions (names, folders)

    def load_listing(self, paths: Iterable[str]) -> None:
        self.listed = set(paths)

    def state_of(self, rel: str, is_dir: bool = False) -> State:
        rel = rel.strip("/")
        if self._refused(rel) or self.excluded(rel + ("/" if is_dir else "")):
            return State.EXCLUDED
        if is_dir:
            below = [State.worst(self._states_below(rel))]
            if below[0] is not State.NONE:
                return below[0]
            return State.SYNCED if rel in self.listed or rel == "" else State.PENDING
        if rel in self.conflicts:
            return State.CONFLICT
        if rel in self.errors:
            return State.ERROR
        if rel in self.syncing:
            return State.SYNCING
        if rel in self.pending:
            return State.PENDING
        return State.SYNCED if rel in self.listed else State.PENDING

    def _refused(self, rel: str) -> bool:
        return any(_under(rel, r) for r in self.refused)

    def _states_below(self, folder: str) -> Iterable[State]:
        for state, paths in ((State.ERROR, self.errors), (State.CONFLICT, self.conflicts),
                             (State.SYNCING, self.syncing), (State.PENDING, self.pending)):
            if any(p != folder and _under(p, folder) and not self._refused(p) and not self.excluded(p)
                   for p in paths):
                yield state

    def folder_states(self, folder: str, children: Iterable[tuple[str, bool]]) -> dict[str, State]:
        """name -> state for the children of `folder` (relative), given
        (name, is_dir) pairs from the file manager or a directory scan."""
        folder = folder.strip("/")
        return {name: self.state_of(os.path.join(folder, name) if folder else name, is_dir)
                for name, is_dir in children}

    # -- changes, each returning the paths whose state may have changed -------------

    def mark_pending(self, rel: str) -> list[str]:
        rel = rel.strip("/")
        if not rel:
            return []
        self.pending.add(rel)
        return ancestors_and_self(rel)

    def run_started(self, syncing: Iterable[str]) -> list[str]:
        self.syncing = {p.strip("/") for p in syncing}
        changed = set()
        for p in self.syncing:
            changed.update(ancestors_and_self(p))
        return sorted(changed)

    def run_finished(self, listing: Iterable[str], touched: Iterable[str], errors: Iterable[str],
                     conflicts: Iterable[str], keep_pending: Iterable[str]) -> list[str]:
        """After a successful run: the listing is the new truth; what was
        pending is synced unless it changed again during the run."""
        changed = set()
        for group in (self.pending, self.syncing, self.errors, self.conflicts, set(touched)):
            for p in group:
                changed.update(ancestors_and_self(p))
        self.load_listing(listing)
        self.syncing = set()
        self.pending = {p.strip("/") for p in keep_pending}
        self.errors = {p.strip("/") for p in errors}
        self.conflicts = {p.strip("/") for p in conflicts}
        for group in (self.pending, self.errors, self.conflicts):
            for p in group:
                changed.update(ancestors_and_self(p))
        return sorted(changed)


def ancestors_and_self(rel: str) -> list[str]:
    """"a/b/c" -> ["", "a", "a/b", "a/b/c"] ("" is the root)."""
    rel = rel.strip("/")
    parts = rel.split("/") if rel else []
    return [""] + ["/".join(parts[:i + 1]) for i in range(len(parts))]
