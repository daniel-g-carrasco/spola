"""The synced folders' status in File Explorer, kept up to date (Windows).

One worker thread does all of it, in the Windows Runtime's multithreaded
apartment, one job after the other: the shell's calls block, and a whole
tree marked in sync takes a while. The jobs:

  * the sync roots made to match the pairs: one registered and connected
    per pair as soon as it is added, so that what is not yet in sync shows
    waiting from the start, as other sync clients do; Spola's ones without
    a pair unregistered; all of them when the preference is off;
  * marked in sync as soon as a run copies it: a file then is the same on
    both sides (also during the first alignment);
  * marked in sync, at each connection and after each run that went well:
    what bisync's last listing holds, which is what both sides agree on.
    Marked excluded, so with no status rather than waiting forever: what
    the default filters leave out (.spola-root, .cache...), looked for in
    the folders just marked. Windows itself shows the rest, what changed
    here since, as waiting.

A folder that cannot be a sync root (not NTFS, inside OneDrive's...) just
shows no status.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback

from . import cloudfiles, listings
from .rclone import MODIFY_WINDOW_NS, Result, default_excluded


class ExplorerStatus:
    def __init__(self, engine, icon: str, version: str):
        self.engine, self.icon, self.version = engine, icon, version
        self._jobs: queue.Queue = queue.Queue()
        self._connected: dict[str, int] = {}        # pair id -> connection key
        self._marked: dict[str, set[str]] = {}       # pair id -> what was marked in sync this session
        self._refused: set[str] = set()             # pair ids whose folder cannot be a sync root
        self._sid = ""
        self._thread = threading.Thread(target=self._work, name="explorer-status", daemon=True)
        self._thread.start()
        engine.connect("pairs-changed", lambda *_: self.update())
        engine.connect("run-finished", self._run_finished)
        engine.connect("path-copied", self._copied)

    # -- main thread ----------------------------------------------------------------

    def update(self) -> None:
        """The sync roots as the pairs and the preference are now."""
        pairs = ([(p.id, p.local, p.name) for p in self.engine.cfg.pairs]
                 if self.engine.cfg.explorer_status else [])
        self._jobs.put(lambda: self._match(pairs))

    def _copied(self, _engine, pair_id: str, rel: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        if pair is not None and rel:
            self._jobs.put(lambda: self._mark_copied(pair_id, pair.local, rel))

    def stop(self) -> None:
        self._jobs.put(None)

    def _run_finished(self, _engine, pair_id: str, _reason: str) -> None:
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime.get(pair_id)
        if pair is None or rt is None or rt.last_outcome is None or rt.last_outcome.result is not Result.OK:
            return
        if pair_id not in self._connected:
            if pair_id not in self._refused:
                self.update()       # not registered yet: then its tree, in sync
            return
        plan = rt.last_outcome.plan
        touched = (plan.upload + plan.download + plan.replace_local + plan.conflicts
                   + [to for _from, to in plan.moved_remote + plan.moved_local])
        workdir = self.engine.paths.workdir(pair_id)
        self._jobs.put(lambda: self._mark(pair_id, pair.local, workdir, touched))

    def _mark(self, pair_id: str, root: str, workdir, touched=()) -> None:
        """In sync: what bisync's last listing holds, which is what both
        sides agree on. Excluded: what the default filters leave out, in
        the root and the folders that hold what was just marked. Each item
        once in a session, but what the run touched again: a file
        downloaded replaces its placeholder."""
        if not listings.find(workdir, 1):
            return
        done = self._marked.setdefault(pair_id, set())
        touched = set(touched)
        done.difference_update(touched)
        entries = listings.load(workdir, 1) or []
        todo = [e for e in entries if e.path not in done]
        for entry in sorted(todo, key=lambda e: (-e.path.count("/"), e.path)):     # a folder after what it holds
            path = os.path.join(root, *entry.path.split("/"))
            if not entry.is_dir and not self._as_listed(path, entry, entry.path in touched):
                continue            # changed here since: waiting, as Windows shows it
            if cloudfiles.mark_in_sync(path):
                done.add(entry.path)
        todo_paths = {e.path for e in todo}
        listed_folders = {e.path for e in entries if e.is_dir}
        folders = {""} | {rel.rpartition("/")[0] for rel in todo_paths} | listed_folders.intersection(todo_paths)
        for folder in sorted(folders):
            self._mark_excluded(root, folder, done)
        cloudfiles.mark_in_sync(root)

    @staticmethod
    def _as_listed(path: str, entry: listings.Entry, touched: bool) -> bool:
        """Is the file still what the listing says both sides agree on?
        Not if its size or date changed since (edited while Spola was closed
        or paused, or during the run), nor if it was written since it was
        marked (Windows took it out of sync), whatever its size and date,
        unless the run just copied it."""
        try:
            st = os.lstat(path)
        except OSError:
            return False
        listed = listings.modtime_ns(entry.modtime)
        if st.st_size != entry.size or listed is None or abs(st.st_mtime_ns - listed) > MODIFY_WINDOW_NS:
            return False
        return touched or cloudfiles.in_sync(path) is not False

    def _mark_copied(self, pair_id: str, root: str, rel: str) -> None:
        if pair_id in self._connected and cloudfiles.mark_in_sync(os.path.join(root, *rel.split("/"))):
            self._marked.setdefault(pair_id, set()).add(rel)

    @staticmethod
    def _mark_excluded(root: str, folder: str, done: set[str]) -> None:
        try:
            items = list(os.scandir(os.path.join(root, *folder.split("/"))))
        except OSError:
            return
        for item in items:
            rel = f"{folder}/{item.name}" if folder else item.name
            if rel in done or item.is_symlink() or item.is_junction():
                continue            # a link: what it points to is not ours (node_modules into OneDrive)
            is_dir = item.is_dir(follow_symlinks=False)
            if default_excluded(rel + "/" if is_dir else rel) and cloudfiles.mark_excluded(item.path, is_dir):
                done.add(rel)

    # -- the worker -------------------------------------------------------------------

    def _work(self) -> None:
        cloudfiles.winrt_thread()
        while True:
            job = self._jobs.get()
            if job is None:
                break
            try:
                job()
            except Exception:  # a folder the shell refuses: no status, no harm
                self._log()
        for key in self._connected.values():
            cloudfiles.disconnect(key)
        self._connected.clear()

    def _log(self) -> None:
        """The exception being handled, at the end of a small log in Spola's
        state folder (kept under 100 kB): what went wrong, for a bug report."""
        try:
            path = self.engine.paths.state / "explorer.log"
            if path.exists() and path.stat().st_size > 100_000:
                path.unlink()
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
        except Exception:
            pass

    def _match(self, pairs: list[tuple[str, str, str]]) -> None:
        if not self._sid:
            self._sid = cloudfiles.user_sid()
        prefix = cloudfiles.root_id(self._sid, "")
        wanted = {cloudfiles.root_id(self._sid, pid): (pid, local, name) for pid, local, name in pairs}
        if not pairs:
            self._refused.clear()           # the switch off: on again, every folder is tried again
        for identity in set(cloudfiles.registered()) | cloudfiles.left_behind(prefix):
            if identity.startswith(prefix) and identity not in wanted:
                pid = identity[len(prefix):]
                if pid in self._connected:
                    cloudfiles.disconnect(self._connected.pop(pid))
                try:
                    cloudfiles.unregister(identity)
                except OSError:
                    self._log()             # the others still go
        known = set(cloudfiles.registered())
        for identity, (pid, local, name) in wanted.items():
            if pid in self._connected or pid in self._refused:
                continue
            try:
                if identity not in known:
                    cloudfiles.register(identity, local, f"Spola · {name}", self.icon, self.version)
                self._connected[pid] = cloudfiles.connect(local)
            except OSError:
                # not NTFS, a network drive, inside another provider's root:
                # no status, and not tried again after every run
                self._refused.add(pid)
                continue
            # connected again (Spola started again, the switch on): what is
            # in sync now, whatever was synced while it was not
            self._marked.pop(pid, None)
            self._mark(pid, local, self.engine.paths.workdir(pid))
