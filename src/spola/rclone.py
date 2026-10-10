"""Everything about talking to rclone: finding it, building bisync command
lines, the environment that carries the Koofr credentials, reading the JSON
log, and turning an exit code plus log into an Outcome.

Safety rules enforced here, and checked by tests/test_rclone.py:
  * --max-delete is always passed explicitly;
  * flags that can lose data (--delete-excluded, --inplace, --copy-links...)
    can never appear;
  * --resync and --force appear only when the caller asks for them by name,
    which the app does only after the user confirmed a dry-run preview;
  * the password never appears in the command line (anyone can read
    /proc/<pid>/cmdline), only in the child's environment.
"""

from __future__ import annotations

import enum
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import ngettext

REMOTE_NAME = "koofr"   # changing it would orphan every pair's bisync listings

FORBIDDEN_FLAGS = frozenset({
    "--delete-excluded", "--inplace", "--copy-links", "-L", "--links", "-l",
    "--ignore-listing-checksum", "--no-cleanup", "--remove-empty-dirs",
    "--delete-before", "--delete-during", "--ignore-errors", "--max-delete=-1",
})

# Below this many entries, a percentage is a poor guard ("1 of 4" is 25%):
# small folders may lose up to this many files without a confirmation.
MIN_FREE_DELETES = 5

# Default exclusions, in rclone filter syntax. A pattern without a leading
# "/" matches at any depth; "dir/**" leaves out a whole folder. The same list
# drives the filters file and default_excluded(), so they cannot disagree.
# Koofr Desktop's own list is included, so a migrated folder keeps out what
# Koofr Desktop kept out.
DEFAULT_EXCLUDES = (
    # temporary, lock and partial files
    ".~lock.*#", ".~*", "~$*", "~*.tmp", "*.part", ".part-*", "*.partial", "*.crdownload",
    ".goutputstream-*", "*.swp", "*.ffs_tmp",
    # files the systems write next to the user's ones
    "desktop.ini", "Desktop.ini", "Thumbs.db", "Thumbs.db:*", ".DS_Store", "*.lnk",
    # trash and system folders
    ".Trash-*/**", "$RECYCLE.BIN/**", "System Volume Information/**", ".Spotlight-V100/**",
    ".fseventsd/**", ".Trashes/**",
    # left behind by Koofr Desktop
    "SyncTrash/**", "/.cache/**", "*.syncState", ".syncignore",
    # rebuilt from package-lock.json: never worth syncing (and its .bin links are skipped anyway)
    "node_modules/**",
    # Spola's own
    ".spola-*",
)

DEFAULT_FILTERS = (
    "# Spola default exclusions (rclone filter syntax). Changing this file makes\n"
    "# bisync ask for a new first alignment, which Spola shows as a preview first.\n"
    + "".join(f"- {pattern}\n" for pattern in DEFAULT_EXCLUDES)
)


def default_excluded(rel: str) -> bool:
    """Would the default filters leave out this path (relative to the pair's
    root; folders may end with "/")? Used to skip scans and change events."""
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return False
    is_dir = rel.endswith("/")
    for pattern in DEFAULT_EXCLUDES:
        if pattern.endswith("/**"):
            name = pattern[:-3]
            if name.startswith("/"):
                # anchored: only the first component, and only as a folder
                if fnmatch.fnmatchcase(parts[0], name[1:]) and (len(parts) > 1 or is_dir):
                    return True
            elif any(fnmatch.fnmatchcase(part, name) for part in (parts if is_dir else parts[:-1])):
                return True
        elif fnmatch.fnmatchcase(parts[-1], pattern):
            return True
    return False


def conflict_suffix(local_word: str, remote_word: str) -> str:
    """--conflict-suffix for readable names: report.conflict-local-2026-10-10-1.odt.
    The trailing "-" separates the date from the number bisync appends. (The
    "pathname" loser mode would give cleaner names but overwrites the copies
    of an earlier conflict on the same day: never use it.)"""
    def clean(word: str) -> str:
        return "-".join(word.replace(",", " ").replace("/", " ").split()) or "conflict"
    return f"{clean(local_word)}-{{DateOnly}}-,{clean(remote_word)}-{{DateOnly}}-"


def filter_escape(path: str) -> str:
    """A literal path as an rclone filter glob, anchored at the root."""
    out = "".join("\\" + ch if ch in "\\*?[]{}" else ch for ch in path)
    return "/" + out


def exclude_lines(refused) -> str:
    lines = [filter_escape(r.path) + ("/**" if r.is_dir else "") for r in refused]
    return "\n".join(lines) + ("\n" if lines else "")


def find_rclone() -> str | None:
    override = os.environ.get("SPOLA_RCLONE")
    if override:
        return override
    if os.path.exists("/app/bin/rclone"):
        return "/app/bin/rclone"
    return shutil.which("rclone")


def remote_spec(remote_path: str) -> str:
    """'/data/work' -> 'koofr:data/work' (root: 'koofr:')."""
    return f"{REMOTE_NAME}:" + remote_path.strip("/")


def obscure(password: str, rclone: str) -> str:
    """rclone's reversible obfuscation, required for passwords given through
    the environment. Passed on stdin so it never shows in a command line."""
    proc = subprocess.run([rclone, "obscure", "-"], input=password, capture_output=True,
                          text=True, timeout=30, check=True, env=clean_env())
    return proc.stdout.strip()


def clean_env(base=None) -> dict:
    """The parent's environment without any RCLONE_* variable, so nothing the
    user set for their own rclone use (a config password, default flags,
    another remote called koofr) can change what Spola runs."""
    base = os.environ if base is None else base
    return {k: v for k, v in base.items() if not k.startswith("RCLONE_")}


def koofr_env(user: str, obscured_password: str, base=None) -> dict:
    env = clean_env(base)
    prefix = f"RCLONE_CONFIG_{REMOTE_NAME.upper()}_"
    env[prefix + "TYPE"] = "koofr"
    env[prefix + "PROVIDER"] = "koofr"
    env[prefix + "USER"] = user
    env[prefix + "PASSWORD"] = obscured_password
    return env


def effective_max_delete(percent: int, previous_entries: int | None) -> int:
    """The --max-delete percentage to pass, never below `percent`, but high
    enough that tiny folders may lose MIN_FREE_DELETES files without asking."""
    percent = max(1, min(100, int(percent)))
    if previous_entries:
        needed = math.ceil(100 * MIN_FREE_DELETES / previous_entries)
        percent = max(percent, min(100, needed))
    return percent


class Mode(enum.Enum):
    SYNC = "sync"           # a normal run
    RESYNC = "resync"       # first alignment or repair, never deletes
    CHECK = "check"         # compare listings only (--check-sync only)


@dataclass
class BisyncRequest:
    path1: str                  # local folder
    path2: str                  # remote spec, e.g. "koofr:data/work"
    workdir: Path
    rclone_config: Path
    filters_file: Path
    backup_dir1: Path
    max_delete: int
    mode: Mode = Mode.SYNC
    dry_run: bool = False
    force: bool = False         # only after the user confirmed a preview
    resync_mode: str = "newer"
    backup_dir2: str | None = None
    bwlimit: str | None = None
    exclude_from: Path | None = None   # per-run exclusions (names Koofr refuses)
    conflict_suffix: str = "conflict-local-{DateOnly}-,conflict-koofr-{DateOnly}-"
    user_agent: str | None = None
    extra_test_flags: list[str] = field(default_factory=list)   # tests only


def bisync_args(rclone: str, req: BisyncRequest) -> list[str]:
    args = [
        rclone, "bisync", req.path1.rstrip("/") + "/", req.path2,
        "--config", str(req.rclone_config),
        "--workdir", str(req.workdir),
        "--filters-file", str(req.filters_file),
        "--max-delete", str(int(req.max_delete)),
        "--backup-dir1", str(req.backup_dir1),
        "--conflict-resolve", "none",
        "--conflict-loser", "num",
        "--conflict-suffix", req.conflict_suffix,
        "--suffix-keep-extension",
        "--compare", "size,modtime,checksum",
        "--slow-hash-sync-only",
        "--create-empty-src-dirs",
        "--resilient", "--recover",
        "--max-lock", "2m",
        # Koofr has no recursive listing: folders are listed one by one, and
        # 16 in parallel halve the time of 8 (data/work: 49 s instead of 95)
        "--checkers", "16",
        "--skip-links",
        "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE",
        "--color", "NEVER",
        "-v",
    ]
    if req.user_agent:
        args += ["--user-agent", req.user_agent]
    if req.exclude_from:
        # a separate file on purpose: bisync fingerprints only --filters-file,
        # and a change there would demand a new first alignment
        args += ["--exclude-from", str(req.exclude_from)]
    if req.backup_dir2:
        args += ["--backup-dir2", req.backup_dir2]
    if req.bwlimit:
        args += ["--bwlimit", req.bwlimit]
    if req.mode is Mode.RESYNC:
        args += ["--resync", "--resync-mode", req.resync_mode]
    elif req.mode is Mode.CHECK:
        args += ["--check-sync", "only"]
    if req.force:
        args.append("--force")
    if req.dry_run:
        args.append("--dry-run")
    args += req.extra_test_flags
    bad = [a for a in args if a in FORBIDDEN_FLAGS or a.split("=")[0] in FORBIDDEN_FLAGS]
    if bad:
        raise ValueError(f"refusing to run rclone with {bad}")
    return args


# ---------------------------------------------------------------------------
# JSON log
# ---------------------------------------------------------------------------

class Kind(enum.Enum):
    STATS = "stats"
    DELTA = "delta"              # change found on one side
    QUEUE = "queue"              # planned operation (copy, delete)
    CONFLICT = "conflict"        # changed on both sides
    CONFLICT_RENAME = "conflict-rename"
    PHASE = "phase"              # "Path2 ... copying files to Path1": direction of what follows
    COPIED = "copied"
    BACKUP = "backup"            # a file moved into the backup folder
    SKIPPED = "skipped"          # dry-run: what would have been done
    SAFETY_ABORT = "safety-abort"
    CRITICAL = "critical"
    ERROR = "error"
    SUCCESS = "success"
    OTHER = "other"


@dataclass
class Event:
    kind: Kind
    message: str
    level: str = "info"
    side: str = ""               # "Path1" (local) or "Path2" (Koofr)
    action: str = ""             # e.g. "File is new", "Queue copy to Path2"
    path: str = ""
    stats: dict | None = None


_DELTA = re.compile(r"^- (Path[12])\s+(File (?:changed: .*?|is new|was deleted))\s+- (.*)$", re.S)
_QUEUE = re.compile(r"^- (Path[12])\s+(Queue (?:copy to Path[12]|delete))\s+- (.*)$", re.S)
_CONFLICT = re.compile(r"^- WARNING\s+New or changed in both paths\s+- (.*)$", re.S)
_RENAME = re.compile(r"^- (Path[12])\s+Renaming Path[12] copy\s+- (.*)$", re.S)
_PHASE = re.compile(r"^- (Path[12])\s+(?:Resync is copying files to|Do queued copies to)\s+- (Path[12])$")


def parse_line(line: str) -> Event | None:
    line = line.strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        # rclone prints a few plain lines before logging is set up
        return Event(Kind.OTHER, line, level="notice")
    if not isinstance(d, dict):
        return None
    msg = str(d.get("msg", "")).strip()
    level = str(d.get("level", "info"))
    if isinstance(d.get("stats"), dict):
        return Event(Kind.STATS, msg, level, stats=d["stats"])
    if (m := _DELTA.match(msg)):
        return Event(Kind.DELTA, msg, level, side=m[1], action=m[2], path=m[3].strip())
    if (m := _QUEUE.match(msg)):
        return Event(Kind.QUEUE, msg, level, side=m[1], action=m[2], path=m[3].strip())
    if (m := _CONFLICT.match(msg)):
        return Event(Kind.CONFLICT, msg, level, path=m[1].strip())
    if (m := _RENAME.match(msg)):
        return Event(Kind.CONFLICT_RENAME, msg, level, side=m[1], path=m[2].strip())
    if (m := _PHASE.match(msg)):
        # side = where the copies go to
        return Event(Kind.PHASE, msg, level, side=m[2], action=f"{m[1]}->{m[2]}")
    if d.get("object") == "Safety abort" or msg.startswith("Safety abort") or "too many deletes" in msg:
        return Event(Kind.SAFETY_ABORT, msg, level)
    if msg.startswith("Bisync critical error"):
        return Event(Kind.CRITICAL, msg, level)
    if msg == "Bisync successful":
        return Event(Kind.SUCCESS, msg, level)
    if d.get("skipped"):
        return Event(Kind.SKIPPED, msg, level, action=str(d["skipped"]), path=str(d.get("object", "")))
    # a move into --backup-dir: "Moved (server-side)" without " to: <new name>"
    # (that form is a conflict rename), then on some backends "Moved into backup dir"
    if (msg == "Moved (server-side)" or msg.startswith("Moved into backup dir")) and d.get("object"):
        return Event(Kind.BACKUP, msg, level, path=str(d["object"]))
    if msg.startswith("Copied") and d.get("object"):
        return Event(Kind.COPIED, msg, level, path=str(d["object"]))
    if level in ("error", "critical"):
        obj = d.get("object")
        return Event(Kind.ERROR, msg, level, path=str(obj) if obj and d.get("objectType") != "string" else "")
    return Event(Kind.OTHER, msg, level)


# ---------------------------------------------------------------------------
# Outcome of a run
# ---------------------------------------------------------------------------

class Result(enum.Enum):
    OK = "ok"
    NEEDS_CONFIRMATION = "needs-confirmation"   # safety abort: too many deletes, all files changed
    NEEDS_REPAIR = "needs-repair"               # critical: listings lost, empty side...
    RETRY = "retry"                             # transient (network, lock...)
    FAILED = "failed"                           # usage error, rclone missing...
    CANCELLED = "cancelled"


@dataclass
class Plan:
    """What a run did, or (dry-run) would do, in user terms."""
    upload: list[str] = field(default_factory=list)       # local -> Koofr
    download: list[str] = field(default_factory=list)     # Koofr -> local
    delete_local: list[str] = field(default_factory=list)
    delete_remote: list[str] = field(default_factory=list)
    replace_local: list[str] = field(default_factory=list)    # local version moved to the backup
    refused: list[str] = field(default_factory=list)          # left out: names Koofr refuses
    error_paths: list[str] = field(default_factory=list)      # files rclone reported an error for
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.upload or self.download or self.delete_local or self.delete_remote
                    or self.replace_local or self.conflicts)


def describe(plan: Plan) -> str:
    """One line for the row and the toasts: "3 files to Koofr, 1 deleted"."""
    parts = []
    if plan.upload:
        parts.append(ngettext("{n} file to Koofr", "{n} files to Koofr", len(plan.upload)).format(n=len(plan.upload)))
    if plan.download or plan.replace_local:
        n = len(set(plan.download) | set(plan.replace_local))
        parts.append(ngettext("{n} file from Koofr", "{n} files from Koofr", n).format(n=n))
    deleted = len(plan.delete_local) + len(plan.delete_remote)
    if deleted:
        parts.append(ngettext("{n} deleted", "{n} deleted", deleted).format(n=deleted))
    if plan.conflicts:
        parts.append(ngettext("{n} conflict", "{n} conflicts", len(plan.conflicts)).format(n=len(plan.conflicts)))
    return ", ".join(parts)


@dataclass
class Outcome:
    result: Result
    exit_code: int
    message: str = ""
    plan: Plan = field(default_factory=Plan)
    stats: dict | None = None
    network: bool = False      # the run failed because Koofr could not be reached


NETWORK_HINTS = ("no such host", "dial tcp", "connection refused", "connection reset",
                 "i/o timeout", "network is unreachable", "tls handshake timeout",
                 "context deadline exceeded", "temporary failure in name resolution",
                 "no route to host")


# rclone names a remote whose options it overrides with a hash suffix:
# "koofr{Y64jj}:spola-test/a.txt", "local{HK82T}:/home/u/folder/a.txt".
_REMOTE_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+(?:\{[A-Za-z0-9]+\})?:")


def _strip_remote(path: str) -> str:
    if path.startswith("/"):
        return path
    return _REMOTE_PREFIX.sub("", path, count=1)


def relative_path(path: str, roots: list[str]) -> str:
    """The path of a log line relative to the root of its side."""
    path = _strip_remote(path)
    for root in roots:
        root = _strip_remote(root).rstrip("/") + "/"
        if root != "/" and path.startswith(root):
            return path[len(root):]
    return path.lstrip("/")


def summarize(exit_code: int, events: list[Event], path1: str, path2: str,
              cancelled: bool = False) -> Outcome:
    roots = [path1, path2]
    plan = Plan()
    stats = None
    first_error = ""
    resync = any(ev.kind is Kind.PHASE and "Resync" in ev.message for ev in events)
    into = ""          # side the current copy phase writes to
    for ev in events:
        if ev.kind is Kind.STATS:
            stats = ev.stats
        elif ev.kind is Kind.PHASE:
            into = ev.side
        elif resync and ev.kind in (Kind.SKIPPED, Kind.COPIED) and into:
            # --resync reports no "Queue" lines: what it copies (or, in a
            # dry run, would copy) belongs to the direction of the phase.
            if ev.kind is Kind.SKIPPED and ev.action == "move":
                if into == "Path1":
                    plan.replace_local.append(ev.path)
            elif ev.kind is Kind.COPIED or ev.action == "copy":
                (plan.download if into == "Path1" else plan.upload).append(ev.path)
        elif resync and ev.kind is Kind.BACKUP and into == "Path1":
            plan.replace_local.append(ev.path)
        elif ev.kind is Kind.QUEUE:
            rel = relative_path(ev.path, roots)
            if ev.action == "Queue copy to Path2":
                plan.upload.append(rel)
            elif ev.action == "Queue copy to Path1":
                plan.download.append(rel)
            elif ev.action == "Queue delete":
                (plan.delete_local if ev.side == "Path1" else plan.delete_remote).append(rel)
        elif ev.kind is Kind.CONFLICT:
            plan.conflicts.append(relative_path(ev.path, roots))
        elif ev.kind in (Kind.ERROR, Kind.CRITICAL, Kind.SAFETY_ABORT):
            plan.errors.append(ev.message)
            first_error = first_error or ev.message
            if ev.kind is Kind.ERROR and ev.path:
                plan.error_paths.append(relative_path(ev.path, roots))

    kinds = {ev.kind for ev in events}
    if cancelled:
        result = Result.CANCELLED
    elif Kind.SAFETY_ABORT in kinds:
        result = Result.NEEDS_CONFIRMATION
    elif exit_code == 7 or Kind.CRITICAL in kinds:
        result = Result.NEEDS_REPAIR
    elif exit_code == 0:
        result = Result.OK
    elif exit_code == 2:
        result = Result.FAILED
    else:
        result = Result.RETRY
    # A network failure in the middle of a run can end as a critical error; the
    # next run recovers it (--recover), so it is a retry, not a repair. If the
    # retry fails again without network errors, that one asks for the repair.
    network = any(hint in message.lower() for message in plan.errors for hint in NETWORK_HINTS)
    if network and result in (Result.NEEDS_REPAIR, Result.RETRY):
        result = Result.RETRY
    for name in ("upload", "download", "delete_local", "delete_remote", "replace_local", "conflicts"):
        setattr(plan, name, sorted(set(getattr(plan, name)), key=lambda s: (s.count("/"), s.casefold())))
    return Outcome(result, exit_code, first_error, plan, stats, network)
