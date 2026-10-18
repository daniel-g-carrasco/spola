"""Previous versions: the copies Spola keeps here of what a sync replaced or
deleted in a synced folder, seen and restored. No GTK, no D-Bus: the window
and the file managers' service build on this.

Where the copies are (checked with rclone 1.75.2 and the engine's own
arguments, 2026-10-18): each run of bisync gets `--backup-dir1
<backup_dir(pair)>/<YYYYMMDD-HHMMSS>` (the time the run was prepared), and
rclone moves there, under its own relative path and its own name (no
suffix: --suffix-keep-extension does nothing without --suffix), every file
of this computer that the run replaces with Koofr's version or deletes
because it was deleted on Koofr. One copy per path per run. A file deleted
here by the user has no copy here: it is in the system's wastebasket, and
after the next run in Koofr's trash (the window says so).

Next to each run folder, a manifest `<run>.spola.json` says why each file
is there: "replaced" (a sync brought Koofr's newer version), "deleted"
(deleted here because deleted on Koofr), "realigned" (a first alignment or
a repair settled it by date), "conflict" (the copy not kept when a conflict
was resolved), "restored" (the file that was in its place when a version
was put back). Written whole or not at all; removed with its run by the
retention (jobs.prune_backups). Runs from before the manifests, or a file
the manifest does not name: "replaced" when a file is in its place now,
else "deleted"; only a label.

Nothing here ever deletes a copy: the only deletion is the retention.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Pair
from .i18n import translation_for, _
from .paths import Paths

RUN_NAME = "%Y%m%d-%H%M%S"
MANIFEST = ".spola.json"           # <run>.spola.json, next to the run folder
REASONS = ("replaced", "deleted", "realigned", "conflict", "restored")


def run_time(name: str) -> float | None:
    """When the run named `name` started, or None for a name not ours (a
    folder or file that is not a run: never touched)."""
    try:
        return time.mktime(time.strptime(name, RUN_NAME))
    except ValueError:
        return None


def safe_rel(rel: str) -> str:
    """`rel` if it is a relative path inside a folder, else ValueError."""
    parts = rel.replace("\\", "/").split("/")
    if not rel or rel.startswith("/") or ".." in parts or "" in parts:
        raise ValueError(f"not a path inside the folder: {rel!r}")
    return "/".join(parts)


@dataclass(frozen=True)
class Version:
    pair_id: str
    run: str            # "20261017-123000"
    rel: str            # the file's path in the synced folder (and in the run)
    size: int
    time: float         # when the run started
    reason: str

    @property
    def id(self) -> str:
        """What the D-Bus service and the window pass around: run/rel."""
        return f"{self.run}/{self.rel}"

    def expires(self, days: int) -> float:
        return self.time + days * 86400


@dataclass
class Summary:
    files: int = 0
    size: int = 0
    oldest: float = 0.0          # the oldest run's time, 0 with no copy


# -- manifests -------------------------------------------------------------------

def manifest_path(paths: Paths, pair_id: str, run: str) -> Path:
    return paths.backup_dir(pair_id) / (run + MANIFEST)


def read_manifest(paths: Paths, pair_id: str, run: str) -> dict[str, str]:
    try:
        data = json.loads(manifest_path(paths, pair_id, run).read_text(encoding="utf-8"))
        files = data.get("files", {}) if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in files.items() if v in REASONS}
    except (OSError, ValueError):
        return {}


def write_manifest(paths: Paths, pair_id: str, run: str, reasons: dict[str, str]) -> None:
    """The run's manifest, merged with what it says already, written whole
    or not at all (temporary file, fsync, rename)."""
    if run_time(run) is None:
        raise ValueError(f"not a run: {run!r}")
    merged = read_manifest(paths, pair_id, run)
    merged.update({rel: reason for rel, reason in reasons.items() if reason in REASONS})
    target = manifest_path(paths, pair_id, run)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"format": 1, "run": run, "files": merged}, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def files_of_run(paths: Paths, pair_id: str, run: str) -> list[tuple[str, int]]:
    """(relative path, size) of every file kept by that run."""
    root = paths.backup_dir(pair_id) / run
    found = []
    for folder, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(folder, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            found.append((os.path.relpath(full, root).replace("\\", "/"), st.st_size))
    return sorted(found)


def record_run(paths: Paths, pair: Pair, run: str, deleted: Iterable[str], realigned: bool) -> int:
    """After a run: the manifest of what it kept, from what the run said it
    did (`deleted`: the paths it deleted here) and the kind of run. Returns
    how many files the run kept (0: no folder, no manifest)."""
    if run_time(run) is None or not (paths.backup_dir(pair.id) / run).is_dir():
        return 0
    gone = set(deleted)
    kept = files_of_run(paths, pair.id, run)
    reasons = {rel: "deleted" if rel in gone else ("realigned" if realigned else "replaced") for rel, _size in kept}
    if reasons:
        write_manifest(paths, pair.id, run, reasons)
    return len(kept)


# -- reading ---------------------------------------------------------------------

def scan(paths: Paths, pair: Pair) -> list[Version]:
    """Every copy kept for this pair, the newest first (a stat walk of the
    backups: off the main loop)."""
    root = paths.backup_dir(pair.id)
    if not root.is_dir():
        return []
    found: list[Version] = []
    for run in root.iterdir():
        started = run_time(run.name)
        if started is None or not run.is_dir() or run.is_symlink():
            continue
        reasons = read_manifest(paths, pair.id, run.name)
        for rel, size in files_of_run(paths, pair.id, run.name):
            reason = reasons.get(rel) or ("replaced" if os.path.lexists(os.path.join(pair.local, rel)) else "deleted")
            found.append(Version(pair.id, run.name, rel, size, started, reason))
    return sorted(found, key=lambda v: (-v.time, v.rel))


@dataclass
class Index:
    """The copies of one pair, as `scan` found them, and the questions the
    window and the file managers ask of them."""
    pair: Pair
    versions: list[Version] = field(default_factory=list)

    def of(self, rel: str) -> list[Version]:
        """The copies of the file at `rel`, newest first."""
        return [v for v in self.versions if v.rel == rel]

    def deleted_under(self, folder: str) -> list[Version]:
        """The copies of files under `folder` ("" for the whole pair) that
        are not in their place any more, newest first."""
        prefix = folder.rstrip("/") + "/" if folder else ""
        return [v for v in self.versions if v.rel.startswith(prefix)
                and not os.path.lexists(os.path.join(self.pair.local, v.rel))]

    def names_in(self, folder: str) -> set[str]:
        """The names directly in `folder` that have at least one copy (for
        the file managers' menus)."""
        prefix = folder.rstrip("/") + "/" if folder else ""
        return {v.rel[len(prefix):] for v in self.versions
                if v.rel.startswith(prefix) and "/" not in v.rel[len(prefix):]}

    def by_id(self, vid: str) -> Version | None:
        return next((v for v in self.versions if v.id == vid), None)

    def summary(self) -> Summary:
        if not self.versions:
            return Summary()
        return Summary(len(self.versions), sum(v.size for v in self.versions), min(v.time for v in self.versions))


# -- restoring -------------------------------------------------------------------

def new_run(paths: Paths, pair_id: str, now: float | None = None) -> str:
    """A run of Spola's own (a restore, a conflict resolved): named like the
    sync runs, so the retention treats it the same, and never one that
    exists (a second later until free)."""
    t = int(time.time() if now is None else now)
    root = paths.backup_dir(pair_id)
    while True:
        name = time.strftime(RUN_NAME, time.localtime(t))
        if not (root / name).exists() and not manifest_path(paths, pair_id, name).exists():
            return name
        t += 1


def keep_aside(paths: Paths, pair: Pair, rel: str, reason: str, source: str | None = None,
               now: float | None = None) -> Version:
    """Move the file at `rel` (or at `source`, an absolute path inside the
    pair, kept under `rel`) into a new run with its reason: a copy among
    the others, kept until the retention, never lost."""
    rel = safe_rel(rel)
    src = source or os.path.join(pair.local, rel)
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    run = new_run(paths, pair.id, now)
    target = paths.backup_dir(pair.id) / run / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, target)
    except OSError:                    # another disk (a folder mounted inside the pair)
        shutil.move(src, target)
    write_manifest(paths, pair.id, run, {rel: reason})
    return Version(pair.id, run, rel, os.lstat(target).st_size, run_time(run) or 0.0, reason)


def version_name(rel: str, when: float, language: str = "") -> str:
    """'relazione (versione del 2026-10-17 12.30).odt': where a version goes
    when restored as a copy, in the language of the conflict copies."""
    folder, name = os.path.split(rel)
    stem, ext = os.path.splitext(name)
    if name.startswith(".") and not ext:        # ".bashrc": all stem
        stem, ext = name, ""
    stamp = time.strftime("%Y-%m-%d %H.%M", time.localtime(when))
    tr = translation_for(language).gettext if language else _
    # TRANSLATORS: a restored version next to the file, e.g. "report (version of 2026-10-17 12.30).odt"
    new = tr("{stem} (version of {date}){ext}").format(stem=stem, date=stamp, ext=ext)
    return f"{folder}/{new}" if folder else new


def free_name(root: str, rel: str) -> str:
    """`rel` if nothing is there, else "name (2).ext", "name (3).ext"..."""
    if not os.path.lexists(os.path.join(root, rel)):
        return rel
    stem, ext = os.path.splitext(rel)
    for i in range(2, 1000):
        candidate = f"{stem} ({i}){ext}"
        if not os.path.lexists(os.path.join(root, candidate)):
            return candidate
    raise FileExistsError(os.path.join(root, rel))


def copy_path(paths: Paths, version: Version) -> Path:
    return paths.backup_dir(version.pair_id) / version.run / version.rel


def _copy_in(src: Path, dest: str) -> None:
    """A copy of `src` at `dest`, whole or not at all (a temporary name in
    the same folder, then a rename)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".spola-restore-", dir=os.path.dirname(dest))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def restore(paths: Paths, pair: Pair, version: Version, mode: str, language: str = "",
            now: float | None = None) -> str:
    """Put a version back; returns the relative path written. "copy": next
    to the file, under version_name (a free one). "replace": in the file's
    place; the file there now is kept aside first ("restored"), so a
    replace can be undone from the same list. A file no longer there is
    simply put back in its place. The copy itself stays where it is: a
    restore never consumes it. The next sync uploads what was written."""
    if mode not in ("copy", "replace"):
        raise ValueError(mode)
    rel = safe_rel(version.rel)
    src = copy_path(paths, version)
    if not src.is_file() or src.is_symlink():
        raise FileNotFoundError(str(src))
    here = os.path.join(pair.local, rel)
    if mode == "copy" and os.path.lexists(here):
        target = free_name(pair.local, version_name(rel, version.time, language))
    else:
        if os.path.lexists(here):
            keep_aside(paths, pair, rel, "restored", now=now)
        target = rel
    _copy_in(src, os.path.join(pair.local, target))
    return target


def open_copy(paths: Paths, version: Version, folder: str) -> str:
    """A read-only copy of the version in `folder` (a temporary one), for
    opening it with the default app without any chance of changing it."""
    name = os.path.basename(version.rel)
    target = os.path.join(folder, version.run, name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copy2(copy_path(paths, version), target)
    os.chmod(target, 0o444)
    return target
