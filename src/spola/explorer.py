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


def applies_to(pairs: list[Pair]) -> str:
    """The Advanced Query Syntax condition: an item in a synced folder, or
    the synced folder itself."""
    terms = []
    for pair in pairs:
        root = os.path.normpath(pair.local).replace("/", "\\").rstrip("\\")
        terms.append(f'System.ItemPathDisplay:~<"{root}\\"')
        terms.append(f'System.ItemPathDisplay:="{root}"')
    return " OR ".join(terms)


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
