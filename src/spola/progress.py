"""How far a run is, in a few words, from rclone's stats: the copies, then
the deletions and the folders removed. No GTK here (GLib only, for sizes)."""

from __future__ import annotations

from gi.repository import GLib

from .i18n import _, count, ngettext


def progress_text(stats: dict | None, deletes_queued: int = 0) -> tuple[str, float | None]:
    """A short line for the row and the fraction for its bar (None while
    the two sides are still being compared, or while empty folders go).
    `deletes_queued` is how many deletions bisync announced for the run.

    Once the copies are done a run may still delete thousands of files on
    Koofr, one call each, then remove the folders left empty: the line says
    so, and the bar moves again, instead of showing "1,937 of 1,937 files"
    for ten minutes."""
    if not stats:
        return "", None
    total, done = stats.get("totalBytes") or 0, stats.get("bytes") or 0
    files, total_files = stats.get("transfers") or 0, stats.get("totalTransfers") or 0
    deleted, folders = stats.get("deletes") or 0, stats.get("deletedDirs") or 0
    copying = (total_files and files < total_files) or (total and done < total)
    if not copying:
        if folders:
            return _("Removing empty folders: {n}").format(n=count(folders)), None
        if deletes_queued and deleted:
            out_of = max(deletes_queued, deleted)
            return (_("Deleting: {done} of {total}").format(done=count(deleted), total=count(out_of)),
                    deleted / out_of)
        if deleted:
            return _("Deleting: {n}").format(n=count(deleted)), None
    if not total_files and not total:
        checks = stats.get("checks") or 0
        return (_("Comparing files ({n} checked)").format(n=count(checks)) if checks else ""), None
    text = ngettext("{done} of {total} file", "{done} of {total} files", total_files).format(
        done=count(files), total=count(total_files))
    speed = stats.get("speed") or 0
    if speed:
        text += " · " + _("{speed}/s").format(speed=GLib.format_size(int(speed)))
    fraction = done / total if total else (files / total_files if total_files else 0.0)
    return text, max(0.0, min(1.0, fraction))


def time_left(seconds) -> str:
    """rclone's estimate, said the way people say it."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return _("less than a minute left")
    if seconds < 3600:
        n = round(seconds / 60)
        return ngettext("about {n} minute left", "about {n} minutes left", n).format(n=n)
    n = round(seconds / 3600)
    return ngettext("about {n} hour left", "about {n} hours left", n).format(n=n)


