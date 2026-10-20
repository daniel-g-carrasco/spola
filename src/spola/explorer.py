"""Spola in File Explorer: "Open on Koofr" and "Copy Koofr Link" on the
files and folders of the synced folders, "Previous Versions…" on the files
and "Recently Deleted Files…" on the folders (Windows; on Linux the file
managers' extensions do it, over D-Bus).

The entries are verbs of the user's registry classes, shown only where
they apply: their AppliesTo condition lists the synced folders, and Spola
writes it again whenever the pairs change. A click starts
`Spola.exe --open-on-koofr <path>` (or --copy-link, --versions,
--deleted), which the running Spola receives (one instance). Setup removes
them with Spola.
"""

from __future__ import annotations

import os
import posixpath

from .config import Pair
from .i18n import _
from .koofr_api import web_url

VERBS = ("Spola.OpenOnKoofr", "Spola.CopyKoofrLink", "Spola.Versions", "Spola.Deleted")
CLASSES = r"Software\Classes"
TARGETS = ("*", "Directory")        # files, folders (not a folder's background: AppliesTo is not read there)


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


def verbs(exe: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(verb key, text, command, targets) of the entries. "Previous
    Versions…" is on every file: the condition cannot tell which have a
    kept copy (on Linux it shows on those only), and the window says when
    there is none."""
    return [
        (VERBS[0], _("Open on Koofr"), f'"{exe}" --open-on-koofr "%1"', TARGETS),
        (VERBS[1], _("Copy Koofr Share Link"), f'"{exe}" --copy-link "%1"', TARGETS),
        (VERBS[2], _("Previous Versions…"), f'"{exe}" --versions "%1"', ("*",)),
        (VERBS[3], _("Recently Deleted Files…"), f'"{exe}" --deleted "%1"', ("Directory",)),
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
    for key_name, text, command, targets in verbs(exe):
        for target in targets:
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
