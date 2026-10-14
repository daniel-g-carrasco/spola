"""Restoring from Koofr's trash what was deleted on Koofr in a synced folder.

Learned on 2026-10-14 (docs/progetto.md, appendix W), when /data was deleted
on Koofr by mistake:

  * the trash holds files only, one entry each (folders are not in it), and
    every entry of one deletion has the same time: a deletion is a group of
    entries with the same "deleted" (a few seconds apart at most);
  * only the chosen deletion is restored, never "everything": an empty list
    asks Koofr to restore the whole trash, intended deletions included;
  * Koofr restores 500 files in about 85 s per job, and runs four jobs side
    by side; a job may report a file restored that is still in the trash, so
    the trash is read again at the end and what is left is asked again;
  * a 404 on undelete means "not in the trash": restored already;
  * folders left empty do not come back (bisync would then remove them here);
  * every restored file gets today's modification time on Koofr, which Koofr
    only sets on upload: see Engine.restore_from_trash for what Spola does.

No GTK here; the Koofr calls go through an object with three methods (the
real one is KoofrTrash, tests use their own).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import koofr_api

BATCH = 500
WORKERS = 4
SAME_DELETION_MS = 5_000        # entries this close belong to one deletion
MAX_PAGES = 400                 # 400,000 entries: Koofr keeps the trash 30 days


@dataclass(frozen=True)
class Entry:
    mount_id: str
    path: str           # absolute on Koofr, "/data/work/a.txt"
    size: int
    deleted: int        # ms since the epoch


@dataclass
class Deletion:
    """Files deleted together, under one synced folder."""
    deleted: int                    # the newest entry's time, ms
    entries: list[Entry] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(e.size for e in self.entries)

    def folders(self, root: str, limit: int = 3) -> list[str]:
        """The top folders it touched, relative to the synced folder's root."""
        seen: list[str] = []
        for e in self.entries:
            rel = e.path[len(root.rstrip("/")) + 1:]
            top = rel.split("/", 1)[0] if "/" in rel else rel
            if top not in seen:
                seen.append(top)
            if len(seen) >= limit:
                break
        return seen


@dataclass
class Result:
    restored: int = 0
    left: list[Entry] = field(default_factory=list)     # still in the trash after a second try
    cancelled: bool = False


class KoofrTrash:
    """The real trash, through Koofr's REST API."""

    def __init__(self, user: str, password: str):
        self.user, self.password = user, password

    def page(self, cursor: str | None) -> tuple[list[Entry], str | None]:
        return koofr_api.trash_page(self.user, self.password, cursor)

    def undelete(self, entries: list[Entry]) -> str | None:
        return koofr_api.trash_undelete(self.user, self.password, [(e.mount_id, e.path) for e in entries])

    def job_done(self, job_id: str) -> bool | None:
        """True when finished, False when it failed, None while it runs."""
        return koofr_api.job_done(self.user, self.password, job_id)


def under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return root == "" or path.casefold() == root.casefold() or path.casefold().startswith(root.casefold() + "/")


def read(api, mount_id: str, root: str, since: int = 0,
         cancelled: Callable[[], bool] = lambda: False) -> list[Entry]:
    """The trash entries under `root` (newest first), deleted at `since` or later."""
    found, cursor = [], None
    for _page in range(MAX_PAGES):
        entries, cursor_next = api.page(cursor)
        found += [e for e in entries if e.mount_id == mount_id and under(e.path, root) and e.deleted >= since]
        if (not entries or not cursor_next or cursor_next == cursor or cancelled()
                or min(e.deleted for e in entries) < since):
            break
        cursor = cursor_next
    return found


def deletions(entries: list[Entry]) -> list[Deletion]:
    """Entries grouped into deletions, the newest first."""
    groups: list[Deletion] = []
    for e in sorted(entries, key=lambda e: -e.deleted):
        if groups and groups[-1].entries[-1].deleted - e.deleted <= SAME_DELETION_MS:
            groups[-1].entries.append(e)
        else:
            groups.append(Deletion(e.deleted, [e]))
    for group in groups:
        group.entries.sort(key=lambda e: e.path)
    return groups


def restore(api, entries: list[Entry], progress: Callable[[int, int], None] = lambda done, total: None,
            cancelled: Callable[[], bool] = lambda: False, poll_s: float = 3.0) -> Result:
    """Undelete `entries` in batches, a few at a time, then look in the trash
    for what is still there and ask once more."""
    if not entries:
        return Result()             # never an empty list: that restores the whole trash
    total = len(entries)
    result = Result()
    lock = threading.Lock()

    def one(batch: list[Entry]) -> None:
        if cancelled():
            return
        try:
            job = api.undelete(batch)
        except koofr_api.NotInTrash:
            job = None              # restored already (an earlier try)
        while job is not None and api.job_done(job) is None:
            time.sleep(poll_s)
        with lock:
            result.restored += len(batch)
            progress(min(result.restored, total), total)

    def run(todo: list[Entry]) -> None:
        batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(one, batches))

    run(entries)
    if cancelled():
        result.cancelled = True
        return result
    # what a job said it restored and did not
    mount_ids = {e.mount_id for e in entries}
    wanted = {(e.mount_id, e.path) for e in entries}
    oldest = min(e.deleted for e in entries)
    left = [e for m in mount_ids for e in read(api, m, "/", oldest) if (e.mount_id, e.path) in wanted]
    if left:
        run(left)
        left = [e for m in mount_ids for e in read(api, m, "/", oldest) if (e.mount_id, e.path) in wanted]
    result.restored = total - len(left)
    result.left = left
    return result
