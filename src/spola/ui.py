"""What the window, its pages and dialogs share: status names and icons,
the one-line status of a pair, paths shown the GNOME way, threads."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, conflicts  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .paths import nearest_folder  # noqa: E402
from .progress import progress_text, time_left  # noqa: E402,F401 (used by the window and the tray)
from .rclone import Result, describe  # noqa: E402

STATUS_ICONS = {
    Status.OK: "-synced-symbolic",
    Status.SYNCING: "-syncing-symbolic",
    Status.PENDING: "-syncing-symbolic",
    Status.PAUSED: "-paused-symbolic",
    Status.NEEDS_SETUP: "-paused-symbolic",
    Status.ERROR: "-error-symbolic",
    Status.NEEDS_REPAIR: "-error-symbolic",
    Status.NEEDS_CONFIRMATION: "-attention-symbolic",
    Status.CONFLICTS: "-attention-symbolic",
    Status.SKIPPED: "-attention-symbolic",
}

# what the user has to look at before the pair can go on (SKIPPED goes on:
# only the files Koofr refuses are left out)
NEEDS_LOOK = (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.NEEDS_SETUP, Status.CONFLICTS)
ATTENTION = (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED)


def status_icon(status: Status) -> str:
    return APP_ID + STATUS_ICONS[status]


BADGES = {
    Status.OK: "synced", Status.SYNCING: "syncing", Status.PENDING: "syncing", Status.PAUSED: "paused",
    Status.NEEDS_SETUP: "attention", Status.NEEDS_CONFIRMATION: "attention", Status.CONFLICTS: "attention",
    Status.SKIPPED: "attention", Status.ERROR: "error", Status.NEEDS_REPAIR: "error",
}


def status_badge(status: Status) -> str:
    """The full-colour version: blue while syncing, orange to look at, red
    for errors, green and grey for up to date and paused."""
    return f"{APP_ID}-status-{BADGES[status]}"


def tray_icon(status: Status) -> str:
    """Quiet when there is no news: up to date and paused keep the panel's
    own colour (symbolic icons); syncing, attention and errors get theirs."""
    return status_icon(status) if status in (Status.OK, Status.PAUSED) else status_badge(status)


def status_css(status: Status) -> list[str]:
    """Colour only where something is wrong: a list of green ticks says less
    than a quiet list with one orange icon."""
    if status in (Status.ERROR, Status.NEEDS_REPAIR):
        return ["error"]
    if status in (Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED, Status.NEEDS_SETUP):
        return ["warning"]
    return ["dim-label"]


def status_text(status: Status) -> str:
    return {
        Status.OK: _("Up to date"),
        Status.SYNCING: _("Syncing"),
        Status.PENDING: _("Changes waiting to be synced"),
        Status.PAUSED: _("Paused"),
        Status.NEEDS_SETUP: _("First alignment to confirm"),
        Status.ERROR: _("Error"),
        Status.NEEDS_REPAIR: _("Needs repair"),
        Status.NEEDS_CONFIRMATION: _("Waiting for your confirmation"),
        Status.CONFLICTS: _("Conflicts to check"),
        Status.SKIPPED: _("Some files cannot be synced"),
    }[status]


def pair_status_text(engine: Engine, pair_id: str) -> str:
    """The status of one pair: a run the user confirmed, waiting its turn,
    says so (not just "changes waiting")."""
    rt = engine.runtime.get(pair_id)
    if rt is None:
        return ""
    if rt.status is Status.PENDING and engine.confirmed_waiting(pair_id):
        return _("Confirmed")
    return status_text(rt.status)


def when(timestamp: float) -> str:
    """10:42 today, 9 Oct otherwise."""
    moment = GLib.DateTime.new_from_unix_local(int(timestamp))
    today = GLib.DateTime.new_now_local()
    same_day = (moment.get_year(), moment.get_day_of_year()) == (today.get_year(), today.get_day_of_year())
    return moment.format("%H:%M" if same_day else "%-d %b")


def syncing_for_real(engine: Engine, pair_id: str) -> bool:
    rt = engine.runtime.get(pair_id)
    return bool(rt and rt.status is Status.SYNCING and not (rt.task and rt.task.dry_run))


def status_detail(engine: Engine, pair_id: str, short: bool) -> str:
    """What goes after the status: the progress, how many conflicts, when
    the last sync was. `short` is for the list (one line), else the pair page."""
    rt = engine.runtime.get(pair_id)
    if rt is None:
        return ""
    if rt.status is Status.SYNCING:
        if syncing_for_real(engine, pair_id):
            return progress_text(rt.stats, rt.activity.deletes_queued)[0]
        return rt.message
    if rt.status is Status.PENDING and engine.confirmed_waiting(pair_id):
        return _("starts after the sync in progress") if short else \
            _("It starts as soon as the sync in progress is done.")
    if rt.status is Status.CONFLICTS:
        n = len(engine.conflicts(pair_id))           # conflicts, not copies
        return ngettext("{n} file", "{n} files", n).format(n=n)
    if rt.status is Status.SKIPPED:
        n = len(rt.refused)
        return ngettext("{n} file left out", "{n} files left out", n).format(n=n)
    if rt.status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
        # what the review will show, once its preview is ready (prepared ahead)
        base = rt.message if rt.message != status_text(rt.status) else ""
        return " · ".join(part for part in (base, preview_summary(rt.preview)) if part)
    if rt.status is Status.OK and rt.last_sync:
        if short:
            return when(rt.last_sync)
        line = _("Last sync: {time}").format(time=when(rt.last_sync))
        summary = describe(rt.last_outcome.plan) if rt.last_outcome and rt.last_outcome.result is Result.OK else ""
        return f"{line}: {summary}" if summary else line
    if rt.message and rt.message != status_text(rt.status):
        return rt.message
    return ""


def preview_summary(preview) -> str:
    """A kept preview in a few words: being prepared, or what it found."""
    if preview is None:
        return ""
    if preview.running:
        return _("preparing the preview…")
    if not preview.fresh():
        return ""
    plan = preview.outcome.plan
    if plan.empty:
        return _("nothing to do")
    parts = [describe(plan)]
    size = plan.upload_bytes + plan.download_bytes
    if size:
        parts.append(GLib.format_size(size))
    if preview.free_bytes is not None and plan.upload_bytes > preview.free_bytes:
        parts.append(_("not enough space on Koofr"))
    return " · ".join(parts)


def status_line(engine: Engine, pair_id: str) -> str:
    rt = engine.runtime.get(pair_id)
    if rt is None:
        return ""
    detail = status_detail(engine, pair_id, short=True)
    return pair_status_text(engine, pair_id) + (" · " + detail if detail else "")


def display_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def place_name(folder: str) -> str:
    """Where a synced folder lives, for a group title: the home folder by
    its name (a lone "~" says nothing), anything else as ~/path."""
    homes = {str(Path.home()), os.path.realpath(Path.home())}
    return _("Home Folder") if folder in homes else display_path(folder)


def reveal(window, path: str) -> None:
    """Show the file in the file manager, selected (the OpenURI portal's
    OpenDirectory inside the sandbox); a file that is not there any more
    opens the nearest folder that is."""
    if os.path.lexists(path):
        Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(window, None, None, None)
        return
    Gtk.FileLauncher.new(Gio.File.new_for_path(nearest_folder(path))).launch(window, None, None, None)


def file_details(copy: conflicts.Copy) -> str:
    moment = GLib.DateTime.new_from_unix_local(int(copy.mtime)).format("%d %b %H:%M")
    return f"{GLib.format_size(copy.size)} · {moment}"


def run_in_thread(work, done) -> None:
    """work() in a thread, done(result, error) back on the main loop."""
    def target():
        try:
            result, error = work(), None
        except Exception as e:  # shown to the user, never swallowed
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=target, daemon=True).start()


def escape(text: str) -> str:
    return GLib.markup_escape_text(text)
