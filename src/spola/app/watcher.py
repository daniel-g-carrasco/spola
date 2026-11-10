"""Recursive change monitor for a pair's local folder.

inotify (through Gio.FileMonitor) watches one directory at a time, so the
tree gets one monitor per folder, added and removed as folders come and go.
The kernel limit is per user (fs.inotify.max_user_watches); past a budget we
stop adding monitors and the pair falls back to the periodic check only.
On Windows the whole tree has one monitor instead (winwatch.py).

The folders are found with os.walk on the main loop for WALK_BUDGET_S at
most: a small tree is watched at once, as before; the rest of a big one
(tens of thousands of folders take seconds) is listed in a thread and
watched a batch at a time, the window going on meanwhile. Changes in a
folder not watched yet are caught by the periodic check, as past the budget.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

WALK_BUDGET_S = 0.05
BATCH = 200

EVENTS = {
    Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
    Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_IN,
    Gio.FileMonitorEvent.MOVED_OUT, Gio.FileMonitorEvent.RENAMED,
}


def watch_budget() -> int:
    """Use at most half of the user's inotify watches, leaving the rest to
    the file manager, the editor and everybody else."""
    try:
        with open("/proc/sys/fs/inotify/max_user_watches") as f:
            return int(f.read()) // 2
    except (OSError, ValueError):
        return 4096


class GioTreeWatcher:
    def __init__(self, root: str, on_change: Callable[[str], None],
                 ignore: Callable[[str], bool] = lambda rel: False, budget: int | None = None):
        self.root = root.rstrip("/")
        self.on_change = on_change
        self.ignore = ignore
        self.budget = budget if budget is not None else watch_budget()
        self.overflow = False
        self._monitors: dict[str, Gio.FileMonitor] = {}
        self._generation = 0        # a walk of a stopped watcher adds nothing

    def start(self) -> None:
        self._add_tree(self.root)

    def stop(self) -> None:
        self._generation += 1
        for monitor in self._monitors.values():
            monitor.cancel()
        self._monitors.clear()

    @property
    def count(self) -> int:
        return len(self._monitors)

    def _rel(self, path: str) -> str:
        return os.path.relpath(path, self.root) if path != self.root else ""

    def _folders(self, top: str):
        """The folders to watch below top (itself first), parents before
        children: not the excluded ones, not through links."""
        for dirpath, dirnames, _files in os.walk(top):
            rel = self._rel(dirpath)
            if rel and self.ignore(rel + "/"):
                dirnames[:] = []
                continue
            yield dirpath
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]

    def _add_tree(self, top: str) -> None:
        folders, generation = self._folders(top), self._generation
        deadline = time.monotonic() + WALK_BUDGET_S
        for path in folders:
            if not self._add(path):
                return
            if time.monotonic() > deadline:
                break
        else:
            return                  # the whole tree, at once

        def rest() -> None:         # the same walk, on in this thread
            try:
                found = list(folders)
            except OSError:
                return
            GLib.idle_add(self._add_batch, found, 0, generation)
        threading.Thread(target=rest, name="watch-walk", daemon=True).start()

    def _add_batch(self, folders: list[str], start: int, generation: int) -> bool:
        if generation != self._generation:
            return False            # stopped meanwhile
        for path in folders[start:start + BATCH]:
            if not self._add(path):
                return False
        if start + BATCH < len(folders):
            GLib.idle_add(self._add_batch, folders, start + BATCH, generation)
        return False

    def _add(self, path: str) -> bool:
        if path in self._monitors:
            return True
        if len(self._monitors) >= self.budget:
            self.overflow = True
            return False
        try:
            monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except Exception:
            return True     # unreadable folder: rclone will report it
        monitor.connect("changed", self._on_changed)
        self._monitors[path] = monitor
        return True

    def _remove_tree(self, top: str) -> None:
        prefix = top.rstrip("/") + "/"
        for path in [p for p in self._monitors if p == top or p.startswith(prefix)]:
            self._monitors.pop(path).cancel()

    def _on_changed(self, _monitor, file, other, event) -> None:
        if event not in EVENTS:
            return
        path = file.get_path()
        if not path:
            return
        if event in (Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.MOVED_OUT):
            self._remove_tree(path)
        elif event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.MOVED_IN) and os.path.isdir(path) \
                and not os.path.islink(path):
            self._add_tree(path)
        elif event is Gio.FileMonitorEvent.RENAMED and other is not None:
            self._remove_tree(path)
            new = other.get_path()
            if new and os.path.isdir(new) and not os.path.islink(new):
                self._add_tree(new)
            if new and not self.ignore(self._rel(new)):
                self.on_change(self._rel(new))
        rel = self._rel(path)
        if not self.ignore(rel):
            self.on_change(rel)


if sys.platform == "win32":
    from .winwatch import WinTreeWatcher as TreeWatcher
else:
    TreeWatcher = GioTreeWatcher
