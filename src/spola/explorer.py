"""Spola in File Explorer: "Open on Koofr" and "Copy Koofr Link" on the
files and folders of the synced folders (Windows; on Linux the file
managers' extensions do it, over D-Bus).

The two entries are verbs of the user's registry classes, shown only where
they apply: their AppliesTo condition lists the synced folders, and Spola
writes it again whenever the pairs change. A click starts
`Spola.exe --open-on-koofr <path>` (or --copy-link), which the running
Spola receives (one instance). Setup removes them with Spola.
"""

from __future__ import annotations

import os
import posixpath

from .config import Pair
from .i18n import _
from .koofr_api import web_url

VERBS = ("Spola.OpenOnKoofr", "Spola.CopyKoofrLink")
CLASSES = r"Software\Classes"
TARGETS = ("*", "Directory")        # files, folders (not a folder's background: AppliesTo is not read there)


def _slashes(path: str) -> str:
    return os.path.normpath(path).replace("\\", "/").rstrip("/")


def _key(path: str) -> str:
    return path.casefold() if os.name == "nt" else path


def locate(pairs: list[Pair], path: str) -> tuple[Pair, str] | None:
    """(pair, path relative to its root, with "/") of a path inside a pair;
    on Windows whatever the case and the separators."""
    wanted = _slashes(path)
    for pair in pairs:
        root = _slashes(pair.local)
        if _key(wanted) == _key(root):
            return pair, ""
        if _key(wanted).startswith(_key(root) + "/"):
            return pair, wanted[len(root) + 1:].strip("/")
    return None


def remote_path(pair: Pair, relative: str) -> str:
    return posixpath.join(pair.remote, relative) if relative else pair.remote


def page_url(mount_id: str, pair: Pair, relative: str, is_dir: bool) -> str:
    """Koofr's web page of the item: the folder itself, or the one that
    holds the file."""
    remote = remote_path(pair, relative)
    if relative and not is_dir:
        remote = posixpath.dirname(remote) or "/"
    return web_url(mount_id, remote)


def link_refusal(pair: Pair, relative: str, is_dir: bool, excluded) -> str:
    """Why an item has no share link to make ("" when it may have one):
    Spola never syncs it (its own marker, an exclusion). Anything else is
    asked of Koofr, which may have it even before the first alignment."""
    name = posixpath.basename(relative) or pair.name
    if relative and excluded(relative + ("/" if is_dir else "")):
        return _("“{name}” is not synced with Koofr: it has no share link.").format(name=name)
    return ""


def link_error(name: str, stderr: str) -> str:
    """What to tell when rclone link fails: not on Koofr (yet), in words;
    anything else, its last line."""
    text = stderr.lower()
    if "not found" in text or "got 400" in text or "got 404" in text:
        return _("“{name}” is not on Koofr yet: it will be after the next sync.").format(name=name)
    last = (stderr.strip().splitlines() or ["rclone link failed"])[-1]
    return _("The link could not be made: {error}").format(error=last[:200])


def applies_to(pairs: list[Pair]) -> str:
    """The Advanced Query Syntax condition: an item in a synced folder, or
    the synced folder itself. System.ParsingPath: the context menu has no
    System.ItemPathDisplay to compare (tried: never matched); the
    comparison ignores case, as NTFS does."""
    terms = []
    for pair in pairs:
        root = os.path.normpath(pair.local).replace("/", "\\").rstrip("\\")
        terms.append(f'System.ParsingPath:~<"{root}\\"')
        terms.append(f'System.ParsingPath:="{root}"')
    # not on Spola's own files (".spola-root"), which never go to Koofr
    return f'({" OR ".join(terms)}) AND NOT System.FileName:~<".spola-"'


def verbs(exe: str) -> list[tuple[str, str, str]]:
    """(verb key, text, command) of the two entries."""
    return [
        (VERBS[0], _("Open on Koofr"), f'"{exe}" --open-on-koofr "%1"'),
        (VERBS[1], _("Copy Koofr Share Link"), f'"{exe}" --copy-link "%1"'),
    ]


def register(exe: str, pairs: list[Pair]) -> None:
    """The entries for these pairs (none without pairs)."""
    import winreg
    unregister()
    if not pairs:
        return
    # with backslashes: Explorer cannot start "C:/.../Spola.exe", which is
    # how MSYS2's Python, the Windows build's, writes sys.executable
    exe = os.path.normpath(exe).replace("/", "\\")
    condition = applies_to(pairs)
    for target in TARGETS:
        for key_name, text, command in verbs(exe):
            path = f"{CLASSES}\\{target}\\shell\\{key_name}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, text)
                winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, f'"{exe}",0')
                winreg.SetValueEx(key, "AppliesTo", 0, winreg.REG_SZ, condition)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + "\\command") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def unregister() -> None:
    import winreg
    for target in TARGETS:
        for key_name in VERBS:
            path = f"{CLASSES}\\{target}\\shell\\{key_name}"
            for sub in (path + "\\command", path):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass
