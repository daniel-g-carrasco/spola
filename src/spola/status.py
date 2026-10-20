"""What Spola is doing, as a small JSON file for other programs to show:
the Noctalia plugin (noctalia/ in the sources) puts it in the bar, a
panel and the launcher. Written atomically in the state folder
(status.json) at every change of the engine's, a second apart at most,
and again every minute as a heartbeat, and removed at quit; a reader
takes a file older than a few minutes for a Spola not running (the pid in
it is the sandbox's own, no use outside the Flatpak). Data only, no words: the
statuses are the engine's names, the reader translates them; the
messages are the engine's, already in the user's language."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from gi.repository import GLib

from . import __version__
from .engine import Engine, Status
from .log import complain

FORMAT = 1
DEBOUNCE_MS = 1000
HEARTBEAT_S = 60        # written again even when nothing changes: a reader tells a crash from a quiet day
# the statuses that wait for the user (a paused folder waits for nothing)
NEEDS_YOU = (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED)


def _progress(rt) -> dict | None:
    stats = rt.stats or {}
    if not stats:
        return None
    total_bytes = stats.get("totalBytes") or 0
    done_bytes = stats.get("bytes") or 0
    total = stats.get("totalTransfers") or 0
    done = stats.get("transfers") or 0
    fraction = None
    if total_bytes:
        fraction = min(1.0, done_bytes / total_bytes)
    elif total:
        fraction = min(1.0, done / total)
    return {"fraction": fraction, "bytes": done_bytes, "total_bytes": total_bytes, "transfers": done,
            "total_transfers": total, "eta_s": stats.get("eta"), "speed": stats.get("speed") or 0,
            "deletes_queued": getattr(rt.activity, "deletes_queued", 0) if rt.activity else 0}


def _pair(engine: Engine, pair, rt) -> dict:
    syncing = rt.status is Status.SYNCING and not (rt.task is not None and rt.task.dry_run)
    stopping = rt.run is not None and rt.run.stopping
    return {
        "id": pair.id, "name": pair.name, "local": pair.local, "remote": pair.remote,
        "status": rt.status.name.lower(), "message": rt.message or "",
        "paused": pair.paused, "initialized": pair.initialized,
        "syncing": syncing, "stopping": stopping,
        "progress": _progress(rt) if syncing else None,
        "needs_you": rt.status in NEEDS_YOU and not pair.paused,
        "last_sync": rt.last_sync, "conflicts": len(rt.conflicts), "refused": len(rt.refused),
        "gone": rt.gone, "safety": rt.safety,
    }


def snapshot(engine: Engine) -> dict:
    """The engine's state as plain data. Never raises: a folder whose
    state cannot be read is left out, said on stderr."""
    pairs = []
    for pair in engine.cfg.pairs:
        rt = engine.runtime.get(pair.id)
        if rt is None:
            continue
        try:
            pairs.append(_pair(engine, pair, rt))
        except Exception as e:      # one folder's trouble must not blank the file
            complain(f"the status of {pair.name}", e)
    signed_in = engine.creds is not None
    overall = engine.overall() if signed_in else Status.PAUSED
    blocked = "" if signed_in else "not-signed-in"
    blocked = blocked or (engine.blocked_reason() if overall is Status.PAUSED else "") or ""
    task = engine.active
    syncing = task.pair_id if task is not None and not task.dry_run and engine.cfg.pair(task.pair_id) else None
    space = engine.space
    return {
        "format": FORMAT, "spola": __version__, "pid": os.getpid(), "updated": time.time(),
        "signed_in": signed_in, "paused_all": engine.cfg.paused_all, "blocked": blocked,
        "overall": overall.name.lower(),
        "syncing": syncing,
        "needs_you": [p["id"] for p in pairs if p["needs_you"]],
        "space": {"total": space.total, "used": space.used, "free": space.free} if space is not None else None,
        "pairs": pairs,
    }


def write(path: Path, payload: dict) -> None:
    """Whole or not at all: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".status-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


class StatusFile:
    """Keeps the file current from the engine's signals, one write at most
    every DEBOUNCE_MS; `stop()` removes it (Spola is quitting)."""

    def __init__(self, engine: Engine, path: Path | None = None):
        self.engine = engine
        self.path = path or engine.paths.status_file
        self._source = 0
        self._handlers = [engine.connect(signal, self._changed)
                          for signal in ("status-changed", "pair-changed", "pairs-changed", "run-finished")]
        self._heartbeat = GLib.timeout_add_seconds(HEARTBEAT_S, lambda: self._changed() or True)
        self._write()

    def _changed(self, *_args) -> None:
        if not self._source:
            self._source = GLib.timeout_add(DEBOUNCE_MS, self._write)

    def _write(self) -> bool:
        self._source = 0
        try:
            write(self.path, snapshot(self.engine))
        except OSError as e:
            complain("writing the status file", e)
        return False

    def stop(self) -> None:
        for handler in self._handlers:
            self.engine.disconnect(handler)
        self._handlers = []
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
        GLib.source_remove(self._heartbeat)
        remove(self.path)
