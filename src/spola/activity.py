"""What a run is doing, file by file, for the list under the status in the
window: the operations bisync queued, the ones in progress (from rclone's
stats, with their progress) and the ones done. No GTK here.

bisync announces each operation when it decides it ("Queue copy to Path1",
"Queue delete" on a side), rclone reports the files being copied every second
and logs each one when it is done ("Copied", "Deleted", "Moved" into the
backup folder). A first alignment queues nothing: its copies are known from
the stats and the "Copied" lines alone. A file moved on one side is queued as
a delete plus a copy; when rclone moves it instead (--track-renames: "Moved
... to", then "Renamed from") the two become one move.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

DOWNLOAD, UPLOAD, DELETE_HERE, DELETE_KOOFR, CONFLICT = "download", "upload", "delete-here", "delete-koofr", "conflict"
MOVE_KOOFR, MOVE_HERE = "move-koofr", "move-here"     # moved here and then on Koofr, or the other way
WAITING, ACTIVE, DONE, FAILED = "waiting", "active", "done", "failed"
MAX_OPS = 5000            # a huge run keeps its first ones: the list shows four at a time


@dataclass
class Op:
    path: str                 # relative to the pair's root
    kind: str
    state: str = WAITING
    size: int = 0
    bytes: int = 0
    changed: float = 0.0      # when the state last changed, for the order of the list
    origin: str = ""          # a move: where the file was

    @property
    def fraction(self) -> float | None:
        return min(1.0, self.bytes / self.size) if self.size else None


def kind_of_queue(side: str, action: str) -> str | None:
    """A "Queue ..." line of bisync: side is where the change was seen for
    copies ("Path2 Queue copy to Path1" = a download), where the deletion
    happens for deletes."""
    if action == "Queue copy to Path1":
        return DOWNLOAD
    if action == "Queue copy to Path2":
        return UPLOAD
    if action == "Queue delete":
        return DELETE_HERE if side == "Path1" else DELETE_KOOFR
    return None


class RunActivity:
    def __init__(self, clock=time.monotonic):
        self.ops: dict[str, Op] = {}
        self.clock = clock
        self.into = ""                        # the side the current copies go to (bisync's phases)
        self.moving: dict[str, str] = {}      # new name -> old, until rclone confirms the move

    def _op(self, path: str, kind: str) -> Op | None:
        op = self.ops.get(path)
        if op is None:
            if len(self.ops) >= MAX_OPS:
                return None
            op = self.ops[path] = Op(path, kind, changed=self.clock())
        return op

    def queued(self, path: str, kind: str) -> None:
        if path:
            self._op(path, kind)

    def conflict(self, path: str) -> None:
        if path and (op := self._op(path, CONFLICT)):
            op.kind = CONFLICT

    def transferring(self, entries: list[dict], relative) -> None:
        """rclone's "transferring" stats: the copies in progress."""
        for entry in entries:
            path = relative(str(entry.get("name") or ""))
            if not path:
                continue
            kind = UPLOAD if str(entry.get("srcFs") or "").startswith("local") else DOWNLOAD
            op = self._op(path, kind)
            if op is None or op.state in (DONE, FAILED):
                continue
            if op.state != ACTIVE:
                op.state, op.changed = ACTIVE, self.clock()
            op.size = int(entry.get("size") or 0)
            op.bytes = int(entry.get("bytes") or 0)

    def copied(self, path: str, to_koofr: bool) -> None:
        op = self._op(path, UPLOAD if to_koofr else DOWNLOAD)
        if op is not None and op.kind in (UPLOAD, DOWNLOAD):
            self._finish(op)

    def deleted(self, path: str, here: bool) -> None:
        """A "Deleted" line: only for a deletion. The same line also comes
        when an old local version goes into the backup folder before the new
        one is downloaded, and that download is not done yet."""
        op = self.ops.get(path)
        if op is not None and op.kind == (DELETE_HERE if here else DELETE_KOOFR):
            self._finish(op)

    def phase(self, into: str) -> None:
        self.into = into

    def move_seen(self, old: str, new: str) -> None:
        """A "Moved ... to" line: a rename if rclone confirms it, or bisync's
        own rename of a conflict copy if not."""
        if old and new:
            self.moving[new] = old

    def renamed(self, new: str) -> None:
        """--track-renames moved `new` from where it was: the delete and the
        copy queued for it are one move, done."""
        old = self.moving.pop(new, None)
        if old is None or not self.into:
            return
        kind = MOVE_KOOFR if self.into == "Path2" else MOVE_HERE
        self.ops.pop(old, None)
        op = self._op(new, kind)
        if op is not None:          # (the copy queued for it: now a move, with nothing to transfer)
            op.kind, op.origin, op.size, op.bytes = kind, old, 0, 0
            self._finish(op)

    def backed_up(self, path: str) -> None:
        op = self.ops.get(path)
        if op is not None and op.kind == DELETE_HERE:
            self._finish(op)

    def failed(self, path: str) -> None:
        op = self.ops.get(path)
        if op is not None and op.state != DONE:
            op.state, op.changed = FAILED, self.clock()

    def run_ended(self, ok: bool) -> None:
        """A run that succeeded did everything it queued: folders, for one,
        are created without a "Copied" line."""
        if ok:
            for op in self.ops.values():
                if op.state in (WAITING, ACTIVE):
                    self._finish(op)

    def _finish(self, op: Op) -> None:
        if op.state != DONE:
            op.state, op.changed = DONE, self.clock()
            if op.size:
                op.bytes = op.size

    def rows(self, limit: int = 4) -> list[Op]:
        """What the window shows: the copies in progress, then the latest done
        or failed, then the next waiting, `limit` in all."""
        ops = list(self.ops.values())
        active = sorted((o for o in ops if o.state == ACTIVE), key=lambda o: -o.changed)
        done = sorted((o for o in ops if o.state in (DONE, FAILED)), key=lambda o: -o.changed)
        waiting = [o for o in ops if o.state == WAITING]
        return (active + done + waiting)[:limit]

    def __len__(self) -> int:
        return len(self.ops)
