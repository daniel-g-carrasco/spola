"""Checks run before a pair is added and before every sync run.

These are the barriers in front of rclone that protect the user's files when
something around the sync is wrong rather than the sync itself: a folder that
should never be synced, an unmounted disk, two pairs on the same tree, Koofr
Desktop still syncing the same folder, names that Koofr would refuse.
Every check returns a list of Problem; an empty list means "go ahead".
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Pair, normalize_remote
from .i18n import _

# Koofr limits (koofr.eu help pages): 255 characters per name, 1023 per path,
# nothing outside the Unicode Basic Multilingual Plane (most emoji).
KOOFR_MAX_NAME = 255
KOOFR_MAX_PATH = 1023

# Inside the home folder, trees that hold the system's and other apps' state.
# A pair rooted in, or containing, one of these could wreck the session.
PROTECTED_IN_HOME = (".var", ".local", ".config", ".cache", ".ssh", ".gnupg",
                     ".pki", ".mozilla", ".steam", ".koofr", ".koofr-dist")

# Outside home, only these trees are acceptable roots (removable media, mounts).
ALLOWED_OUTSIDE_HOME = ("/run/media", "/media", "/mnt")


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    path: str = ""


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _is_within(child: str, parent: str) -> bool:
    child, parent = child.rstrip("/") or "/", parent.rstrip("/") or "/"
    return child == parent or child.startswith(parent + "/") or parent == "/"


def home_dir(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return _real(environ.get("HOME") or os.path.expanduser("~"))


def check_local_root(path: str, home: str | None = None) -> list[Problem]:
    """May this folder be the local side of a pair?"""
    home = _real(home or home_dir())
    if not os.path.isabs(path):
        return [Problem("not-absolute", _("The folder path must be absolute."), path)]
    if os.path.islink(path.rstrip("/") or "/"):
        return [Problem("symlink-root", _("The folder is a symbolic link: choose the folder it points to."), path)]
    real = _real(path)
    if not os.path.isdir(real):
        return [Problem("missing", _("The folder does not exist."), path)]

    if _is_within(home, real):
        # "/" , "/var", "/var/home", the home folder itself...
        return [Problem("too-broad", _("This folder contains your whole home folder: choose a folder inside it."), path)]
    if _is_within(real, home):
        rel = os.path.relpath(real, home).split("/")[0]
        if rel in PROTECTED_IN_HOME:
            return [Problem("protected", _("This folder holds settings and data of the system or of other apps and cannot be synced."), path)]
        return []
    if any(_is_within(real, allowed) and real.rstrip("/") != allowed for allowed in ALLOWED_OUTSIDE_HOME):
        return []
    return [Problem("outside-home", _("Only folders inside your home folder or on removable drives can be synced."), path)]


def check_overlap(local: str, remote: str, cfg: Config, ignore_id: str | None = None) -> list[Problem]:
    """A new pair must not be inside, or contain, another pair, on either side."""
    problems = []
    real = _real(local)
    remote = normalize_remote(remote)
    for other in cfg.pairs:
        if other.id == ignore_id:
            continue
        other_real = _real(other.local)
        if _is_within(real, other_real) or _is_within(other_real, real):
            problems.append(Problem("local-overlap", _("This folder overlaps with the folder of another pair."), other.local))
        if _is_within(remote, other.remote) or _is_within(other.remote, remote):
            problems.append(Problem("remote-overlap", _("This Koofr folder overlaps with the Koofr folder of another pair."), other.remote))
    return problems


def koofr_desktop_syncs(config_path: Path) -> list[dict]:
    """The folder pairs configured in Koofr Desktop (~/.koofr/config.json)."""
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    syncs = []
    for user in (data.get("users") or {}).values():
        for s in user.get("syncs") or []:
            if isinstance(s, dict) and s.get("localPath"):
                syncs.append(s)
    return syncs


KOOFR_DESKTOP_PROCESSES = ("storagegui", "storagesync")


def koofr_desktop_active(autostart_dir: Path | None = None) -> bool:
    """Is Koofr Desktop running, or set to start at the next login? Either
    way it would sync its folders again."""
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    with open(f"/proc/{entry.name}/comm", encoding="utf-8", errors="replace") as f:
                        if f.read().strip() in KOOFR_DESKTOP_PROCESSES:
                            return True
                except OSError:
                    continue
    except OSError:
        pass
    autostart_dir = autostart_dir or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
    return (autostart_dir / "koofr.desktop").exists()


def check_koofr_desktop(local: str, config_path: Path, active: bool | None = None) -> list[Problem]:
    """Two sync engines on the same tree is the quickest way to lose files:
    refuse a folder that Koofr Desktop still syncs (not paused), as long as
    Koofr Desktop runs or is set to start at login."""
    problems = []
    real = _real(local)
    if active is None:
        active = koofr_desktop_active()
    if not active:
        return problems
    for s in koofr_desktop_syncs(config_path):
        if s.get("paused"):
            continue
        theirs = _real(s["localPath"])
        if _is_within(real, theirs) or _is_within(theirs, real):
            problems.append(Problem(
                "koofr-desktop",
                _("Koofr Desktop is syncing this folder. Pause or remove that sync in Koofr Desktop first."),
                s["localPath"]))
    return problems


# A hidden marker in every synced root (excluded from the sync by the
# ".spola-*" rule): an unmounted drive's empty mount point or a folder deleted
# and recreated has none, whatever inode numbers the filesystem reuses.
ROOT_MARKER = ".spola-root"


def marker_path(pair: Pair) -> str:
    return os.path.join(pair.local, ROOT_MARKER)


def write_marker(pair: Pair) -> None:
    with open(marker_path(pair), "w", encoding="utf-8") as f:
        f.write(f"{pair.id}\n")


def check_root_identity(pair: Pair) -> list[Problem]:
    """Before every run: is the local root still the very folder we paired?
    The marker decides; a root paired before markers existed gets one now,
    as long as its device and inode are still the ones recorded."""
    try:
        st = os.stat(pair.local, follow_symlinks=False)
    except FileNotFoundError:
        return [Problem("root-missing", _("The local folder is missing. If it is on a drive, connect it."), pair.local)]
    except OSError as e:
        return [Problem("root-unreadable", _("The local folder cannot be read: {error}").format(error=e.strerror), pair.local)]
    if not os.path.isdir(pair.local) or os.path.islink(pair.local):
        return [Problem("root-not-dir", _("The local path is no longer a folder."), pair.local)]
    replaced = Problem("root-replaced", _("The local folder was replaced by a different one (another drive, or deleted and recreated). Sync is stopped to protect your files."), pair.local)
    try:
        with open(marker_path(pair), encoding="utf-8") as f:
            marker = f.read().strip()
    except FileNotFoundError:
        marker = None
    except OSError as e:
        return [Problem("root-unreadable", _("The local folder cannot be read: {error}").format(error=e.strerror), pair.local)]
    if marker is not None:
        return [] if marker == pair.id else [replaced]
    if pair.root_dev is not None and (st.st_dev, st.st_ino) != (pair.root_dev, pair.root_ino):
        return [replaced]
    try:
        write_marker(pair)
    except OSError as e:
        return [Problem("root-unreadable", _("The local folder cannot be written: {error}").format(error=e.strerror), pair.local)]
    return []


def root_identity(path: str) -> tuple[int, int]:
    st = os.stat(path, follow_symlinks=False)
    return st.st_dev, st.st_ino


def _non_bmp(name: str) -> bool:
    return any(ord(ch) > 0xFFFF for ch in name)


def scan_names(root: str, remote_root: str = "/", limit: int = 200,
               skip=lambda rel: False) -> list[Problem]:
    """Names that Koofr would refuse, and names that collide on Koofr because
    it does not tell upper and lower case apart. One failed upload is a
    critical error for bisync, so these are found before the first run."""
    problems: list[Problem] = []
    remote_prefix = len(normalize_remote(remote_root).rstrip("/"))
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if not skip(os.path.join(rel_dir, d) + "/")]
        seen: dict[str, str] = {}
        for name in dirnames + filenames:
            rel = os.path.join(rel_dir, name)
            if skip(rel):
                continue
            if _non_bmp(name):
                problems.append(Problem("emoji", _("Koofr does not accept emoji and similar characters in names."), rel))
            if len(name) > KOOFR_MAX_NAME:
                problems.append(Problem("long-name", _("The name is longer than the 255 characters Koofr accepts."), rel))
            if remote_prefix + 1 + len(rel) > KOOFR_MAX_PATH:
                problems.append(Problem("long-path", _("The path is longer than the 1023 characters Koofr accepts."), rel))
            key = unicodedata.normalize("NFC", name).casefold()
            if key in seen:
                problems.append(Problem("case-collision", _("Koofr does not tell upper and lower case apart: this name collides with “{other}”.").format(other=seen[key]), rel))
            else:
                seen[key] = name
            if len(problems) >= limit:
                return problems
    return problems


@dataclass(frozen=True)
class Refused:
    path: str        # relative to the pair's root
    is_dir: bool
    code: str        # "emoji", "long-path", "case-collision"


def refused_paths(root: str, remote_root: str = "/", known: frozenset[str] = frozenset(),
                  skip=lambda rel: False, limit: int = 5000) -> list[Refused]:
    """What a run must leave out because Koofr would refuse it. One refused
    upload makes the whole bisync run fail, deletions included, so these are
    excluded from the run and shown to the user instead. A folder with a bad
    name is left out whole. Of names that differ only in case, the one already
    synced (in `known`, the last listing) is kept, otherwise the first in order."""
    refused: list[Refused] = []
    remote_prefix = len(normalize_remote(remote_root).rstrip("/"))
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep_dirs = []
        groups: dict[str, list[tuple[str, bool]]] = {}
        for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
            rel = os.path.join(rel_dir, name)
            if skip(rel + ("/" if is_dir else "")):
                continue
            if _non_bmp(name):
                refused.append(Refused(rel, is_dir, "emoji"))
            elif remote_prefix + 1 + len(rel) > KOOFR_MAX_PATH:
                refused.append(Refused(rel, is_dir, "long-path"))
            else:
                groups.setdefault(unicodedata.normalize("NFC", name).casefold(), []).append((name, is_dir))
                continue
            if len(refused) >= limit:
                return refused
        for members in groups.values():
            members.sort()
            kept = next((m for m in members if os.path.join(rel_dir, m[0]) in known), members[0])
            for name, is_dir in members:
                if (name, is_dir) != kept:
                    refused.append(Refused(os.path.join(rel_dir, name), is_dir, "case-collision"))
                elif is_dir:
                    keep_dirs.append(name)
        dirnames[:] = [d for d in dirnames if d in keep_dirs]
    return refused


def refused_reason(code: str) -> str:
    return {
        "emoji": _("Koofr does not accept emoji and similar characters in names."),
        "long-path": _("The path is longer than the 1023 characters Koofr accepts."),
        "case-collision": _("Another name in the same folder differs only in upper and lower case, and Koofr does not tell them apart."),
    }.get(code, code)
