"""Main window, sign-in page, "add folder" and preview dialogs."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, KOOFR_APP_PASSWORDS_URL  # noqa: E402
from . import account, autostart, conflicts, jobs, koofr_api, nautilus_integration, preflight, secrets  # noqa: E402
from .config import normalize_remote  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .rclone import Mode, Outcome, Result, default_excluded, describe  # noqa: E402

PREVIEW_LIMIT = 300

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


def status_icon(status: Status) -> str:
    return APP_ID + STATUS_ICONS[status]


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


def progress_text(stats: dict | None) -> tuple[str, float | None]:
    """A short line for the row and the fraction for its bar (None while
    the two sides are still being compared)."""
    if not stats:
        return "", None
    total, done = stats.get("totalBytes") or 0, stats.get("bytes") or 0
    files, total_files = stats.get("transfers") or 0, stats.get("totalTransfers") or 0
    if not total_files and not total:
        checks = stats.get("checks") or 0
        return (_("Comparing files ({n} checked)").format(n=checks) if checks else ""), None
    text = ngettext("{done} of {total} file", "{done} of {total} files", total_files).format(done=files, total=total_files)
    speed = stats.get("speed") or 0
    if speed:
        text += " · " + _("{speed}/s").format(speed=GLib.format_size(int(speed)))
    fraction = done / total if total else (files / total_files if total_files else 0.0)
    return text, max(0.0, min(1.0, fraction))


def display_path(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def reveal(window, path: str) -> None:
    """Show the file in the file manager, selected (the OpenURI portal's
    OpenDirectory inside the sandbox)."""
    Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(window, None, None, None)


def file_details(copy: conflicts.Copy) -> str:
    when = GLib.DateTime.new_from_unix_local(int(copy.mtime)).format("%d %b %H:%M")
    return f"{GLib.format_size(copy.size)} \u00b7 {when}"


def run_in_thread(work, done) -> None:
    """work() in a thread, done(result, error) back on the main loop."""
    def target():
        try:
            result, error = work(), None
        except Exception as e:  # shown to the user, never swallowed
            result, error = None, e
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=target, daemon=True).start()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, engine: Engine):
        super().__init__(application=app, title=APP_NAME, default_width=600, default_height=680)
        self.set_hide_on_close(True)
        self.app = app
        self.engine = engine
        self.rows: dict[str, PairRow] = {}
        self._state_file = engine.paths.state / "window.json"
        self._restore_state()
        self.connect("close-request", lambda *_: self.save_state() and False)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Sync All Now"), "app.sync-all")
        section.append(_("Pause All"), "app.pause-all")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Preferences"), "app.preferences")
        section.append(_("About Spola"), "app.about")
        section.append(_("Quit"), "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text=_("Main Menu"), primary=True))
        view.add_top_bar(header)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._login_page(), "login")
        self.stack.add_named(self._main_page(), "main")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        engine.connect("pair-changed", lambda _e, pid: self._update_row(pid))
        engine.connect("pairs-changed", lambda _e: self._rebuild_rows())
        engine.connect("run-finished", self._on_run_finished)
        self._rebuild_rows()
        self.show_page()

    def show_page(self) -> None:
        self.stack.set_visible_child_name("main" if self.engine.creds else "login")
        if self.engine.creds:
            self.account_row.set_title(self.engine.creds.user)
            self._refresh_quota()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=5))

    def _on_run_finished(self, _engine, pair_id: str, reason: str) -> None:
        """A click on Sync Now deserves an answer, even when nothing changed."""
        if reason != "manual" or not self.get_visible():
            return
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime.get(pair_id)
        if pair is None or rt is None or rt.last_outcome is None or rt.last_outcome.result is not Result.OK:
            return
        summary = describe(rt.last_outcome.plan)
        self.toast(_("“{name}”: {summary}").format(name=pair.name, summary=summary) if summary
                   else _("“{name}” was already up to date").format(name=pair.name))

    # -- remembered size ------------------------------------------------------------

    def _restore_state(self) -> None:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                state = json.load(f)
            self.set_default_size(int(state["width"]), int(state["height"]))
            if state.get("maximized"):
                self.maximize()
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save_state(self) -> None:
        width, height = self.get_default_size()
        state = {"width": width, "height": height, "maximized": self.is_maximized()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    # -- sign in ---------------------------------------------------------------

    def _login_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(icon_name=APP_ID, title=_("Sign in to Koofr"))
        page.set_description(
            _("Spola needs your Koofr e-mail and an <b>app password</b>: Koofr does not accept your main "
              "password from other apps. You can create one in your Koofr account settings, under "
              "<a href=\"{url}\">Passwords</a>.").format(url=KOOFR_APP_PASSWORDS_URL))
        clamp = Adw.Clamp(maximum_size=420)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        group = Adw.PreferencesGroup()
        self.email = Adw.EntryRow(title=_("E-mail"))
        self.password = Adw.PasswordEntryRow(title=_("App password"))
        self.password.connect("entry-activated", lambda *_: self._sign_in())
        group.add(self.email)
        group.add(self.password)
        box.append(group)
        self.login_error = Gtk.Label(wrap=True, visible=False, css_classes=["error"])
        box.append(self.login_error)
        self.login_button = Gtk.Button(label=_("Sign In"), halign=Gtk.Align.CENTER,
                                       css_classes=["suggested-action", "pill"])
        self.login_button.connect("clicked", lambda *_: self._sign_in())
        box.append(self.login_button)
        clamp.set_child(box)
        page.set_child(clamp)
        return page

    def _sign_in(self) -> None:
        user, password = self.email.get_text().strip(), self.password.get_text()
        if not user or not password:
            return
        if self.engine.rclone is None:
            self._login_failed(_("rclone was not found."))
            return
        self.login_button.set_sensitive(False)
        self.login_error.set_visible(False)

        def work():
            creds = account.Credentials.from_password(user, password, self.engine.rclone)
            quota = account.check(self.engine.rclone, self.engine.paths, creds)
            secrets.store(user, password)
            try:
                mount_id = koofr_api.primary_mount_id(user, password)
            except koofr_api.ApiError:
                mount_id = ""      # the activity poll fetches it later
            return creds, quota, mount_id

        def done(result, error):
            self.login_button.set_sensitive(True)
            if error:
                self._login_failed(str(error))
                return
            creds, quota, mount_id = result
            self.password.set_text("")
            self.engine.cfg.user = creds.user
            self.engine.cfg.mount_id = mount_id or self.engine.cfg.mount_id
            self.engine.save_config()
            self.engine.set_credentials(creds)
            self._show_quota(quota)
            self.show_page()

        run_in_thread(work, done)

    def _login_failed(self, message: str) -> None:
        self.login_error.set_label(message)
        self.login_error.set_visible(True)

    def _sign_out(self) -> None:
        user = self.engine.cfg.user
        dialog = Adw.AlertDialog(
            heading=_("Sign out?"),
            body=_("Syncing stops and the app password is removed from the keyring. Your files stay where they are."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("sign-out", _("Sign Out"))
        dialog.set_response_appearance("sign-out", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, response):
            if response != "sign-out":
                return
            try:
                secrets.clear(user)
            except GLib.Error:
                pass
            self.engine.set_paused_all(True)
            self.engine.set_credentials(None)
            self.engine.set_paused_all(False)
            self.email.set_text(user)
            self.show_page()

        dialog.connect("response", on_response)
        dialog.present(self)

    # -- main page ---------------------------------------------------------------

    def _main_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.pairs_group = Adw.PreferencesGroup(title=_("Folders"))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Folder"), css_classes=["flat"])
        add.connect("clicked", lambda *_: AddPairDialog(self).present(self))
        self.pairs_group.set_header_suffix(add)
        self.empty_row = Adw.ActionRow(title=_("No folders yet"),
                                       subtitle=_("Add a folder to keep it in sync with Koofr."))
        page.add(self.pairs_group)

        group = Adw.PreferencesGroup(title=_("Account"))
        self.account_row = Adw.ActionRow(title="", subtitle="")
        sign_out = Gtk.Button(label=_("Sign Out"), valign=Gtk.Align.CENTER)
        sign_out.connect("clicked", lambda *_: self._sign_out())
        self.account_row.add_suffix(sign_out)
        group.add(self.account_row)
        page.add(group)
        return page

    def _refresh_quota(self) -> None:
        creds = self.engine.creds
        if creds is None or self.engine.rclone is None or self.engine.dev_remote:
            return
        run_in_thread(lambda: account.check(self.engine.rclone, self.engine.paths, creds),
                      lambda quota, error: self._show_quota(quota) if quota else None)

    def _show_quota(self, quota) -> None:
        if quota and quota.total:
            self.account_row.set_subtitle(_("{used} used of {total}").format(
                used=GLib.format_size(quota.used or 0), total=GLib.format_size(quota.total)))

    def _rebuild_rows(self) -> None:
        for row in list(self.rows.values()) + [self.empty_row]:
            if row.get_parent() is not None:
                self.pairs_group.remove(row)
        self.rows.clear()
        for pair in self.engine.cfg.pairs:
            row = PairRow(self, pair.id)
            self.rows[pair.id] = row
            self.pairs_group.add(row)
            row.update()
        if not self.engine.cfg.pairs:
            self.pairs_group.add(self.empty_row)

    def _update_row(self, pair_id: str) -> None:
        row = self.rows.get(pair_id)
        if row:
            row.update()

    # -- previews and confirmations ----------------------------------------------------

    def review(self, pair_id: str) -> None:
        """Whatever the pair needs a look at: a preview to confirm, the
        conflicts, the files left out."""
        rt = self.engine.runtime.get(pair_id)
        if rt is None:
            return
        if rt.status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
            self.open_preview(pair_id)
        elif rt.conflicts:
            ConflictsDialog(self, pair_id).present(self)
        elif rt.refused:
            LeftOutDialog(self, pair_id).present(self)

    def open_preview(self, pair_id: str) -> None:
        """The right preview for the pair's state: first alignment, repair,
        or confirming a run that stopped on a safety check."""
        rt = self.engine.runtime[pair_id]
        pair = self.engine.cfg.pair(pair_id)
        if not pair.initialized:
            PreviewDialog(self, pair_id, Mode.RESYNC, False, _("First Alignment"),
                          _("Nothing is deleted: files only on one side are copied to the other; where both "
                            "sides have a different version, the newer one wins and the other is kept in "
                            "Spola's backup or in Koofr's previous versions."), _("Align")).present(self)
        elif rt.status is Status.NEEDS_REPAIR:
            PreviewDialog(self, pair_id, Mode.RESYNC, False, _("Repair"),
                          _("Spola lost track of the last synced state ({reason}). A new alignment copies "
                            "what is missing on each side and deletes nothing.").format(reason=rt.message),
                          _("Repair")).present(self)
        else:
            PreviewDialog(self, pair_id, Mode.SYNC, True, _("Confirm Changes"),
                          _("The sync stopped because it would delete or change many files at once ({reason}). "
                            "Check the list: if it is what you expect, confirm.").format(reason=rt.message),
                          _("Confirm")).present(self)


class PairRow(Adw.ActionRow):
    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__()
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        self.icon = Gtk.Image(pixel_size=16)
        self.add_prefix(self.icon)
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=110, visible=False)
        self.add_suffix(self.progress)
        self.action_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.action_button.connect("clicked", lambda *_: self._primary())
        self.add_suffix(self.action_button)
        self.menu_button = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                          css_classes=["flat"], tooltip_text=_("More"))
        self.add_suffix(self.menu_button)
        self.set_subtitle_lines(3)

        group = Gio.SimpleActionGroup()
        for name, handler in (("sync", self._sync_now), ("pause", self._toggle_pause),
                              ("preview", self._preview), ("open", self._open_local),
                              ("open-web", self._open_web), ("remove", self._remove),
                              ("conflicts", lambda: ConflictsDialog(self.window, self.pair_id).present(self.window)),
                              ("left-out", lambda: LeftOutDialog(self.window, self.pair_id).present(self.window)),
                              ("exclusions", lambda: ExclusionsDialog(self.window, self.pair_id).present(self.window)),
                              ("stop", lambda: self.engine.stop(self.pair_id))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            group.add_action(action)
        self.insert_action_group("pair", group)
        self.actions = group
        self.connect("activated", lambda *_: self.window.review(self.pair_id))

    def update(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        rt = self.engine.runtime.get(self.pair_id)
        if pair is None or rt is None:
            return
        self.set_title(GLib.markup_escape_text(pair.name))
        lines = [GLib.markup_escape_text(f"{display_path(pair.local)}  ⇄  Koofr {pair.remote}")]
        state = status_text(rt.status)
        syncing = rt.status is Status.SYNCING and not (rt.task and rt.task.dry_run)
        self.progress.set_visible(syncing)
        if rt.status is Status.SYNCING:
            if syncing:
                detail, fraction = progress_text(rt.stats)
                if fraction is None:
                    self.progress.pulse()
                else:
                    self.progress.set_fraction(fraction)
            else:
                detail = rt.message
            state = f"{state}: {detail}" if detail else state
        elif rt.status is Status.CONFLICTS:
            n = len(self.engine.conflicts(self.pair_id))       # conflicts, not copies
            state += " (" + ngettext("{n} file", "{n} files", n).format(n=n) + ")"
        elif rt.status is Status.SKIPPED:
            names = ", ".join(r.path for r in rt.refused[:2]) + (" \u2026" if len(rt.refused) > 2 else "")
            state += f": {names}"
        elif rt.message and rt.status is not Status.OK and rt.message != state:
            state = f"{state}: {rt.message}"
        elif rt.status is Status.OK and rt.last_sync:
            state += " · " + _("last sync at {time}").format(
                time=GLib.DateTime.new_from_unix_local(int(rt.last_sync)).format("%H:%M"))
            summary = describe(rt.last_outcome.plan) if rt.last_outcome and rt.last_outcome.result is Result.OK else ""
            if summary:
                state += f": {summary}"
        lines.append(GLib.markup_escape_text(state))
        self.set_subtitle("\n".join(lines))
        self.icon.set_from_icon_name(status_icon(rt.status))

        attention = rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED)
        self.set_activatable(attention or rt.status is Status.NEEDS_SETUP)
        if rt.status is Status.NEEDS_SETUP:
            label, css = _("Align…"), ["suggested-action"]
        elif attention:
            label, css = _("Review…"), ["suggested-action"]
        elif rt.status is Status.SYNCING:
            label, css = _("Stop"), []
        else:
            label, css = _("Sync Now"), []
        self.action_button.set_label(label)
        self.action_button.set_css_classes(css)
        self.action_button.set_sensitive(self.engine.creds is not None)

        menu = Gio.Menu()
        if rt.conflicts:
            menu.append(_("Conflicts…"), "pair.conflicts")
        if rt.refused:
            menu.append(_("Files Not Synced…"), "pair.left-out")
        menu.append(_("Resume") if pair.paused else _("Pause"), "pair.pause")
        if pair.initialized:
            menu.append(_("Preview Next Sync…"), "pair.preview")
        menu.append(_("Exclusions…"), "pair.exclusions")
        menu.append(_("Open Folder"), "pair.open")
        menu.append(_("Open on Koofr"), "pair.open-web")
        section = Gio.Menu()
        section.append(_("Remove…"), "pair.remove")
        menu.append_section(None, section)
        self.menu_button.set_menu_model(menu)

    def _primary(self) -> None:
        rt = self.engine.runtime[self.pair_id]
        if rt.status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION,
                         Status.CONFLICTS, Status.SKIPPED):
            self.window.review(self.pair_id)
        elif rt.status is Status.SYNCING:
            self.engine.stop(self.pair_id)
        else:
            self._sync_now()

    def _sync_now(self) -> None:
        self.engine.request_sync(self.pair_id, "manual")

    def _toggle_pause(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        self.engine.set_paused(self.pair_id, not pair.paused)

    def _preview(self) -> None:
        PreviewDialog(self.window, self.pair_id, Mode.SYNC, False, _("Preview"),
                      _("What the next sync would do. Nothing is changed."), None).present(self.window)

    def _open_local(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        Gtk.FileLauncher.new(Gio.File.new_for_path(pair.local)).launch(self.window, None, None, None)

    def _open_web(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        url = koofr_api.web_url(self.engine.cfg.mount_id, pair.remote)
        Gtk.UriLauncher.new(url).launch(self.window, None, None, None)

    def _remove(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        dialog = Adw.AlertDialog(
            heading=_("Stop syncing “{name}”?").format(name=pair.name),
            body=_("Spola forgets this pair. No file is deleted, neither here nor on Koofr."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Stop Syncing"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: self.engine.remove_pair(self.pair_id) if r == "remove" else None)
        dialog.present(self.window)


class ConflictsDialog(Adw.Dialog):
    """One row per conflict: the original name, the two copies with size and
    date; the row shows the files in the file manager, the menu decides."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Conflicts"), content_width=560, content_height=520)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        view.set_content(self.page)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_child(self.toasts)
        self.group: Adw.PreferencesGroup | None = None
        self._handler = self.engine.connect("pair-changed", lambda _e, pid: self._fill() if pid == pair_id else None)
        self.connect("closed", lambda *_: self.engine.disconnect(self._handler))
        self._fill()

    def _fill(self) -> None:
        if self.group is not None:
            self.page.remove(self.group)
        items = self.engine.conflicts(self.pair_id)
        pair = self.engine.cfg.pair(self.pair_id)
        self.group = Adw.PreferencesGroup(description=_(
            "These files were changed both here and on Koofr before the two versions met: both are kept. "
            "Choose which one goes on under the original name; the other one goes to the wastebasket "
            "here and to Koofr's trash."))
        if not items:
            self.group.add(Adw.ActionRow(title=_("No conflicts left.")))
        for conflict in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(conflict.original), title_lines=2,
                                activatable=True, tooltip_text=_("Show in the file manager"))
            parts = []
            if conflict.local:
                parts.append(_("this computer: {details}").format(details=file_details(conflict.local)))
            if conflict.koofr:
                parts.append(_("Koofr: {details}").format(details=file_details(conflict.koofr)))
            row.set_subtitle(GLib.markup_escape_text(" \u00b7 ".join(parts)))
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
            first = conflict.copies[0]
            row.connect("activated", lambda _r, c=first: reveal(self.window, os.path.join(pair.local, c.path)))
            group = Gio.SimpleActionGroup()
            menu = Gio.Menu()
            for choice, text in (("local", _("Keep This Computer's")), ("koofr", _("Keep Koofr's")),
                                 ("both", _("Keep Both, Renamed"))):
                action = Gio.SimpleAction.new(choice, None)
                action.connect("activate", lambda _a, _p, c=conflict, ch=choice: self._resolve(c, ch))
                action.set_enabled((choice != "local" or conflict.local is not None)
                                   and (choice != "koofr" or conflict.koofr is not None))
                group.add_action(action)
                menu.append(text, f"conflict.{choice}")
            row.insert_action_group("conflict", group)
            button = Gtk.MenuButton(label=_("Resolve"), menu_model=menu, valign=Gtk.Align.CENTER)
            row.add_suffix(button)
            self.group.add(row)
        self.page.add(self.group)

    def _resolve(self, conflict: conflicts.Conflict, choice: str) -> None:
        try:
            self.engine.resolve_conflict(self.pair_id, conflict, choice)
        except (conflicts.ResolveError, OSError) as e:
            self.toasts.add_toast(Adw.Toast(title=str(e), timeout=6))


class LeftOutDialog(Adw.Dialog):
    """The files a run leaves out because Koofr refuses their names."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Files Not Synced"), content_width=560, content_height=480)
        engine = window.engine
        pair, rt = engine.cfg.pair(pair_id), engine.runtime.get(pair_id)
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description=_(
            "Koofr does not accept these names, so Spola leaves them out and syncs the rest. "
            "Rename them and they are synced at the next run. Nothing is deleted."))
        for refused in (rt.refused if rt else []):
            row = Adw.ActionRow(title=GLib.markup_escape_text(refused.path), title_lines=2, activatable=True,
                                subtitle=GLib.markup_escape_text(preflight.refused_reason(refused.code)),
                                subtitle_lines=3, tooltip_text=_("Show in the file manager"))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if refused.is_dir else "action-unavailable-symbolic"))
            row.connect("activated", lambda _r, p=refused.path: reveal(window, os.path.join(pair.local, p)))
            group.add(row)
        page.add(group)
        view.set_content(page)
        self.set_child(view)


class ExclusionsDialog(Adw.Dialog):
    """The pair's exclusions, in rclone's filter syntax, as a text."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Exclusions"), content_width=560, content_height=560)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        pair = self.engine.cfg.pair(pair_id)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label=_("Save"), css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self._save())
        header.pack_end(save)
        view.add_top_bar(header)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                      margin_start=12, margin_end=12)
        box.append(Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label"], label=_(
            "One pattern per line, with “- ” in front: a name, a *.ext, or folder/** for a whole "
            "folder. Excluded files are never deleted on either side. Changing the exclusions needs a new "
            "alignment of the pair, shown as a preview first.")))
        self.text = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
                                 left_margin=8, right_margin=8)
        self.text.get_buffer().set_text(jobs.read_filters(self.engine.paths, pair))
        scroller = Gtk.ScrolledWindow(child=self.text, vexpand=True, css_classes=["card"])
        box.append(scroller)
        view.set_content(box)
        self.set_child(view)

    def _save(self) -> None:
        buffer = self.text.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        changed = self.engine.set_filters(self.pair_id, text)
        self.close()
        pair = self.engine.cfg.pair(self.pair_id)
        if changed and pair and pair.initialized:
            self.window.open_preview(self.pair_id)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window: MainWindow):
        super().__init__(title=_("Preferences"))
        self.engine = window.engine
        cfg = self.engine.cfg
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title=_("Syncing"))
        metered = Adw.SwitchRow(title=_("Pause on metered connections"), active=cfg.pause_on_metered)
        metered.connect("notify::active", lambda r, _p: self._set("pause_on_metered", r.get_active(), conditions=True))
        group.add(metered)
        saver = Adw.SwitchRow(title=_("Pause in power saver mode"), active=cfg.pause_on_power_saver)
        saver.connect("notify::active", lambda r, _p: self._set("pause_on_power_saver", r.get_active(), conditions=True))
        group.add(saver)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Speed limits"), description=_("In KB/s; 0 means no limit. Applied from the next sync."))
        for name, title in (("upload_limit_kb", _("Upload to Koofr")), ("download_limit_kb", _("Download from Koofr"))):
            row = Adw.SpinRow.new_with_range(0, 1_000_000, 100)
            row.set_title(title)
            row.set_value(getattr(cfg, name))
            row.connect("notify::value", lambda r, _p, n=name: self._set(n, int(r.get_value())))
            group.add(row)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Startup"))
        auto = Adw.SwitchRow(title=_("Start at login"), subtitle=_("In the background, with the tray icon only"),
                             active=cfg.autostart)
        auto.connect("notify::active", lambda r, _p: self._set_autostart(r.get_active()))
        group.add(auto)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Files"))
        try:
            available = bool(nautilus_integration.source_dir())
        except FileNotFoundError:
            available = False
        files = Adw.SwitchRow(title=_("Show sync status in Files"),
                              subtitle=_("Emblems on files and folders, and a Koofr menu; needs nautilus-python "
                                         "and a restart of Files"),
                              active=nautilus_integration.installed(), sensitive=available)
        files.connect("notify::active", lambda r, _p: self._set_files(r.get_active()))
        group.add(files)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Backups"))
        days = Adw.SpinRow.new_with_range(1, 365, 1)
        days.set_title(_("Keep replaced and deleted files for"))
        days.set_subtitle(_("Days, in Spola’s local backup"))
        days.set_value(cfg.backup_days)
        days.connect("notify::value", lambda r, _p: self._set("backup_days", int(r.get_value())))
        group.add(days)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Conflicts"))
        languages = [("", _("Desktop language")), ("en", "English"), ("it", "Italiano")]
        combo = Adw.ComboRow(title=_("Language of the conflict copies’ names"),
                             subtitle=_("For example report.conflict-local-2026-10-10-1.odt"))
        combo.set_model(Gtk.StringList.new([label for _code, label in languages]))
        combo.set_selected(next((i for i, (code, _l) in enumerate(languages) if code == cfg.conflict_language), 0))
        combo.connect("notify::selected", lambda r, _p: self._set("conflict_language", languages[r.get_selected()][0]))
        group.add(combo)
        page.add(group)
        self.add(page)

    def _set(self, name: str, value, conditions: bool = False) -> None:
        setattr(self.engine.cfg, name, value)
        self.engine.save_config()
        if conditions:
            self.engine.conditions_changed()

    def _set_files(self, enabled: bool) -> None:
        try:
            if enabled:
                nautilus_integration.install()
            else:
                nautilus_integration.uninstall()
        except OSError as e:
            self.add_toast(Adw.Toast(title=str(e), timeout=6))
            return
        self._set("nautilus_integration", enabled)
        # (no process check: inside the Flatpak the host's Files is not visible)
        self.add_toast(Adw.Toast(title=_("Restart Files to apply (it keeps running until then)"), timeout=8))

    def _set_autostart(self, enabled: bool) -> None:
        self._set("autostart", enabled)
        app = getattr(self.engine, "app", None)
        autostart.apply(enabled, app.get_dbus_connection() if app else None)


class AddPairDialog(Adw.Dialog):
    def __init__(self, window: MainWindow):
        super().__init__(title=_("Add Folder"), content_width=520)
        self.window, self.engine = window, window.engine
        self.local: str | None = None
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.add_button = Gtk.Button(label=_("Add"), css_classes=["suggested-action"], sensitive=False)
        self.add_button.connect("clicked", lambda *_: self._check_and_add())
        header.pack_end(self.add_button)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description=_(
            "The two folders are kept identical in both directions. Choose a Koofr folder that is empty "
            "or that already holds the same files: the first alignment is shown before anything happens."))
        self.local_row = Adw.ActionRow(title=_("Folder on this computer"), subtitle=_("None chosen"))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose())
        self.local_row.add_suffix(choose)
        group.add(self.local_row)
        self.remote_row = Adw.EntryRow(title=_("Folder on Koofr"))
        self.remote_row.connect("changed", lambda *_: self._validate())
        group.add(self.remote_row)
        page.add(group)
        self.problems = Adw.PreferencesGroup(title=_("Cannot add this folder"), visible=False)
        page.add(self.problems)
        self.problem_rows: list[Gtk.Widget] = []
        view.set_content(page)
        self.set_child(view)

    def _choose(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose a Folder"), modal=True)

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            path = folder.get_path() if folder else None
            if not path:
                return
            self.local = path
            self.local_row.set_subtitle(GLib.markup_escape_text(path))
            if not self.remote_row.get_text():
                self.remote_row.set_text("/" + Path(path).name)
            self._validate()

        dialog.select_folder(self.window, None, done)

    def _validate(self) -> None:
        self.add_button.set_sensitive(bool(self.local and self.remote_row.get_text().strip()))

    def _show_problems(self, problems) -> None:
        for row in self.problem_rows:
            self.problems.remove(row)
        self.problem_rows = []
        for p in problems[:12]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.message),
                                subtitle=GLib.markup_escape_text(p.path), subtitle_lines=2, title_lines=3)
            self.problems.add(row)
            self.problem_rows.append(row)
        if len(problems) > 12:
            row = Adw.ActionRow(title=_("…and {n} more").format(n=len(problems) - 12))
            self.problems.add(row)
            self.problem_rows.append(row)
        self.problems.set_visible(bool(problems))

    def _check_and_add(self) -> None:
        local = self.local
        try:
            remote = normalize_remote(self.remote_row.get_text())
        except ValueError as e:
            self._show_problems([preflight.Problem("remote", str(e), self.remote_row.get_text())])
            return
        problems = (preflight.check_local_root(local)
                    + preflight.check_overlap(local, remote, self.engine.cfg)
                    + preflight.check_koofr_desktop(local, Path.home() / ".koofr" / "config.json"))
        if remote == "/":
            problems.append(preflight.Problem("remote-root", _("Choose a folder on Koofr, not the whole Koofr storage."), "/"))
        if problems:
            self._show_problems(problems)
            return
        self.add_button.set_sensitive(False)
        self.add_button.set_label(_("Checking names…"))
        def done(found, error):
            self.add_button.set_label(_("Add"))
            self._validate()
            if error:
                self._show_problems([preflight.Problem("scan", str(error), local)])
                return
            if found:
                self._show_problems(found)
                return
            pair = self.engine.add_pair(local, remote)
            self.close()
            self.window.open_preview(pair.id)

        run_in_thread(lambda: preflight.scan_names(local, remote, skip=default_excluded), done)


class PreviewDialog(Adw.Dialog):
    """Runs a dry run and shows what would happen; `confirm_label` None means
    information only."""

    def __init__(self, window: MainWindow, pair_id: str, mode: Mode, force: bool,
                 title: str, explanation: str, confirm_label: str | None):
        super().__init__(title=title, content_width=560, content_height=560)
        self.window, self.engine = window, window.engine
        self.pair_id, self.mode, self.force = pair_id, mode, force
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=confirm_label is None,
                               show_start_title_buttons=False)
        if confirm_label:
            cancel = Gtk.Button(label=_("Cancel"))
            cancel.connect("clicked", lambda *_: self.close())
            header.pack_start(cancel)
            self.confirm = Gtk.Button(label=confirm_label, css_classes=["suggested-action"], sensitive=False)
            self.confirm.connect("clicked", lambda *_: self._confirm())
            header.pack_end(self.confirm)
        else:
            self.confirm = None
        view.add_top_bar(header)

        self.page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(description=explanation)
        self.page.add(intro)
        self.spinner_group = Adw.PreferencesGroup()
        spinner_row = Adw.ActionRow(title=_("Comparing the two folders…"),
                                    subtitle=_("Nothing is changed while the preview runs."))
        spinner_row.add_prefix(Adw.Spinner())
        self.spinner_group.add(spinner_row)
        self.page.add(self.spinner_group)
        view.set_content(self.page)
        self.set_child(view)
        self.engine.preview(pair_id, mode, force, self._done)

    def _done(self, outcome: Outcome | None, error: str) -> None:
        self.page.remove(self.spinner_group)
        group = Adw.PreferencesGroup()
        self.page.add(group)
        if outcome is None:
            group.add(Adw.ActionRow(title=_("The preview failed"), subtitle=GLib.markup_escape_text(error)))
            return
        plan = outcome.plan
        if outcome.result is not Result.OK and not plan.empty:
            group.set_description(_("Note: {message}").format(message=outcome.message))
        elif outcome.result is not Result.OK:
            group.add(Adw.ActionRow(title=_("The preview stopped"),
                                    subtitle=GLib.markup_escape_text(outcome.message or ""), subtitle_lines=4))
            return
        rows = [
            (plan.upload, _("Copied to Koofr"), "go-up-symbolic"),
            (plan.download, _("Copied to this computer"), "go-down-symbolic"),
            (plan.replace_local, _("Replaced here by the newer Koofr version (the old one goes to the backup)"),
             "document-revert-symbolic"),
            (plan.delete_local, _("Deleted here (moved to Spola's backup)"), "user-trash-symbolic"),
            (plan.delete_remote, _("Deleted on Koofr"), "user-trash-symbolic"),
            (plan.conflicts, _("Changed on both sides: both versions are kept"), "dialog-warning-symbolic"),
            (plan.refused, _("Left out: Koofr does not accept these names (rename them to sync them)"),
             "action-unavailable-symbolic"),
        ]
        if plan.empty:
            group.add(Adw.ActionRow(title=_("Nothing to do: the two folders already match.")))
        for paths, title, icon in rows:
            if not paths:
                continue
            row = Adw.ExpanderRow(title=title, subtitle=ngettext("{n} file", "{n} files", len(paths)).format(n=len(paths)))
            row.add_prefix(Gtk.Image(icon_name=icon))
            for path in paths[:PREVIEW_LIMIT]:
                row.add_row(Adw.ActionRow(title=GLib.markup_escape_text(path), title_lines=2))
            if len(paths) > PREVIEW_LIMIT:
                row.add_row(Adw.ActionRow(title=_("…and {n} more").format(n=len(paths) - PREVIEW_LIMIT)))
            group.add(row)
        if self.confirm is not None:
            self.confirm.set_sensitive(True)
            if plan.delete_remote or plan.delete_local:
                self.confirm.set_css_classes(["destructive-action"])

    def _confirm(self) -> None:
        self.engine.confirm(self.pair_id, self.mode, self.force)
        self.close()


def about_dialog(parent) -> None:
    from . import WEBSITE, __version__
    dialog = Adw.AboutDialog(
        application_name=APP_NAME, application_icon=APP_ID, version=__version__,
        developer_name="Daniel Grasso", license_type=Gtk.License.MIT_X11, website=WEBSITE,
        comments=_("Keep your folders in sync with Koofr.\nNot affiliated with Koofr."))
    dialog.add_credit_section(_("Sync engine"), ["rclone https://rclone.org"])
    dialog.present(parent)

