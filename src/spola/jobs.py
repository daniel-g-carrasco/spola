"""Turn "run this pair in this mode" into a checked BisyncRequest.

This is the single place where the GUI and the CLI decide what rclone will
be asked to do, so every rule about when a run may start lives here:
  * a pair that was never aligned only accepts a RESYNC (first alignment);
  * the local root must still be the folder that was paired;
  * --max-delete is always derived from the pair's limit and the size of the
    last agreed listing.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass

from . import listings
from .config import Config, Pair
from .paths import Paths
from .preflight import Problem, Refused, check_root_identity, refused_paths
from .rclone import DEFAULT_FILTERS, BisyncRequest, Mode, effective_max_delete, exclude_lines, remote_spec


class NotReady(Exception):
    def __init__(self, problems: list[Problem]):
        super().__init__("; ".join(p.message for p in problems))
        self.problems = problems


@dataclass
class Job:
    pair: Pair
    request: BisyncRequest
    path1: str
    path2: str
    refused: list[Refused]


def ensure_filters(paths: Paths, pair: Pair) -> None:
    path = paths.filters_file(pair.id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_FILTERS, encoding="utf-8")


def prepare(cfg: Config, pair: Pair, paths: Paths, mode: Mode = Mode.SYNC,
            dry_run: bool = False, force: bool = False,
            path2_override: str | None = None) -> Job:
    """`path2_override` replaces the Koofr side with another path (tests run
    bisync between two local folders); it is never set by the app."""
    problems = check_root_identity(pair)
    if problems:
        raise NotReady(problems)
    if mode is Mode.SYNC and not pair.initialized:
        raise NotReady([Problem("not-initialized", "the first alignment has not been confirmed yet", pair.local)])

    workdir = paths.workdir(pair.id)
    workdir.mkdir(parents=True, exist_ok=True)
    ensure_filters(paths, pair)
    # One backup folder per run: a file replaced twice keeps both old copies.
    # rclone creates it only if something is actually backed up.
    backup = paths.backup_dir(pair.id) / time.strftime("%Y%m%d-%H%M%S")
    paths.rclone_config.parent.mkdir(parents=True, exist_ok=True)

    previous = listings.load(workdir, 1) if listings.find(workdir, 1) else None
    known = frozenset(e.path for e in previous or [])
    refused = refused_paths(pair.local, pair.remote, known,
                            skip=lambda rel: rel.startswith(("SyncTrash/", ".Trash-")))
    exclude_file = None
    if refused:
        exclude_file = paths.state / "exclude" / f"{pair.id}.txt"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text(exclude_lines(refused), encoding="utf-8")

    path2 = path2_override or remote_spec(pair.remote)
    request = BisyncRequest(
        path1=pair.local,
        path2=path2,
        workdir=workdir,
        rclone_config=paths.rclone_config,
        filters_file=paths.filters_file(pair.id),
        backup_dir1=backup,
        max_delete=effective_max_delete(pair.max_delete_percent, listings.count(workdir)),
        mode=mode,
        dry_run=dry_run,
        force=force,
        exclude_from=exclude_file,
    )
    return Job(pair, request, pair.local.rstrip("/") + "/", path2, refused)


def prune_backups(paths: Paths, pair_id: str, days: int, now: float | None = None) -> int:
    """Delete this pair's run backups older than `days`; return how many."""
    root = paths.backup_dir(pair_id)
    now = time.time() if now is None else now
    removed = 0
    if not root.is_dir():
        return 0
    for run in root.iterdir():
        try:
            stamp = time.mktime(time.strptime(run.name, "%Y%m%d-%H%M%S"))
        except ValueError:
            continue    # not ours: never touch it
        if now - stamp > days * 86400 and run.is_dir() and not run.is_symlink():
            shutil.rmtree(run)
            removed += 1
    return removed
