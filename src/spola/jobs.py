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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import listings, versions
from .config import Config, Pair
from .paths import Paths
from .preflight import (CaseRename, Problem, Refused, case_renames as find_case_renames, check_koofr_desktop,
                        check_root_identity, koofr_desktop_config, refused_paths)
from .i18n import _, translation_for
from .koofr_api import user_agent
from .rclone import (DEFAULT_FILTERS, BisyncRequest, Mode, allowed_changes, conflict_suffix, default_excluded,
                     effective_max_delete, exclude_lines, filter_escape, no_window, remote_spec,
                     reserved_exclude_lines,
                     windows_reserved_names_matter)


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


# A file written less than this long ago may still be being written (a
# transcription, a download, a log): copied now, the other side gets half of
# it, and the next run takes it for changed on both sides.
SETTLE_S = 30


class TooManyChanges(Exception):
    """More files changed here since the last run than may go up over
    Koofr's copies unasked: the run waits for a confirmation, with a preview."""

    def __init__(self, changed: int, total: int, examples: list[str]):
        super().__init__(f"{changed} of {total}")
        self.changed, self.total, self.examples = changed, total, examples


def changed_here(root: str, previous: list[listings.Entry]) -> list[str]:
    """The files of the last listing whose size or date differ here now:
    what the next run uploads over Koofr's copy of them."""
    changed = []
    for entry in previous:
        if entry.is_dir:
            continue
        try:
            st = os.stat(os.path.join(root, entry.path))
        except OSError:
            continue            # gone: a deletion, bisync's own check
        known = listings.modtime_ns(entry.modtime)
        if st.st_size != entry.size or (known is not None and abs(st.st_mtime_ns - known) > 1_000_000):
            changed.append(entry.path)
    return changed


class DamagedListings(Exception):
    """A listing of the last synced state cannot be read: a repair (the kept
    copy put back, or a new alignment), not an error to retry."""


def _load(workdir: Path, side: int):
    """listings.load, a damaged file said as such."""
    try:
        return listings.load(workdir, side)
    except ValueError as e:
        raise DamagedListings(str(e)) from e


class Busy(Exception):
    """Files already in sync are being written right now: the run waits for
    them. They are never left out of a run: bisync would then forget them,
    and at the next run take them for new on both sides, a conflict."""

    def __init__(self, paths: list[str]):
        super().__init__(", ".join(paths[:5]))
        self.paths = paths


@dataclass
class Job:
    pair: Pair
    request: BisyncRequest
    path1: str
    path2: str
    refused: list[Refused]
    busy: list[str] = field(default_factory=list)     # new files left out: still being written


def being_written(root: str, previous: list[listings.Entry], now: float,
                  settle_s: float | None = None) -> tuple[list[str], list[str]]:
    """(new, known): the files written less than settle_s (SETTLE_S) ago,
    those not in the last listing and those in it. Only the files new or
    changed since the listing are looked at closely: their own last write
    (a folder's listing can show an older one for a file open for writing)."""
    settle_s = SETTLE_S if settle_s is None else settle_s
    listed = {e.path: e for e in previous if not e.is_dir}
    new, known = [], []
    stack = [(root, "")]
    while stack:
        folder, prefix = stack.pop()
        try:
            items = list(os.scandir(folder))
        except OSError:
            continue
        for item in items:
            rel = prefix + item.name
            try:
                if item.is_symlink():
                    continue
                if item.is_dir():
                    if not default_excluded(rel + "/"):
                        stack.append((item.path, rel + "/"))
                    continue
                if default_excluded(rel):
                    continue
                st = item.stat()
            except OSError:
                continue
            entry = listed.get(rel)
            if entry is not None and st.st_size == entry.size:
                listed_ns = listings.modtime_ns(entry.modtime)
                if listed_ns is not None and abs(st.st_mtime_ns - listed_ns) <= 1_000_000:
                    continue            # as it was synced: not being written
            try:
                written = os.stat(item.path).st_mtime       # its own, up to date
            except OSError:
                continue
            if 0 <= now - written < settle_s:
                (known if entry is not None else new).append(rel)
    return sorted(new), sorted(known)


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


def local_files(root: str) -> set[str]:
    """Every file below root a run would sync (the default exclusions left
    out), as relative paths with "/"."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not default_excluded(f"{rel_dir}{d}/")]
        found.update(f"{rel_dir}{f}" for f in filenames if not default_excluded(f"{rel_dir}{f}"))
    return found


def files_on_koofr(rclone: str, env: dict, paths: Paths, pair: Pair,
                   path2_override: str | None = None) -> set[str] | None:
    """Every file of the pair's Koofr folder (its filters applied), or None
    when Koofr could not be read."""
    root = path2_override or remote_spec(pair.remote)
    try:
        done = subprocess.run([rclone, "lsf", "-R", "--files-only", "--filter-from", str(paths.filters_file(pair.id)),
                               root, "--config", str(paths.rclone_config), "--color", "NEVER"],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=600, **no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(done.stdout.splitlines()) if done.returncode == 0 else None


def koofr_folder_exists(rclone: str, env: dict, paths: Paths, pair: Pair,
                        path2_override: str | None = None) -> bool | None:
    """Is the pair's folder still on Koofr? None when Koofr could not be asked."""
    root = path2_override or remote_spec(pair.remote)
    try:
        done = subprocess.run([rclone, "lsjson", "--stat", root, "--config", str(paths.rclone_config),
                               "--color", "NEVER"], env=env, capture_output=True, text=True, encoding="utf-8",
                              timeout=120, **no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode == 0:
        return True
    return False if "not found" in done.stderr else None


def make_koofr_folder(rclone: str, env: dict, paths: Paths, pair: Pair, path2_override: str | None = None) -> str:
    """The pair's folder made again on Koofr, empty: '' or what went wrong."""
    root = path2_override or remote_spec(pair.remote)
    try:
        done = subprocess.run([rclone, "mkdir", root, "--config", str(paths.rclone_config), "--color", "NEVER"],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=120, **no_window())
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    return "" if done.returncode == 0 else (lines[-1] if lines else str(done.returncode))


def _folded(files: set[str]) -> set[str]:
    # Windows and Koofr take names that differ in case alone for the same file
    return {f.casefold() for f in files}


def prepare(cfg: Config, pair: Pair, paths: Paths, mode: Mode = Mode.SYNC,
            dry_run: bool = False, force: bool = False,
            path2_override: str | None = None, koofr_files=None, wait_for_writes: bool = False,
            now: float | None = None, max_delete: int | None = None, max_changes: int | None = None) -> Job:
    """`path2_override` replaces the Koofr side with another path (tests run
    bisync between two local folders); it is never set by the app.
    `koofr_files()` lists the Koofr folder's files, or None if it cannot:
    asked only after a first alignment of two empty folders.
    `wait_for_writes` (the automatic runs; never one the user asked for):
    new files still being written are left out of the run (Job.busy), and
    for ones already in sync the run raises Busy."""
    workdir = paths.workdir(pair.id)
    known = (_load(workdir, 1) or []) if listings.find(workdir, 1) else []
    problems = check_root_identity(pair, [e.path for e in known[:50]])
    # before every run, not only when the pair was added: Koofr Desktop may
    # have come back (an autostart entry it rewrote, a session that runs it)
    problems += check_koofr_desktop(pair.local, koofr_desktop_config())
    if problems:
        raise NotReady(problems)
    if mode is Mode.SYNC and not pair.initialized:
        raise NotReady([Problem("not-initialized", "the first alignment has not been confirmed yet", pair.local)])

    workdir.mkdir(parents=True, exist_ok=True)
    ensure_filters(paths, pair)
    if mode is Mode.SYNC and listings.find(workdir, 1) and listings.count(workdir) == 0:
        # bisync refuses a run from an empty listing ("Empty prior Path1
        # listing. Cannot sync to an empty directory"): two folders empty
        # but for what Spola leaves out stopped for a repair right after
        # their first alignment. With nothing known in sync there is nothing
        # to delete either: the run is a resync, which copies what each side
        # has to the other (the engine still takes it for a sync). Only if
        # no file is on both sides: a resync settles one by date, the older
        # copy lost and no preview asked; that stays a repair, with its preview.
        on_koofr = koofr_files() if koofr_files else None
        if on_koofr is not None and not (_folded(local_files(pair.local)) & _folded(on_koofr)):
            mode = Mode.RESYNC
    # One backup folder per run: a file replaced twice keeps both old copies.
    # rclone creates it only if something is actually backed up.
    backup = paths.backup_dir(pair.id) / time.strftime("%Y%m%d-%H%M%S")
    paths.rclone_config.parent.mkdir(parents=True, exist_ok=True)

    previous = _load(workdir, 1) if listings.find(workdir, 1) else None
    known = frozenset(e.path for e in previous or [])
    refused = refused_paths(pair.local, pair.remote, known, skip=default_excluded)
    busy: list[str] = []
    if mode is Mode.SYNC and wait_for_writes:
        busy, known_busy = being_written(pair.local, previous or [], time.time() if now is None else now)
        if known_busy:
            raise Busy(known_busy)
    if mode is Mode.SYNC and not dry_run and previous:
        # too many files changed here at once: not over Koofr's copies unasked
        changed = changed_here(pair.local, previous)
        allowed = max_changes if max_changes is not None else allowed_changes(pair.max_delete_percent, len(previous))
        if len(changed) > allowed:
            raise TooManyChanges(len(changed), sum(1 for e in previous if not e.is_dir), changed[:5])
    exclude_file = None
    lines = exclude_lines(refused) + "".join(filter_escape(path) + "\n" for path in busy)
    if windows_reserved_names_matter():
        lines += reserved_exclude_lines()       # Windows 10's device names, on both sides
    if lines:
        exclude_file = paths.exclude_file(pair.id)
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        # whole or not at all: a torn file would be a wrong set of rules
        tmp = exclude_file.with_name(exclude_file.name + ".new")
        tmp.write_text(lines, encoding="utf-8", newline="\n")
        tmp.replace(exclude_file)

    path2 = path2_override or remote_spec(pair.remote)
    request = BisyncRequest(
        path1=pair.local,
        path2=path2,
        workdir=workdir,
        rclone_config=paths.rclone_config,
        filters_file=paths.filters_file(pair.id),
        backup_dir1=backup,
        max_delete=max_delete if max_delete is not None
        else effective_max_delete(pair.max_delete_percent, listings.count(workdir)),
        mode=mode,
        dry_run=dry_run,
        force=force,
        exclude_from=exclude_file,
        conflict_suffix=conflict_suffix(*conflict_words(cfg.conflict_language)),
        bwlimit=os.environ.get("SPOLA_DEV_BWLIMIT") or cfg.bwlimit,   # the env is a developer aid
        user_agent=user_agent(cfg.instance_id) if cfg.instance_id else None,
    )
    return Job(pair, request, pair.local.rstrip("/") + "/", path2, refused, busy)


def listed_files(paths: Paths, pair: Pair) -> tuple[dict[str, int], dict[str, int]] | None:
    """The files of the last run's two listings, {path: size}, here and on
    Koofr (the state a preview starts from); None before a first alignment
    or when a listing cannot be read. A second or so on 40,000 files: off
    the main loop."""
    workdir = paths.workdir(pair.id)
    try:
        sides = [listings.load(workdir, side) for side in (1, 2)]
    except (OSError, ValueError):
        return None
    if any(side is None for side in sides):
        return None
    here, there = ({e.path: e.size for e in side if not e.is_dir} for side in sides)
    return here, there


def case_renames(paths: Paths, pair: Pair) -> list[CaseRename]:
    """The renames by case alone made here since the last run (see
    preflight.case_renames), from the two listings of that run."""
    workdir = paths.workdir(pair.id)
    try:
        before, koofr = listings.load(workdir, 1), listings.load(workdir, 2)
    except (OSError, ValueError):
        return []
    if not before or not koofr:
        return []
    return find_case_renames(pair.local, (e.path for e in before), (e.path for e in koofr))


def _join(root: str, rel: str) -> str:
    return root + rel if root.endswith((":", "/")) else f"{root}/{rel}"


def rename_on_koofr(rclone: str, env: dict, cfg: Config, pair: Pair, paths: Paths, renames: list[CaseRename],
                    path2_override: str | None = None) -> str:
    """The renames by case alone, done on Koofr before the run, on the
    server (a folder at once). Koofr is asked for the name it has first: a
    run stopped by a safety check after renaming leaves it done for the next.
    '' when done, else what went wrong."""
    root = path2_override or remote_spec(pair.remote)
    common = ["--config", str(paths.rclone_config), "--color", "NEVER"]
    if cfg.instance_id:
        common += ["--user-agent", user_agent(cfg.instance_id)]     # Spola's own change, for the feed

    def rclone_run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([rclone, *args, *common], env=env, capture_output=True, text=True, timeout=600,
                              **no_window())

    for r in renames:
        parent, _sep, name = r.koofr.rpartition("/")
        target_name = r.new.rpartition("/")[2]
        try:
            listed = rclone_run("lsf", "--max-depth", "1", _join(root, parent) if parent else root)
            if listed.returncode != 0:
                continue        # the folder is gone: bisync will see what happened
            names = [line.rstrip("/") for line in listed.stdout.splitlines()]
            current = next((n for n in names if n.casefold() == name.casefold()), None)
            if current is None or current == target_name:
                continue        # gone, or renamed already
            source = f"{parent}/{current}" if parent else current
            target = f"{parent}/{target_name}" if parent else target_name
            done = rclone_run("moveto", _join(root, source), _join(root, target))
        except (OSError, subprocess.TimeoutExpired) as e:
            return str(e)
        if done.returncode != 0:
            lines = [line for line in done.stderr.splitlines() if line.strip()]
            return _("Could not rename “{old}” to “{new}” on Koofr: {error}").format(
                old=source, new=target, error=lines[-1] if lines else done.returncode)
    return ""


def exclusion_patterns(filters_text: str) -> list[str]:
    """The "- pattern" rules of a filters file, for excluded_by: what the
    folder monitor need not watch."""
    found = []
    for line in filters_text.splitlines():
        line = line.strip()
        if line.startswith("- ") and len(line) > 2:
            found.append(line[2:].strip())
    return found


def backups_older_than(paths: Paths, pair_id: str, days: int, now: float | None = None) -> tuple[int, int]:
    """(runs, bytes) of this pair's backups that `days` of retention would
    delete: what to say before a shorter retention is applied."""
    root = paths.backup_dir(pair_id)
    now = time.time() if now is None else now
    runs = size = 0
    if not root.is_dir():
        return 0, 0
    for run in root.iterdir():
        try:
            stamp = time.mktime(time.strptime(run.name, "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if now - stamp > days * 86400 and run.is_dir() and not run.is_symlink():
            runs += 1
            for folder, _dirs, files in os.walk(run):
                for name in files:
                    try:
                        size += os.lstat(os.path.join(folder, name)).st_size
                    except OSError:
                        pass
    return runs, size


def prune_backups(paths: Paths, pair_id: str, days: int, now: float | None = None) -> int:
    """Delete this pair's run backups older than `days`, each with its
    manifest (versions.py: no run left in the index without its copies);
    return how many. The only place where Spola deletes a kept copy."""
    root = paths.backup_dir(pair_id)
    now = time.time() if now is None else now
    removed = 0
    if not root.is_dir():
        return 0
    for run in root.iterdir():
        stamp = versions.run_time(run.name)
        if stamp is None:
            continue    # not ours: never touch it (a manifest is handled with its run)
        if now - stamp > days * 86400 and run.is_dir() and not run.is_symlink():
            shutil.rmtree(run)
            removed += 1
    # manifests whose run is gone (the retention above, or a run removed by
    # hand), and temporary files of a manifest write cut short
    for item in root.iterdir():
        name = item.name
        orphan = (name.endswith(versions.MANIFEST) and versions.run_time(name[:-len(versions.MANIFEST)]) is not None
                  and not (root / name[:-len(versions.MANIFEST)]).is_dir())
        if orphan or (name.startswith(".manifest-") and name.endswith(".tmp")):
            item.unlink(missing_ok=True)
    return removed


RUN_NAME = versions.RUN_NAME
_run_time = versions.run_time


# -- the listings kept after a successful run ----------------------------------

KEPT_LISTINGS = 3


def _listing_files(paths: Paths, pair_id: str) -> list[Path]:
    """The current listings and the filters' fingerprint: what a run needs
    to know the last synced state."""
    workdir = paths.workdir(pair_id)
    files = sorted(workdir.glob("*.lst")) if workdir.is_dir() else []
    filters = paths.filters_file(pair_id)
    md5 = filters.with_name(filters.name + ".md5")
    return files + ([md5] if md5.is_file() else [])


def keep_listings(paths: Paths, pair_id: str, now: float | None = None) -> Path | None:
    """After a successful run: a copy of the two listings (and the filters'
    fingerprint) in workdir/kept/<time>/, the last KEPT_LISTINGS kept. A
    listing lost or damaged later is put back from here (restore_listings)
    rather than replaced by a new alignment."""
    files = _listing_files(paths, pair_id)
    if sum(1 for f in files if f.suffix == ".lst") != 2:
        return None
    kept = paths.workdir(pair_id) / "kept"
    stamp = time.strftime(RUN_NAME, time.localtime(time.time() if now is None else now))
    folder = kept / stamp
    tmp = kept / (stamp + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for f in files:
        shutil.copy2(f, tmp / f.name)
    if folder.exists():
        shutil.rmtree(folder)
    os.replace(tmp, folder)
    for old in sorted(d for d in kept.iterdir() if d.is_dir() and _run_time(d.name) is not None)[:-KEPT_LISTINGS]:
        shutil.rmtree(old, ignore_errors=True)
    return folder


def kept_listings(paths: Paths, pair_id: str) -> list[tuple[str, float]]:
    """(name, time) of the kept copies that hold both listings, newest first."""
    kept = paths.workdir(pair_id) / "kept"
    found = []
    if kept.is_dir():
        for d in kept.iterdir():
            stamp = _run_time(d.name)
            if stamp is not None and d.is_dir() and len(list(d.glob("*.lst"))) == 2:
                found.append((d.name, stamp))
    return sorted(found, key=lambda item: item[1], reverse=True)


def restore_listings(paths: Paths, pair_id: str, name: str) -> None:
    """The kept copy `name` made the current listings: what is in the
    workdir now (listings, their -old and -err leftovers, bisync's lock)
    goes aside into kept/replaced-<time>/ first."""
    if _run_time(name) is None:
        raise ValueError(f"not a kept copy: {name!r}")
    workdir = paths.workdir(pair_id)
    source = workdir / "kept" / name
    if len(list(source.glob("*.lst"))) != 2:
        raise FileNotFoundError(str(source))
    aside = workdir / "kept" / time.strftime("replaced-" + RUN_NAME)
    aside.mkdir(parents=True, exist_ok=True)
    for f in list(workdir.glob("*.lst*")) + list(workdir.glob("*.lck")):
        if f.is_file():
            os.replace(f, aside / f.name)
    for f in source.iterdir():
        if f.suffix == ".lst":
            shutil.copy2(f, workdir / f.name)
        elif f.name.endswith(".md5"):
            filters = paths.filters_file(pair_id)
            shutil.copy2(f, filters.with_name(filters.name + ".md5"))
