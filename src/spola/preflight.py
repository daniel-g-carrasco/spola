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
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, Pair, normalize_remote
from .i18n import _
from .places import remote_within, within

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


def home_dir(environ=None) -> str:
    environ = os.environ if environ is None else environ
    if os.name == "nt":
        # the profile: HOME, when set on Windows, is some tool's own (MSYS2,
        # Git, an editor), as Python itself holds since 3.8
        return _real(environ.get("USERPROFILE") or os.path.expanduser("~"))
    return _real(environ.get("HOME") or os.path.expanduser("~"))


# Windows: in the profile, what belongs to the system and to the apps
PROTECTED_IN_PROFILE = ("appdata", ".koofr", ".ssh", ".config", ".cache")


def _check_windows_root(real: str, home: str, path: str) -> list[Problem]:
    """The same rules as on Linux: inside the profile but not its app data,
    or on another drive than Windows' own (not a drive's root)."""
    if within(home, real):
        return [Problem("too-broad", _("This folder contains your whole home folder: choose a folder inside it."), path)]
    if within(real, home):
        first = os.path.relpath(real, home).replace("\\", "/").split("/")[0].casefold()
        if first in PROTECTED_IN_PROFILE:
            return [Problem("protected", _("This folder holds settings and data of the system or of other apps and cannot be synced."), path)]
        return []
    drive, rest = os.path.splitdrive(real)
    system = os.environ.get("SystemDrive", "C:")
    if drive and drive.casefold() != system.casefold() and rest.strip("\\/"):
        return []
    return [Problem("outside-home", _("Only folders inside your home folder or on removable drives can be synced."), path)]


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

    if os.name == "nt":
        return _check_windows_root(real, home, path)
    if within(home, real):
        # "/" , "/var", "/var/home", the home folder itself...
        return [Problem("too-broad", _("This folder contains your whole home folder: choose a folder inside it."), path)]
    if within(real, home):
        rel = os.path.relpath(real, home).split("/")[0]
        if rel in PROTECTED_IN_HOME:
            return [Problem("protected", _("This folder holds settings and data of the system or of other apps and cannot be synced."), path)]
        return []
    if any(within(real, allowed) and real.rstrip("/") != allowed for allowed in ALLOWED_OUTSIDE_HOME):
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
        if within(real, other_real) or within(other_real, real):
            problems.append(Problem("local-overlap", _("This folder overlaps with the folder of another pair."), other.local))
        if remote_within(remote, other.remote) or remote_within(other.remote, remote):     # Koofr ignores case
            problems.append(Problem("remote-overlap", _("This Koofr folder overlaps with the Koofr folder of another pair."), other.remote))
    return problems


def koofr_desktop_dir() -> Path:
    """Where Koofr Desktop keeps its config.json and its logs: ~/.koofr, on
    Windows %APPDATA%\\koofr (its programs are in %LOCALAPPDATA%\\koofr)."""
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "koofr"
    return Path.home() / ".koofr"


def koofr_desktop_config() -> Path:
    return koofr_desktop_dir() / "config.json"


def koofr_desktop_syncs(config_path: Path) -> list[dict]:
    """The folder pairs configured in Koofr Desktop (its config.json): each
    with localPath, remotePath, mountId, paused."""
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


KOOFR_DESKTOP_LOG_FRESH_S = 15 * 60      # Koofr Desktop writes its sync logs all the time while it runs


def _host_config_dir() -> Path:
    """The host's ~/.config: inside the Flatpak XDG_CONFIG_HOME is the
    sandbox's own folder."""
    if os.environ.get("FLATPAK_ID"):
        base = os.environ.get("HOST_XDG_CONFIG_HOME")
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base and os.path.isabs(base) else Path.home() / ".config"


def _autostart_starts_koofr(autostart_dir: Path) -> bool:
    """An autostart entry of Koofr Desktop, under any name: systemd's
    xdg-autostart generator (niri and the other sessions without
    gnome-session) runs every file in the folder, also a renamed one.
    Hidden=true is the way the specification turns one off."""
    try:
        entries = list(autostart_dir.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "storagegui" in text and "\nHidden=true" not in "\n" + text.replace("\r", ""):
            return True
    return False


@dataclass(frozen=True)
class KoofrDesktopState:
    running: bool
    at_login: bool          # set to start at the next login

    @property
    def active(self) -> bool:
        return self.running or self.at_login


# Koofr Desktop's programs, by name (storagegui.exe, in %LOCALAPPDATA%\koofr):
# never any "koofr" in the text, which a user profile called so would put in
# every entry, Spola's own too
_KOOFR_PROGRAM = re.compile(r"(?:storagegui|storagesync)(?:\.exe)?|koofr\.exe", re.IGNORECASE)


def _starts_koofr_desktop(text: str) -> bool:
    """A login entry (a Run value, a shortcut's text) that starts one of
    Koofr Desktop's programs."""
    return any(_KOOFR_PROGRAM.fullmatch(name) for name in re.split(r'[\\/"\s\x00]+', text))


def koofr_desktop_state(autostart_dir: Path | None = None, koofr_dir: Path | None = None,
                        now: float | None = None) -> KoofrDesktopState:
    """Is Koofr Desktop running, and is it set to start at the next login?
    Inside the Flatpak the host's processes cannot be seen: its logs,
    written while it runs, tell."""
    if os.name == "nt":
        # Windows: its programs (storagegui, storagesync... or any "koofr")
        # and what starts at login and is not turned off (its installer puts
        # Koofr.lnk in the Startup folder). Not its logs: the processes are
        # in sight, and server.log, written as it quits, would keep it
        # "running" for a quarter of an hour after.
        from . import win32
        names = win32.process_names()
        running = bool(names & set(KOOFR_DESKTOP_PROCESSES)) or any("koofr" in name for name in names)
        return KoofrDesktopState(running, any(_starts_koofr_desktop(line) for line in win32.login_commands()))
    running = False
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    with open(f"/proc/{entry.name}/comm", encoding="utf-8", errors="replace") as f:
                        if f.read().strip() in KOOFR_DESKTOP_PROCESSES:
                            running = True
                            break
                except OSError:
                    continue
    except OSError:
        pass
    at_login = _autostart_starts_koofr(autostart_dir or _host_config_dir() / "autostart")
    return KoofrDesktopState(running or _fresh_koofr_logs(koofr_dir, now), at_login)


def koofr_desktop_active(autostart_dir: Path | None = None, koofr_dir: Path | None = None,
                         now: float | None = None) -> bool:
    """Is Koofr Desktop running, or set to start at the next login? Either
    way it would sync its folders again."""
    return koofr_desktop_state(autostart_dir, koofr_dir, now).active


# Turning off Koofr Desktop's start at login is Windows' Task Manager's own
# reversible switch; on Linux the user does it (the Flatpak cannot write the
# host's autostart folder)
CAN_TURN_OFF_KOOFR_DESKTOP_LOGIN = os.name == "nt"


def turn_off_koofr_desktop_login() -> int:
    """Windows: turn off what starts Koofr Desktop at login, as Task Manager
    does. How many entries; PermissionError for one of the machine's."""
    from . import win32
    entries = [e for e in win32.login_entries() if _starts_koofr_desktop(e.text)]
    for entry in entries:
        win32.turn_off(entry)
    return len(entries)


def _fresh_koofr_logs(koofr_dir: Path | None, now: float | None) -> bool:
    now = time.time() if now is None else now
    try:
        for entry in os.scandir(koofr_dir or koofr_desktop_dir() / "log"):
            if entry.name.endswith(".log") and now - entry.stat().st_mtime < KOOFR_DESKTOP_LOG_FRESH_S:
                return True
    except OSError:
        pass
    return False


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
        if within(real, theirs) or within(theirs, real):
            problems.append(Problem(
                "koofr-desktop",
                _("Koofr Desktop is syncing this folder too: two sync engines on the same files lose files. "
                  "Quit Koofr Desktop and remove it from the programs started at login, or remove this "
                  "folder from it."),
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


def claim_root(pair: Pair) -> None:
    """A new pair marks its folder at once: the marker of a pair removed from
    the same folder before would say the folder had been replaced. (One of a
    pair still configured cannot be there: two pairs on a folder are refused.)"""
    try:
        write_marker(pair)
    except OSError:
        pass            # the check before the first run says why


def release_root(pair: Pair) -> None:
    """A pair removed takes its marker away, if the marker is its own."""
    try:
        with open(marker_path(pair), encoding="utf-8") as f:
            if f.read().strip() != pair.id:
                return
        os.remove(marker_path(pair))
    except OSError:
        pass


def check_root_identity(pair: Pair, known_entries: Iterable[str] = ()) -> list[Problem]:
    """Before every run: is the local root still the very folder we paired?
    The marker decides. A root paired before markers existed gets one now,
    if its device and inode are still the ones recorded and, when the last
    listing is known, at least one of its entries is there: a folder
    recreated empty in the same place can get the same inode number back."""
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
    known = list(known_entries)
    if known and not any(os.path.lexists(os.path.join(pair.local, rel)) for rel in known):
        return [replaced]
    try:
        write_marker(pair)
    except OSError as e:
        return [Problem("root-unreadable", _("The local folder cannot be written: {error}").format(error=e.strerror), pair.local)]
    return []


@dataclass(frozen=True)
class CaseRename:
    """A name changed here in its case alone: "Screencasts" to "screencasts"."""
    old: str        # in the last listing of this computer
    new: str        # here now
    koofr: str      # on Koofr now


def case_renames(root: str, before: Iterable[str], koofr: Iterable[str]) -> list[CaseRename]:
    """What was renamed here by case alone since the last run. Koofr does not
    tell "Screencasts" from "screencasts", and bisync cannot rename one into
    the other there: it plans deletions and uploads that do nothing, the
    files moved into the folder land in the old name, and the next run moves
    them here into a second folder and stops for a repair. Renamed on Koofr
    first, on the server, the two sides agree before bisync looks. In order:
    a folder before what it holds, each with its name at that moment."""
    on_koofr: dict[str, str] = {}
    for path in koofr:
        on_koofr.setdefault(path.casefold(), path)
    listed: dict[str, dict[str, list[str]] | None] = {}

    def names_in(parent: str) -> dict[str, list[str]] | None:
        if parent not in listed:
            try:
                found: dict[str, list[str]] = {}
                for name in os.listdir(os.path.join(root, parent)):
                    found.setdefault(name.casefold(), []).append(name)
                listed[parent] = found
            except OSError:
                listed[parent] = None
        return listed[parent]

    def now(path: str, moved: list[tuple[str, str]]) -> str:
        """The path after the folders renamed so far (the deepest first)."""
        for old, new in moved:
            if path == old or path.startswith(old + "/"):
                return new + path[len(old):]
        return path

    known = set(before)
    renamed: list[CaseRename] = []
    here_moved: list[tuple[str, str]] = []      # (before, now) of the folders renamed, deepest first
    koofr_moved: list[tuple[str, str]] = []
    for old in sorted(known, key=lambda p: (p.count("/"), p)):
        parent, _sep, name = old.rpartition("/")
        parent_now = now(parent, here_moved) if parent else ""
        found = names_in(parent_now)
        variants = found.get(name.casefold(), []) if found is not None else []
        if name in variants or len(variants) != 1:
            continue        # still here, gone, or two names to choose from
        new = f"{parent_now}/{variants[0]}" if parent_now else variants[0]
        there = on_koofr.get(old.casefold())
        if new in known or there is None:
            continue        # both names were here, or it is not on Koofr
        there = now(there, koofr_moved)
        koofr_parent, _sep, koofr_name = there.rpartition("/")
        if koofr_name == variants[0]:
            continue        # Koofr has that name already
        renamed.append(CaseRename(old, new, there))
        here_moved.insert(0, (old, new))
        koofr_moved.insert(0, (there, f"{koofr_parent}/{variants[0]}" if koofr_parent else variants[0]))
    return renamed


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
        # an NTFS junction is never synced (rclone --skip-links): not looked into either
        dirnames[:] = [d for d in dirnames if not os.path.isjunction(os.path.join(dirpath, d))]
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
    other: str = ""  # case-collision: the name synced in its place


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
        # an NTFS junction is never synced (rclone --skip-links): its names are none of Koofr's business
        dirnames[:] = [d for d in dirnames if not os.path.isjunction(os.path.join(dirpath, d))]
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
                    refused.append(Refused(os.path.join(rel_dir, name), is_dir, "case-collision", kept[0]))
                elif is_dir:
                    keep_dirs.append(name)
        dirnames[:] = [d for d in dirnames if d in keep_dirs]
    return refused


# what is left of an emoji once the emoji itself is gone: the joiner, the
# variation selectors, the keycap
_EMOJI_GLUE = {"\u200d", "\ufe0e", "\ufe0f", "\u20e3"}


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _clean(text: str) -> str:
    return " ".join("".join(ch for ch in text if ord(ch) <= 0xFFFF and ch not in _EMOJI_GLUE).split())


def _without_emoji(name: str, is_dir: bool) -> str:
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    stem, ext = _clean(stem), _clean(ext)
    return (stem or _("Untitled")) + ("" if ext == "." else ext)


def _unique(name: str, is_dir: bool, taken: set[str]) -> str:
    """`name`, or "name (2).ext", "name (3).ext"... if a name in the same
    folder already matches it, upper and lower case aside (as on Koofr)."""
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    candidate, n = name, 2
    while _name_key(candidate) in taken:
        candidate, n = f"{stem} ({n}){ext}", n + 1
    taken.add(_name_key(candidate))
    return candidate


def rename_plan(root: str, refused: list[Refused], remote_root: str = "/") -> list[tuple[Refused, str | None]]:
    """A name Koofr accepts for each file or folder it refuses, as a path
    relative to `root`, or None when there is none to suggest: a path too
    long is for the user to shorten. Emoji are taken out of the name; of two
    names that differ only in case, the one left out becomes "name (2)"."""
    prefix = len(normalize_remote(remote_root).rstrip("/"))
    taken: dict[str, set[str] | None] = {}
    plan: list[tuple[Refused, str | None]] = []
    for item in refused:
        directory, name = os.path.split(item.path)
        if item.code not in ("emoji", "case-collision"):
            plan.append((item, None))
            continue
        if directory not in taken:
            try:
                taken[directory] = {_name_key(n) for n in os.listdir(os.path.join(root, directory))}
            except OSError:
                taken[directory] = None
        if taken[directory] is None:
            plan.append((item, None))
            continue
        new = _without_emoji(name, item.is_dir) if item.code == "emoji" else name
        new = _unique(new, item.is_dir, taken[directory])
        rel = os.path.join(directory, new)
        fits = len(new) <= KOOFR_MAX_NAME and prefix + 1 + len(rel) <= KOOFR_MAX_PATH
        plan.append((item, rel if fits else None))
    return plan


def refused_reason(code: str, other: str = "") -> str:
    if code == "case-collision" and other:
        return _("Koofr does not tell upper and lower case apart: “{other}” is synced in its place.").format(other=other)
    return {
        "emoji": _("Koofr does not accept emoji and similar characters in names."),
        "long-path": _("The path is longer than the 1023 characters Koofr accepts."),
        "case-collision": _("Another name in the same folder differs only in upper and lower case, and Koofr does not tell them apart."),
    }.get(code, code)
