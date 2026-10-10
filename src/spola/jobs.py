"""Turn "run this pair in this mode" into a checked BisyncRequest.

This is the single place where the GUI and the CLI decide what rclone will
be asked to do, so every rule about when a run may start lives here:
  * a pair that was never aligned only accepts a RESYNC (first alignment);
  * the local root must still be the folder that was paired;
  * --max-delete is always derived from the pair's limit and the size of the
    last agreed listing.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

from . import listings
from .config import Config, Pair
from .paths import Paths
from .preflight import Problem, Refused, check_root_identity, refused_paths
from .i18n import _, translation_for
from .koofr_api import user_agent
from .rclone import (DEFAULT_FILTERS, BisyncRequest, Mode, conflict_suffix, default_excluded,
                     effective_max_delete, exclude_lines, remote_spec)


def conflict_words(language: str = "") -> tuple[str, str]:
    """The words in the names of conflict copies, in the desktop language or
    in the one chosen in the preferences."""
    if language:
        tr = translation_for(language).gettext
        return tr("conflict-local"), tr("conflict-koofr")
    # TRANSLATORS: goes into file names, e.g. report.conflict-local-2026-10-10-1.odt;
    # use letters and "-" only
    return _("conflict-local"), _("conflict-koofr")


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


def read_filters(paths: Paths, pair: Pair) -> str:
    path = paths.filters_file(pair.id)
    return path.read_text(encoding="utf-8") if path.exists() else DEFAULT_FILTERS


def write_filters(paths: Paths, pair: Pair, text: str) -> bool:
    """Store the pair's exclusions; True if they changed (bisync then asks
    for a new first alignment, which the app shows as a repair preview)."""
    if not text.endswith("\n"):
        text += "\n"
    if text == read_filters(paths, pair):
        return False
    path = paths.filters_file(pair.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


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
    refused = refused_paths(pair.local, pair.remote, known, skip=default_excluded)
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
        conflict_suffix=conflict_suffix(*conflict_words(cfg.conflict_language)),
        bwlimit=os.environ.get("SPOLA_DEV_BWLIMIT") or None,   # developer aid: slow runs down
        user_agent=user_agent(cfg.instance_id) if cfg.instance_id else None,
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
