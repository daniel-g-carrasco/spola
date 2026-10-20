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
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _, count, ngettext
from .listings import go_unquote

REMOTE_NAME = "koofr"   # changing it would orphan every pair's bisync listings

FORBIDDEN_FLAGS = frozenset({
    "--delete-excluded", "--inplace", "--copy-links", "-L", "--links", "-l",
    "--ignore-listing-checksum", "--no-cleanup", "--remove-empty-dirs",
    "--delete-before", "--delete-during", "--ignore-errors", "--max-delete=-1",
    "--size-only", "--ignore-size", "--ignore-checksum", "--immutable", "--backup-dir", "--suffix",
})
# flags whose value, not the flag, is the danger: a conflict loser deleted,
# a conflict settled by a rule instead of kept twice
FORBIDDEN_VALUES = {"--conflict-loser": {"delete"},
                    "--conflict-resolve": {"newer", "older", "larger", "smaller", "path1", "path2"}}

# Below this many entries, a percentage is a poor guard ("1 of 4" is 25%):
# small folders may lose up to this many files without a confirmation.
MIN_FREE_DELETES = 5
# Above, a percentage is too generous: 10% of 40,000 files is 4,000 gone
# without a word. Never more than this many unasked (bisync takes whole
# percentage points, so the real cap is the first point at or above it).
MAX_FREE_DELETES = 500
# The same for files changed here at once (a snapshot put back, a bulk edit,
# a program that rewrites a tree): what goes up over Koofr's copies without
# asking. bisync has no such check; Spola counts them before it runs.
MIN_FREE_CHANGES = 50
MAX_FREE_CHANGES = 500

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
    return excluded_by(rel, DEFAULT_EXCLUDES)


def excluded_by(rel: str, patterns) -> bool:
    """Would these exclusion patterns (rclone's, as in DEFAULT_EXCLUDES or a
    pair's own filters) leave out this path?"""
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return False
    is_dir = rel.endswith("/")
    for pattern in patterns:
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
    # a name with a newline would split the file into two wrong rules: such a
    # character becomes "?", one character of any kind (a near-twin of the
    # name is left out too, which is rare and harmless)
    lines = [re.sub(r"[\x00-\x1f\x7f]", "?", filter_escape(r.path)) + ("/**" if r.is_dir else "") for r in refused]
    return "\n".join(lines) + ("\n" if lines else "")


# Windows 10 (before build 22000) takes CON, PRN, AUX, NUL, COM1-9 and
# LPT1-9, with any extension, for devices in every folder: such a file, which
# Koofr accepts, rclone can still write (through \\?\ paths), but other
# programs can neither open nor delete it. Windows 11 takes them for devices
# only as bare relative names: in a folder they are ordinary files. The
# characters Windows refuses (: * ? " < > |, a final dot or space) rclone
# writes as their full-width look-alikes and back, on every Windows.
WINDOWS_RESERVED = r"{{(?i:con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(?:[ ]*\.[^/]*)?}}"


def windows_reserved_names_matter() -> bool:
    return sys.platform == "win32" and sys.getwindowsversion().build < 22000


def reserved_exclude_lines() -> str:
    """--exclude-from lines leaving them out, files and folders, anywhere."""
    return f"{WINDOWS_RESERVED}\n{WINDOWS_RESERVED}/**\n"


def find_rclone() -> str | None:
    """The rclone Spola runs, with the account's password in its
    environment. The Windows build: only the rclone.exe it ships, next to
    its own files (SPOLA_RCLONE and the PATH are the environment's, which
    any program of the user can change). The Flatpak: its own. From the
    sources: SPOLA_RCLONE (the tests', a developer's), else the PATH's."""
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))) / "rclone.exe"
        return str(bundled) if bundled.is_file() else None
    override = os.environ.get("SPOLA_RCLONE")
    if override:
        return override
    if os.path.exists("/app/bin/rclone"):
        return "/app/bin/rclone"
    return shutil.which("rclone")


def remote_spec(remote_path: str) -> str:
    """'/data/work' -> 'koofr:data/work' (root: 'koofr:')."""
    return f"{REMOTE_NAME}:" + remote_path.strip("/")


def no_window() -> dict:
    """subprocess options for a short rclone call: on Windows a console
    program started from the window would get a console of its own, shown
    for the moment it runs (Windows Terminal hosts it, whatever the window
    asks)."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def obscure(password: str, rclone: str) -> str:
    """rclone's reversible obfuscation, required for passwords given through
    the environment. Passed on stdin so it never shows in a command line."""
    proc = subprocess.run([rclone, "obscure", "-"], input=password, capture_output=True,
                          text=True, timeout=30, check=True, env=clean_env(), **no_window())
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
        cap = math.ceil(100 * MAX_FREE_DELETES / previous_entries)
        percent = max(min(percent, cap), min(100, needed))
    return percent


def allowed_changes(percent: int, previous_entries: int) -> int:
    """How many files may change here at once without a confirmation:
    `percent` of the last listing, never below MIN_FREE_CHANGES, never
    above MAX_FREE_CHANGES."""
    share = previous_entries * max(1, min(100, int(percent))) // 100
    return max(MIN_FREE_CHANGES, min(share, MAX_FREE_CHANGES))


def confirmed_max_changes(plan: Plan, bases, seen: int = 0) -> int:
    """The files changed here a confirmed preview showed (its uploads of
    files this computer's listing already had, and its conflicts: changed
    here and on Koofr both), plus a small margin: what the run that carries
    it out may find changed, no more. `seen` is what the check counted when
    it stopped the run (jobs.changed_here): a file changed here the same way
    as on Koofr is in neither list of the plan, but counts for the check all
    the same, and the confirmed run would stop again for it."""
    known = bases[0] if bases else {}
    changed = sum(1 for path in plan.upload if path in known) + len(plan.conflicts)
    changed = max(changed, int(seen or 0))
    return changed + max(2, math.ceil(changed * CONFIRMED_MARGIN / 100))


# what a confirmed run may delete beyond what its preview showed: bisync
# counts whole percentage points, and the preview's deletions and the run's
# deltas can differ by a folder or two
CONFIRMED_MARGIN = 2


def confirmed_max_delete(plan: Plan, counts: tuple[int, int] | None, excess: dict | None = None) -> int:
    """The --max-delete for the run that carries out a confirmed preview:
    the share of each side's entries the preview deletes (moves count as
    deletions there, as bisync counts them), plus a small margin. A run that
    would delete more than that stops again and asks again, instead of
    carrying it out on the strength of a confirmation given for less.
    Without the listings' counts, nothing to bound against: 100.
    `excess` is what bisync counted when it stopped the run (Outcome.excess):
    a file gone from both sides since the last run counts for its check too,
    but is in no list of the preview (there is nothing to do for it), and
    a bound taken from the preview alone would stop the confirmed run again,
    and again at every confirmation."""
    if not counts:
        return 100
    deleted = (len(plan.delete_remote) + len(plan.moved_remote),      # gone here, deleted on Koofr
               len(plan.delete_local) + len(plan.moved_local))       # gone on Koofr, deleted here
    percent = max(math.ceil(100 * n / c) if c else 0 for n, c in zip(deleted, counts, strict=True))
    for n, c in (excess or {}).values():
        if c:
            percent = max(percent, math.ceil(100 * n / c))
    return max(1, min(100, percent + CONFIRMED_MARGIN))


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
        # Koofr keeps modification times to the millisecond, this computer to
        # the nanosecond. A file moved here (--track-renames) keeps its own
        # time while bisync records Koofr's, and the next run would take the
        # difference for a change and upload the file again: under 1 ms, two
        # times are the same.
        "--modify-window", "1ms",
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
    if req.mode is Mode.SYNC:
        # A file moved or renamed on one side is moved on the other, instead
        # of deleted there and copied again (matched by size and MD5, which
        # both sides have; Koofr moves on the server). Not on --resync, which
        # never deletes: rclone ignores it there with an ERROR line. A move
        # still counts as a delete for --max-delete, so a big reorganisation
        # stops for a confirmation as before, with the moves in its preview.
        args.append("--track-renames")
        # Which file is the moved one, among those with the same size and
        # MD5: the one with the same modification time (within
        # --modify-window). By hash alone, two identical files moved in the
        # same run (a folder reorganised, with copies of a document in it)
        # are matched at random, and the one moved here keeps the date of
        # the other: the next run sees it changed, and uploads it again
        # with the wrong date (737 files on 2026-10-19).
        args += ["--track-renames-strategy", "hash,modtime"]
        # The files a run queues are copied as they are, without asking
        # first whether the other side has them already. That question is
        # what goes wrong on Koofr: it cannot change a file's date, so for a
        # file whose date alone changed rclone deletes the Koofr copy while
        # it compares, up to 10,000 files ahead of their uploads; a run
        # stopped then (a laptop suspended) left 10,020 files missing on
        # Koofr on 2026-10-15. Copied without the question, a file is
        # overwritten on Koofr only when its upload starts. bisync queues
        # only what changed, and tells files equal on both sides its own way.
        # Not on --resync, which compares everything and would copy it all.
        args.append("--ignore-times")
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
    for flag, value in zip(args, args[1:] + [""], strict=True):
        name, _eq, inline = flag.partition("=")
        if name in FORBIDDEN_VALUES and (inline or value) in FORBIDDEN_VALUES[name]:
            bad.append(f"{name} {inline or value}")
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
    CONFLICT = "conflict"        # new or changed on both sides: a conflict if bisync then renames the copies
    EQUAL = "equal"              # ...but the two are the same file: nothing to do
    CONFLICT_RENAME = "conflict-rename"
    PHASE = "phase"              # "Path2 ... copying files to Path1": direction of what follows
    COPIED = "copied"
    DELETED = "deleted"          # a file deleted, side = where (from its object type)
    BACKUP = "backup"            # a file moved into the backup folder
    MOVED = "moved"              # a file moved (path = from, target = to): a rename, a conflict copy
    RENAMED = "renamed"          # --track-renames moved a file (path = its new name), after its MOVED
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
    target: str = ""             # where a file is moved to (MOVED, a dry run's SKIPPED move)
    size: int = 0                # bytes of a file a dry run would copy (SKIPPED copy)
    stats: dict | None = None


def _plain(path: str) -> str:
    """A path of a log line as bisync writes it: quoted the Go way when the
    name holds a quote, a backslash or a control character."""
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        try:
            return go_unquote(path)
        except ValueError:
            return path
    return path


_DELTA = re.compile(r"^- (Path[12])\s+(File (?:changed: .*?|is new|was deleted))\s+- (.*)$", re.S)
_QUEUE = re.compile(r"^- (Path[12])\s+(Queue (?:copy to Path[12]|delete))\s+- (.*)$", re.S)
_CONFLICT = re.compile(r"^- WARNING\s+New or changed in both paths\s+- (.*)$", re.S)
_EQUAL = re.compile(r"^Files are equal! Skipping: (.*)$", re.S)
_EMPTY_SIDE = re.compile(r"Empty current (Path[12]) listing")
# bisync's own count when it stops for too many deletions: the files gone
# from that side since the last run, and the entries that side had
_EXCESS = re.compile(r"too many deletes \(>\d+%, (\d+) of (\d+)\) on (Path[12])")
_RENAME = re.compile(r"^- (Path[12])\s+Renaming Path[12] copy\s+- (.*)$", re.S)
_PHASE = re.compile(r"^- (Path[12])\s+(?:Resync is copying files to|Do queued copies to)\s+- (Path[12])$")
# a move by name: "Moved (server-side) to: <new>" (a rename, or bisync's own
# rename of a conflict copy), and in a dry run "skipped": "move to <new>"; the
# object is the old name. --track-renames confirms each of its moves with
# "Renamed from \"<old>\"", whose object is the new name.
_MOVED_TO = re.compile(r"^Moved \(server-side\) to: (.+)$", re.S)
_RENAMED_FROM = re.compile(r'^Renamed from "', re.S)


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _side_of(d: dict) -> str:
    """Where a copied or deleted file is: "Path1" for this computer, "Path2"
    for Koofr, from rclone's object type ("*local.Object", "*koofr.Object")."""
    kind = str(d.get("objectType") or "")
    return "Path1" if "local." in kind else ("Path2" if kind else "")


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
        return Event(Kind.DELTA, msg, level, side=m[1], action=m[2], path=_plain(m[3]))
    if (m := _QUEUE.match(msg)):
        return Event(Kind.QUEUE, msg, level, side=m[1], action=m[2], path=_plain(m[3]))
    if (m := _CONFLICT.match(msg)):
        return Event(Kind.CONFLICT, msg, level, path=m[1].strip())
    if (m := _EQUAL.match(msg)):
        return Event(Kind.EQUAL, msg, level, path=m[1].strip())
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
        skipped = str(d["skipped"])
        target = skipped[len("move to "):] if skipped.startswith("move to ") else ""
        return Event(Kind.SKIPPED, msg, level, action=skipped, path=str(d.get("object", "")), target=target,
                     size=_int(d.get("size")))
    if (m := _MOVED_TO.match(msg)) and d.get("object"):
        return Event(Kind.MOVED, msg, level, path=str(d["object"]), target=m[1])
    if _RENAMED_FROM.match(msg) and d.get("object"):
        return Event(Kind.RENAMED, msg, level, path=str(d["object"]))
    # a move into --backup-dir: "Moved (server-side)" without " to: <new name>"
    # (that form is a conflict rename), then on some backends "Moved into backup dir"
    if (msg == "Moved (server-side)" or msg.startswith("Moved into backup dir")) and d.get("object"):
        return Event(Kind.BACKUP, msg, level, path=str(d["object"]))
    if msg.startswith("Copied") and d.get("object"):
        return Event(Kind.COPIED, msg, level, side=_side_of(d), path=str(d["object"]))
    if msg == "Deleted" and d.get("object"):
        return Event(Kind.DELETED, msg, level, side=_side_of(d), path=str(d["object"]))
    if msg == "Removing directory" and d.get("object"):
        # a folder deleted, or left empty by the deletions: the side is not said
        return Event(Kind.DELETED, msg, level, side="", path=str(d["object"]))
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
    # (from, to): moved or renamed on one side, moved the same way on the
    # other, instead of a delete plus an upload or a download
    moved_remote: list[tuple[str, str]] = field(default_factory=list)   # moved here, then on Koofr
    moved_local: list[tuple[str, str]] = field(default_factory=list)    # moved on Koofr, then here
    # what a dry run would copy each way, in bytes (a real run does not say)
    upload_bytes: int = 0
    download_bytes: int = 0
    # a dry run's sizes of the files it names, and the folders among the
    # paths above (for the preview's tree; a real run does not say)
    sizes: dict[str, int] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)

    @property
    def empty(self) -> bool:
        return not (self.upload or self.download or self.delete_local or self.delete_remote
                    or self.replace_local or self.conflicts or self.moved_remote or self.moved_local)


def fold_case_renames(plan: Plan, renames) -> None:
    """Renames by case alone (preflight.case_renames), done on Koofr before
    the run: bisync, which cannot tell the two names apart there, plans the
    files they carry as deleted on Koofr and uploaded again (a preview, which
    renames nothing), and then does nothing with them. One move each."""
    if not renames:
        return
    tops = [r.new.casefold() for r in renames]

    def renamed(path: str) -> bool:
        folded = path.casefold()
        return folded in both and any(folded == top or folded.startswith(top + "/") for top in tops)

    both = {p.casefold() for p in plan.upload} & {p.casefold() for p in plan.delete_remote}
    plan.upload = [p for p in plan.upload if not renamed(p)]
    plan.delete_remote = [p for p in plan.delete_remote if not renamed(p)]
    plan.moved_remote = [(r.old, r.new) for r in renames] + plan.moved_remote


def describe(plan: Plan) -> str:
    """One line for the row and the toasts: "3 files to Koofr, 1 deleted"."""
    parts = []
    if plan.upload:
        parts.append(ngettext("{n} file to Koofr", "{n} files to Koofr", len(plan.upload)).format(n=count(len(plan.upload))))
    if plan.download or plan.replace_local:
        n = len(set(plan.download) | set(plan.replace_local))
        parts.append(ngettext("{n} file from Koofr", "{n} files from Koofr", n).format(n=count(n)))
    moved = len(plan.moved_remote) + len(plan.moved_local)
    if moved:
        parts.append(ngettext("{n} moved", "{n} moved", moved).format(n=count(moved)))
    deleted = len(plan.delete_local) + len(plan.delete_remote)
    if deleted:
        parts.append(ngettext("{n} deleted", "{n} deleted", deleted).format(n=count(deleted)))
    if plan.conflicts:
        parts.append(ngettext("{n} conflict", "{n} conflicts", len(plan.conflicts)).format(n=count(len(plan.conflicts))))
    return ", ".join(parts)


@dataclass
class Outcome:
    result: Result
    exit_code: int
    message: str = ""
    plan: Plan = field(default_factory=Plan)
    stats: dict | None = None
    network: bool = False      # the run failed because Koofr could not be reached
    # which safety check stopped the run: "deletes" (too many deletions on
    # one side) or "all-changed" (every file of one side changed)
    safety: str = ""
    # a side with nothing at all ("Path1" here, "Path2" Koofr): bisync stops
    # rather than empty the other one; the folder may also be gone there
    empty_side: str = ""
    root_missing: bool = False  # a side's folder is not there (a resync's "directory not found")
    identical: int = 0          # a verification (verify.py): the files the same on both sides
    # a run stopped for too many deletions: {"Path1": (deleted, entries)}, as
    # bisync counted them (confirmed_max_delete)
    excess: dict = field(default_factory=dict)


NETWORK_HINTS = ("no such host", "dial tcp", "connection refused", "connection reset",
                 "i/o timeout", "network is unreachable", "tls handshake timeout",
                 "context deadline exceeded", "temporary failure in name resolution",
                 "no route to host")


# rclone names a remote whose options it overrides with a hash suffix, in
# URL-safe base64: "koofr{Y64jj}:spola-test/a.txt", "koofr{kFw_7}:data/a.jpg",
# "local{HK82T}:/home/u/folder/a.txt".
_REMOTE_PREFIX = re.compile(r"^[A-Za-z0-9_.-]+(?:\{[A-Za-z0-9_-]+\})?:")


_DRIVE = re.compile(r"^[A-Za-z]:(?:/|$)")


def _strip_remote(path: str) -> str:
    """A path of a log line or a root, as one plain path: without the remote
    ("koofr{kFw_7}:", "local{HK82T}:"; a drive letter is not one) and, on
    Windows, without the long-path prefix and with "/" only: rclone writes
    "local{HK82T}://?/C:/Users/me/x.txt" for C:\\Users\\me\\x.txt."""
    if not path.startswith("/") and not _DRIVE.match(path.replace("\\", "/")):
        path = _REMOTE_PREFIX.sub("", path, count=1)
    # only a Windows path: on Linux a name may hold a backslash
    windows = path.replace("\\", "/")
    if windows.startswith("//?/"):
        windows = windows[4:]
    return windows if _DRIVE.match(windows) else path


def relative_path(path: str, roots: list[str]) -> str:
    """The path of a log line relative to the root of its side."""
    path = _strip_remote(path)
    for root in roots:
        root = _strip_remote(root).rstrip("/") + "/"
        if root == "/":
            continue
        # Windows: the same folder whatever the case of its letters
        if path.startswith(root) or (_DRIVE.match(root) and path.casefold().startswith(root.casefold())):
            return path[len(root):]
    return path.lstrip("/")


def _count_bytes(plan: Plan, ev: Event, into: str) -> None:
    if ev.kind is Kind.SKIPPED and ev.action == "copy":
        if into == "Path1":
            plan.download_bytes += ev.size
        else:
            plan.upload_bytes += ev.size


def summarize(exit_code: int, events: list[Event], path1: str, path2: str,
              cancelled: bool = False) -> Outcome:
    roots = [path1, path2]
    plan = Plan()
    stats = None
    first_error = ""
    resync = any(ev.kind is Kind.PHASE and "Resync" in ev.message for ev in events)
    into = ""          # side the current copy phase writes to
    moving: dict[str, str] = {}      # new name -> old, until --track-renames confirms the move
    # "New or changed in both paths" is only a warning: bisync then finds the
    # two the same ("Files are equal! Skipping"), or it is a folder, or it
    # renames the two copies (a dry run: "skipped": "rename" on the file).
    # Only the last is a conflict.
    warned: str | None = None
    renamed: set[str] = set()
    for ev in events:
        if ev.kind is Kind.SKIPPED and ev.path and not ev.path.startswith("Local file system"):
            # what a dry run would do to a file says how big it is; to a folder, that it is one
            rel = relative_path(ev.path, roots)
            if ev.action in ("remove directory", "make directory"):
                plan.folders.add(rel)
            elif ev.size:
                plan.sizes[rel] = ev.size
                if ev.target:
                    plan.sizes[relative_path(ev.target, roots)] = ev.size
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
                _count_bytes(plan, ev, into)
        elif resync and ev.kind is Kind.BACKUP and into == "Path1":
            plan.replace_local.append(ev.path)
        elif ev.kind in (Kind.MOVED, Kind.SKIPPED) and ev.target:
            moving[relative_path(ev.target, roots)] = relative_path(ev.path, roots)
        elif ev.kind is Kind.SKIPPED and ev.action == "copy" and into:
            _count_bytes(plan, ev, into)
        elif ev.kind is Kind.RENAMED and into:
            new = relative_path(ev.path, roots)
            if new in moving:
                (plan.moved_remote if into == "Path2" else plan.moved_local).append((moving.pop(new), new))
        elif ev.kind is Kind.QUEUE:
            rel = relative_path(ev.path, roots)
            if ev.action == "Queue copy to Path2":
                plan.upload.append(rel)
            elif ev.action == "Queue copy to Path1":
                plan.download.append(rel)
            elif ev.action == "Queue delete":
                (plan.delete_local if ev.side == "Path1" else plan.delete_remote).append(rel)
        elif ev.kind is Kind.CONFLICT:
            warned = relative_path(ev.path, roots)
            plan.conflicts.append(warned)
        elif ev.kind is Kind.CONFLICT_RENAME and warned is not None:
            renamed.add(warned)
        elif ev.kind is Kind.SKIPPED and ev.action == "rename" and ev.path:
            renamed.add(relative_path(ev.path, roots))
        elif ev.kind in (Kind.ERROR, Kind.CRITICAL, Kind.SAFETY_ABORT):
            plan.errors.append(ev.message)
            first_error = first_error or ev.message
            if ev.kind is Kind.ERROR and ev.path:
                plan.error_paths.append(relative_path(ev.path, roots))

    plan.conflicts = [p for p in plan.conflicts if p in renamed]
    # a move was queued as a delete plus a copy: it is neither
    for moves, copies, deletes in ((plan.moved_remote, plan.upload, plan.delete_remote),
                                   (plan.moved_local, plan.download, plan.delete_local)):
        if moves:
            olds, news = {old for old, _new in moves}, {new for _old, new in moves}
            copies[:] = [p for p in copies if p not in news]
            deletes[:] = [p for p in deletes if p not in olds]

    empty_side, root_missing, safety = "", False, ""
    excess: dict[str, tuple[int, int]] = {}
    for ev in events:
        if (m := _EMPTY_SIDE.search(ev.message)):
            empty_side = m[1]
        elif "root directory: directory not found" in ev.message:
            root_missing = True
        elif ev.kind is Kind.SAFETY_ABORT:
            safety = "all-changed" if "all files were changed" in ev.message else "deletes"
            if (m := _EXCESS.search(ev.message)):
                excess[m[3]] = (int(m[1]), int(m[2]))

    kinds = {ev.kind for ev in events}
    if not first_error:
        # bisync's own lock, left by a run that ended abruptly: a notice, not an error line
        if any("prior lock file" in ev.message for ev in events):
            first_error = "prior lock file found"
    if plan.empty and exit_code == 0 and stats and any(stats.get(k) for k in ("transfers", "deletes", "renames")):
        # rclone did (or would do) things the lines above did not say: a run
        # of a newer rclone wording them differently. Never "nothing to do".
        first_error = _("The preview could not be read: rclone counts {t} transfers, {d} deletions, {r} renames") \
            .format(t=count(stats.get("transfers") or 0), d=count(stats.get("deletes") or 0),
                    r=count(stats.get("renames") or 0))
        plan.errors.append(first_error)
        exit_code = 2
    if any("no space left on device" in message.lower() for message in plan.errors):
        first_error = _("A disk of this computer is full: free some space, and the sync goes on")
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
    return Outcome(result, exit_code, first_error, plan, stats, network, safety, empty_side, root_missing,
                   excess=excess)
