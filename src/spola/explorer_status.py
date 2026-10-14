"""The synced folders' status in File Explorer, kept up to date (Windows).

One worker thread does all of it, in the Windows Runtime's multithreaded
apartment, one job after the other: the shell's calls block, and a whole
tree marked in sync takes a while. The jobs:

  * the sync roots made to match the pairs: one registered and connected
    per pair (its whole tree marked in sync the first time), Spola's ones
    without a pair unregistered; all of them when the preference is off;
  * after a run that went well, what it touched (and the folders above)
    marked in sync: Windows itself shows the rest, what changed here since,
    as waiting.

A folder that cannot be a sync root (not NTFS, inside OneDrive's...) just
shows no status.
"""

from __future__ import annotations

import queue
import threading

from gi.repository import GLib

from . import cloudfiles
from .rclone import Result, default_excluded


class ExplorerStatus:
    def __init__(self, engine, icon: str, version: str):
        self.engine, self.icon, self.version = engine, icon, version
        self._jobs: queue.Queue = queue.Queue()
        self._connected: dict[str, int] = {}        # pair id -> connection key
        self._sid = ""
        self._thread = threading.Thread(target=self._work, name="explorer-status", daemon=True)
        self._thread.start()
        engine.connect("pairs-changed", lambda *_: self.update())
        engine.connect("run-finished", self._run_finished)

    # -- main thread ----------------------------------------------------------------

    def update(self) -> None:
        """The sync roots as the pairs and the preference are now."""
        # aligned pairs only: before the first alignment nothing is in sync
        pairs = ([(p.id, p.local, p.name) for p in self.engine.cfg.pairs if p.initialized]
                 if self.engine.cfg.explorer_status else [])
        self._jobs.put(lambda: self._match(pairs))

    def stop(self) -> None:
        self._jobs.put(None)

    def _run_finished(self, _engine, pair_id: str, _reason: str) -> None:
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime.get(pair_id)
        if pair is None or rt is None or rt.last_outcome is None or rt.last_outcome.result is not Result.OK:
            return
        if pair_id not in self._connected:
            self.update()           # its first alignment, just done: its tree in sync
            return
        plan = rt.last_outcome.plan
        touched = (plan.upload + plan.download + plan.replace_local + plan.conflicts
                   + [to for _from, to in plan.moved_remote + plan.moved_local])
        if touched:
            paths = cloudfiles.paths_and_parents(pair.local, touched)
            self._jobs.put(lambda: [cloudfiles.mark_in_sync(p) for p in paths])

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
                pass
        for key in self._connected.values():
            cloudfiles.disconnect(key)
        self._connected.clear()

    def _match(self, pairs: list[tuple[str, str, str]]) -> None:
        if not self._sid:
            self._sid = cloudfiles.user_sid()
        prefix = cloudfiles.root_id(self._sid, "")
        wanted = {cloudfiles.root_id(self._sid, pid): (pid, local, name) for pid, local, name in pairs}
        for identity in cloudfiles.registered():
            if identity.startswith(prefix) and identity not in wanted:
                pid = identity[len(prefix):]
                if pid in self._connected:
                    cloudfiles.disconnect(self._connected.pop(pid))
                cloudfiles.unregister(identity)
        known = set(cloudfiles.registered())
        for identity, (pid, local, name) in wanted.items():
            if pid in self._connected:
                continue
            first = identity not in known
            try:
                if first:
                    cloudfiles.register(identity, local, f"Spola · {name}", self.icon, self.version)
                self._connected[pid] = cloudfiles.connect(local)
            except OSError:
                continue
            if first:
                refused = self._refused(pid)

                def skip(rel: str, refused: set[str] = refused) -> bool:
                    return default_excluded(rel) or rel.rstrip("/") in refused

                for path in cloudfiles.walk(local, skip):
                    cloudfiles.mark_in_sync(path)

    def _refused(self, pair_id: str) -> set[str]:
        """What the last run left out (names Koofr refuses): not in sync."""
        box: list[set[str]] = []
        done = threading.Event()

        def read() -> bool:
            rt = self.engine.runtime.get(pair_id)
            box.append({r.path.replace("\\", "/") for r in rt.refused} if rt else set())
            done.set()
            return False

        GLib.idle_add(read)
        done.wait(5)
        return box[0] if box else set()
