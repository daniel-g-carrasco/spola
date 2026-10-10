"""Conflicts: a file changed on both sides since the last run. bisync keeps
both versions, on both sides, as two copies next to each other:

    report.conflict-local-2026-10-10-1.odt      this computer's version
    report.conflict-koofr-2026-10-10-1.odt      Koofr's version
    (first versions of Spola: report.conflict1.odt / report.conflict2.odt)

This module pairs the copies up and resolves a conflict with local file
operations only (rename, move to the wastebasket): the next run carries the
outcome to Koofr, where the discarded copy lands in Koofr's trash. Nothing is
ever deleted outright.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

from .i18n import _, every_translation

DATE = r"\d{4}-\d{2}-\d{2}"


@dataclass(frozen=True)
class Copy:
    path: str          # relative to the pair's root
    size: int
    mtime: float


@dataclass(frozen=True)
class Conflict:
    original: str      # relative path the file had, e.g. "a/report.odt"
    date: str          # "2026-10-10", or "" for legacy names
    local: Copy | None
    koofr: Copy | None

    @property
    def copies(self) -> list[Copy]:
        return [c for c in (self.local, self.koofr) if c is not None]


def _pattern() -> re.Pattern:
    local_words = every_translation("conflict-local")
    koofr_words = every_translation("conflict-koofr")
    words = "|".join(re.escape(w) for w in sorted(local_words | koofr_words))
    return re.compile(
        rf"^(?P<stem>.*)\.(?:(?P<legacy>conflict[12])|(?P<word>{words})-(?P<date>{DATE})-(?P<n>\d+))"
        rf"(?P<ext>\.[^/.]*)?$")


def group(paths: list[str], root: str) -> list[Conflict]:
    """Pair the conflict copies found in a listing (relative paths) up by
    original name. `root` is read for sizes and dates; missing copies (the
    user already removed one) are kept with what is left."""
    pattern = _pattern()
    local_words = every_translation("conflict-local")
    found: dict[tuple, dict] = {}
    for path in paths:
        directory, name = os.path.split(path)
        m = pattern.match(name)
        if not m:
            continue
        ext = m.group("ext") or ""
        original = os.path.join(directory, m.group("stem") + ext)
        if m.group("legacy"):
            side, key, date = ("local" if m.group("legacy") == "conflict1" else "koofr"), (original, "legacy"), ""
        else:
            side = "local" if m.group("word") in local_words else "koofr"
            date = m.group("date")
            key = (original, f"{date}-{m.group('n')}")
        entry = found.setdefault(key, {"original": original, "date": date})
        try:
            st = os.stat(os.path.join(root, path))
            entry[side] = Copy(path, st.st_size, st.st_mtime)
        except OSError:
            continue
    out = [Conflict(e["original"], e["date"], e.get("local"), e.get("koofr")) for e in found.values()]
    return sorted((c for c in out if c.copies), key=lambda c: (c.original.casefold(), c.date))


class ResolveError(Exception):
    pass


def _free_name(root: str, rel: str) -> str:
    """`rel` if nothing is there, else "name (2).ext", "name (3).ext"..."""
    if not os.path.lexists(os.path.join(root, rel)):
        return rel
    stem, ext = os.path.splitext(rel)
    for i in range(2, 1000):
        candidate = f"{stem} ({i}){ext}"
        if not os.path.lexists(os.path.join(root, candidate)):
            return candidate
    raise ResolveError(_("Too many files with that name."))


def _trash_with_gio(path: str) -> None:
    from gi.repository import Gio
    Gio.File.new_for_path(path).trash(None)


def resolve(root: str, conflict: Conflict, choice: str,
            trash: Callable[[str], None] = _trash_with_gio) -> list[tuple[str, str]]:
    """Apply the user's choice: "local" or "koofr" keeps that copy under the
    original name and sends the other to the wastebasket; "both" keeps both
    under readable names. Returns the renames done as (old, new)."""
    if choice not in ("local", "koofr", "both"):
        raise ValueError(choice)
    renames: list[tuple[str, str]] = []

    def rename(copy: Copy, new_rel: str) -> None:
        src, dst = os.path.join(root, copy.path), os.path.join(root, new_rel)
        if not os.path.lexists(src):
            raise ResolveError(_("That version is no longer there."))
        if os.path.lexists(dst):
            raise ResolveError(_("A file named “{name}” exists now: rename it first.").format(name=new_rel))
        os.replace(src, dst)
        renames.append((copy.path, new_rel))

    if choice == "both":
        stem, ext = os.path.splitext(conflict.original)
        date = conflict.date or ""
        if conflict.local:
            rename(conflict.local, _free_name(root, _("{stem} (this computer {date}){ext}")
                                              .format(stem=stem, date=date, ext=ext).replace("  ", " ").replace(" )", ")")))
        if conflict.koofr:
            rename(conflict.koofr, _free_name(root, _("{stem} (Koofr {date}){ext}")
                                              .format(stem=stem, date=date, ext=ext).replace("  ", " ").replace(" )", ")")))
        return renames

    keep, drop = (conflict.local, conflict.koofr) if choice == "local" else (conflict.koofr, conflict.local)
    if keep is None:
        raise ResolveError(_("That version is no longer there."))
    rename(keep, conflict.original)
    if drop is not None and os.path.lexists(os.path.join(root, drop.path)):
        trash(os.path.join(root, drop.path))
    return renames
