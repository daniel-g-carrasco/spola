"""Main window: the synced folders grouped by where they are, a page per
folder, the sign-in page, and the dialogs that review a folder."""

from __future__ import annotations

import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, KOOFR_APP_PASSWORDS_URL  # noqa: E402
from . import account, autostart, conflicts, jobs, koofr_api, nautilus_integration, preflight, secrets  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .grouping import expected_remote, group_pairs  # noqa: E402
from .i18n import _, ngettext, pgettext  # noqa: E402
from .rclone import Mode, Outcome, Result, describe  # noqa: E402
from .ui import (display_path, escape, file_details, progress_text, reveal, run_in_thread,  # noqa: E402
                 place_name, status_badge, status_css, status_detail, status_icon, status_line, status_text,
                 syncing_for_real, when)

PREVIEW_LIMIT = 300
STATE_VERSION = 2       # window.json of the first layout (600 px wide) is ignored


class StatusIcon(Gtk.Stack):
    """The status icon of a pair, or a spinner while it syncs: one slot, so
    the rows keep the same shape whatever happens."""

    def __init__(self):
        super().__init__(valign=Gtk.Align.CENTER)
        self.image = Gtk.Image(pixel_size=16)
        self.add_child(self.image)
        self.spinner = Adw.Spinner(width_request=16, height_request=16)
        self.add_child(self.spinner)

    def show(self, status: Status) -> None:
        if status is Status.SYNCING:
            self.set_visible_child(self.spinner)
            return
        self.image.set_from_icon_name(status_icon(status))
        self.image.set_css_classes(status_css(status))
        self.set_visible_child(self.image)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, engine: Engine):
        super().__init__(application=app, title=APP_NAME, default_width=480, default_height=640,
                         width_request=360, height_request=400)
        self.set_hide_on_close(True)
        self.app = app
        self.engine = engine
        self.rows: dict[str, PairRow] = {}
        self.groups: list[Adw.PreferencesGroup] = []
        self._state_file = engine.paths.state / "window.json"
        self._restore_state()
        self.connect("close-request", lambda *_: self.save_state() and False)

        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.nav.add(self._folders_page())
        self.toasts.set_child(self.nav)
        self.set_content(self.toasts)

        engine.connect("pair-changed", lambda _e, pid: self._update_row(pid))
        engine.connect("pairs-changed", lambda _e: self._rebuild_rows())
        engine.connect("status-changed", lambda _e: self.overall.update())
        engine.connect("run-finished", self._on_run_finished)
        self.nav.connect("popped", lambda _n, page: page.detach() if isinstance(page, PairPage) else None)
        self._rebuild_rows()
        self.show_page()

    def show_page(self) -> None:
        signed_in = self.engine.creds is not None
        if not signed_in:
            self.nav.pop_to_tag("folders")
        self.stack.set_visible_child_name("list" if signed_in and self.engine.cfg.pairs
                                          else "empty" if signed_in else "login")
        self.add_button.set_visible(signed_in)
        self.overall.update()

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
            if state.get("version") != STATE_VERSION:
                return
            self.set_default_size(int(state["width"]), int(state["height"]))
            if state.get("maximized"):
                self.maximize()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def save_state(self) -> None:
        width, height = self.get_default_size()
        state = {"version": STATE_VERSION, "width": width, "height": height, "maximized": self.is_maximized()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    # -- the folders page --------------------------------------------------------------

    def _folders_page(self) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Folder"))
        self.add_button.connect("clicked", lambda *_: self.add_folder())
        header.pack_start(self.add_button)
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
        empty = Adw.StatusPage(icon_name="folder-symbolic", title=_("No Synced Folders"),
                               description=_("Choose a folder on this computer and the Koofr folder "
                                             "to keep identical to it."))
        add = Gtk.Button(label=_("Add Folder"), halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"])
        add.connect("clicked", lambda *_: self.add_folder())
        empty.set_child(add)
        self.stack.add_named(empty, "empty")
        self.list_page = Adw.PreferencesPage()
        self.overall = OverallStatus(self)
        group = Adw.PreferencesGroup()
        group.add(self.overall)
        self.list_page.add(group)
        self.stack.add_named(self.list_page, "list")
        view.set_content(self.stack)
        return Adw.NavigationPage(title=APP_NAME, tag="folders", child=view)

    def add_folder(self) -> None:
        from .add_folder import AddFolderDialog
        AddFolderDialog(self).present(self)

    def _rebuild_rows(self) -> None:
        for group in self.groups:
            self.list_page.remove(group)
        self.groups.clear()
        self.rows.clear()
        for parent, members in group_pairs(self.engine.cfg.pairs):
            group = Adw.PreferencesGroup(title=escape(place_name(parent)))
            for pair_id, title in members:
                row = PairRow(self, pair_id, title)
                self.rows[pair_id] = row
                group.add(row)
                row.update()
            self.list_page.add(group)
            self.groups.append(group)
        page = self.nav.get_visible_page()
        if isinstance(page, PairPage) and self.engine.cfg.pair(page.pair_id) is None:
            self.nav.pop_to_tag("folders")
        self.show_page()

    def _update_row(self, pair_id: str) -> None:
        row = self.rows.get(pair_id)
        if row:
            row.update()
        self.overall.update()

    def show_pair(self, pair_id: str) -> None:
        page = self.nav.get_visible_page()
        if isinstance(page, PairPage) and page.pair_id == pair_id:
            return
        self.nav.pop_to_tag("folders")
        if self.engine.cfg.pair(pair_id):
            self.nav.push(PairPage(self, pair_id))

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
        self.email = Adw.EntryRow(title=_("E-mail"), text=self.engine.cfg.user or "")
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
            account.check(self.engine.rclone, self.engine.paths, creds)
            secrets.store(user, password)
            try:
                mount_id = koofr_api.primary_mount_id(user, password)
            except koofr_api.ApiError:
                mount_id = ""      # the activity poll fetches it later
            return creds, mount_id

        def done(result, error):
            self.login_button.set_sensitive(True)
            if error:
                self._login_failed(str(error))
                return
            creds, mount_id = result
            self.password.set_text("")
            self.engine.cfg.user = creds.user
            self.engine.cfg.mount_id = mount_id or self.engine.cfg.mount_id
            self.engine.save_config()
            self.engine.set_credentials(creds)
            self.show_page()

        run_in_thread(work, done)

    def _login_failed(self, message: str) -> None:
        self.login_error.set_label(message)
        self.login_error.set_visible(True)

    def sign_out(self, parent: Gtk.Widget, done=None) -> None:
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
            if done:
                done()

        dialog.connect("response", on_response)
        dialog.present(parent)

    # -- previews and confirmations ----------------------------------------------------

    def review(self, pair_id: str) -> None:
        """Whatever the pair needs a look at: a preview to confirm, the
        conflicts, the files left out; on top of the pair's page."""
        rt = self.engine.runtime.get(pair_id)
        if rt is None:
            return
        self.show_pair(pair_id)
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


class OverallStatus(Gtk.Box):
    """Everything at a glance, at the top of the list: a coloured icon, what
    is going on, one line of detail, the progress while syncing and the one
    action that matters now. The folders that wait for the user come first,
    the most serious one is opened by Review."""

    LOOK = (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.ERROR, Status.CONFLICTS,
            Status.SKIPPED, Status.NEEDS_SETUP)

    def __init__(self, window: MainWindow):
        super().__init__(spacing=16, css_classes=["card"])
        self.window, self.engine = window, window.engine
        self.icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER, margin_start=18, margin_top=18,
                              margin_bottom=18, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.append(self.icon)
        self.texts = texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                                     hexpand=True, margin_top=14, margin_bottom=14)
        self.title = Gtk.Label(xalign=0, wrap=True, css_classes=["title-3"])
        self.detail = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self.progress = Gtk.ProgressBar(margin_top=6, visible=False)
        for widget in (self.title, self.detail, self.progress):
            texts.append(widget)
        self.append(texts)
        self.action = Gtk.Button(valign=Gtk.Align.CENTER, margin_end=18, visible=False)
        self.action.connect("clicked", lambda *_: self._act())
        self.append(self.action)
        self._do = None

    def _act(self) -> None:
        if self._do:
            self._do()

    def _show(self, status: Status, title: str, detail: str = "", action: str = "", do=None,
              suggested: bool = False) -> None:
        self.icon.set_from_icon_name(status_badge(status))
        self.title.set_label(title)
        self.detail.set_label(detail)
        self.detail.set_visible(bool(detail))
        self.action.set_visible(bool(action))
        self.texts.set_margin_end(0 if action else 18)      # the progress bar keeps off the edge
        self.action.set_label(action)
        self.action.set_css_classes(["suggested-action"] if suggested else [])
        self._do = do

    def update(self) -> None:
        engine, cfg = self.engine, self.engine.cfg
        pairs = [p for p in cfg.pairs if p.id in engine.runtime]
        self.progress.set_visible(False)
        if not pairs or engine.creds is None:
            return
        waiting = sorted((engine.runtime[p.id].status, p.name.casefold(), p.id) for p in pairs
                         if not p.paused and engine.runtime[p.id].status in self.LOOK)
        if waiting:
            status, _name, pair_id = waiting[0]
            pair, rt = cfg.pair(pair_id), engine.runtime[pair_id]
            if len(waiting) > 1:
                names = [cfg.pair(pid).name for _s, _n, pid in waiting]
                title = ngettext("{n} folder needs your attention", "{n} folders need your attention",
                                 len(waiting)).format(n=len(waiting))
                detail = ", ".join(names[:3]) + (" \u2026" if len(names) > 3 else "")
            else:
                title = {
                    Status.NEEDS_REPAIR: _("“{name}” needs a repair"),
                    Status.NEEDS_CONFIRMATION: _("“{name}” is waiting for your confirmation"),
                    Status.NEEDS_SETUP: _("“{name}” is waiting for its first alignment"),
                    Status.CONFLICTS: _("Conflicts in “{name}”"),
                    Status.SKIPPED: _("Some files in “{name}” are not synced"),
                    Status.ERROR: _("“{name}” cannot be synced"),
                }[status].format(name=pair.name)
                n = len(engine.conflicts(pair_id)) if status is Status.CONFLICTS else len(rt.refused)
                detail = {
                    Status.CONFLICTS: ngettext("{n} file changed on both sides",
                                               "{n} files changed on both sides", n).format(n=n),
                    Status.SKIPPED: ngettext("{n} name Koofr does not accept",
                                             "{n} names Koofr does not accept", n).format(n=n),
                }.get(status, rt.message)
            if status is Status.ERROR:
                self._show(status, title, detail, _("Try Again"),
                           lambda: engine.request_sync(pair_id, "manual"))
            else:
                self._show(status, title, detail, _("Review"), lambda: self.window.review(pair_id), suggested=True)
            return
        task = engine.active
        if task is not None and not task.dry_run and cfg.pair(task.pair_id):
            pair, rt = cfg.pair(task.pair_id), engine.runtime[task.pair_id]
            text, fraction = progress_text(rt.stats)
            self._show(Status.SYNCING, _("Syncing “{name}”").format(name=pair.name), text or _("Comparing the two sides…"))
            self.progress.set_visible(True)
            if fraction is None:
                self.progress.pulse()
            else:
                self.progress.set_fraction(fraction)
            return
        active = [p for p in pairs if not p.paused]
        if cfg.paused_all:
            self._show(Status.PAUSED, _("Paused"), _("Nothing is synced until you resume."), _("Resume"),
                       lambda: engine.set_paused_all(False))
            return
        if not active or engine.blocked_reason():
            self._show(Status.PAUSED, _("Paused"), engine.blocked_reason() or _("Every folder is paused."))
            return
        if any(engine.runtime[p.id].status in (Status.PENDING, Status.SYNCING) for p in active):
            self._show(Status.PENDING, status_text(Status.PENDING))
            return
        synced = [engine.runtime[p.id].last_sync for p in active if engine.runtime[p.id].last_sync]
        detail = ngettext("{n} folder", "{n} folders", len(active)).format(n=len(active))
        if synced:
            detail += " · " + _("last sync {time}").format(time=when(max(synced)))
        paused = len(pairs) - len(active)
        if paused:
            detail += " · " + ngettext("{n} paused", "{n} paused", paused).format(n=paused)
        self._show(Status.OK, _("Everything Is Up to Date"), detail)


class PairRow(Adw.ActionRow):
    """One synced folder in the list: its status icon, its name relative to
    the group, one line of status; the page of the folder opens on click."""

    def __init__(self, window: MainWindow, pair_id: str, title: str):
        super().__init__(title=escape(title), activatable=True, title_lines=1, subtitle_lines=1)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        self.icon = StatusIcon()
        self.add_prefix(self.icon)
        self.add_suffix(Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        self.connect("activated", lambda *_: self.window.show_pair(self.pair_id))

    def update(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        rt = self.engine.runtime.get(self.pair_id)
        if pair is None or rt is None:
            return
        line = status_line(self.engine, self.pair_id)
        if pair.remote != expected_remote(pair.local, str(os.path.expanduser("~"))):
            line += " · " + _("Koofr {path}").format(path=pair.remote)
        self.set_subtitle(escape(line))
        self.set_tooltip_text(line)
        self.icon.show(rt.status)


class PairPage(Adw.NavigationPage):
    """Everything about one synced folder: its status and the action it
    needs, where the two sides are, pause, the reviews, removal."""

    def __init__(self, window: MainWindow, pair_id: str):
        pair = window.engine.cfg.pair(pair_id)
        super().__init__(title=pair.name)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=pair.name, subtitle=place_name(os.path.dirname(pair.local))))
        view.add_top_bar(header)
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup()
        self.status_row = Adw.ActionRow(subtitle_lines=0)
        self.icon = StatusIcon()
        self.status_row.add_prefix(self.icon)
        self.action_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.action_button.connect("clicked", lambda *_: self._primary())
        self.status_row.add_suffix(self.action_button)
        group.add(self.status_row)
        self.progress = Gtk.ProgressBar(margin_top=14, margin_bottom=14, margin_start=12, margin_end=12)
        self.progress_row = Adw.PreferencesRow(child=self.progress, activatable=False, focusable=False)
        group.add(self.progress_row)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Location"))
        here = Adw.ActionRow(title=_("This Computer"), subtitle=escape(display_path(pair.local)), activatable=True,
                             tooltip_text=_("Open Folder"))
        here.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        here.connect("activated", lambda *_: self._open_local())
        group.add(here)
        koofr = Adw.ActionRow(title=_("Koofr"), subtitle=escape(pair.remote), activatable=True,
                              tooltip_text=_("Open on Koofr"))
        koofr.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
        koofr.connect("activated", lambda *_: self._open_web())
        group.add(koofr)
        page.add(group)

        group = Adw.PreferencesGroup(title=pgettext("settings group", "Syncing"))
        self.keep = Adw.SwitchRow(title=_("Keep in Sync"),
                                  subtitle=_("When off, nothing is copied either way until you turn it on"))
        self.keep.connect("notify::active", lambda r, _p: self.engine.set_paused(self.pair_id, not r.get_active()))
        group.add(self.keep)
        self.conflicts_row = self._link_row(group, _("Conflicts"),
                                            lambda: ConflictsDialog(self.window, self.pair_id).present(self.window))
        self.left_out_row = self._link_row(group, _("Files Not Synced"),
                                           lambda: LeftOutDialog(self.window, self.pair_id).present(self.window))
        self.preview_row = self._link_row(group, _("Preview Next Sync"), self._preview,
                                          _("What the next sync would do, without changing anything"))
        self.exclusions_row = self._link_row(group, _("Exclusions"),
                                             lambda: ExclusionsDialog(self.window, self.pair_id).present(self.window))
        page.add(group)

        group = Adw.PreferencesGroup()
        remove = Adw.ButtonRow(title=_("Stop Syncing This Folder…"))
        remove.add_css_class("destructive-action")      # (css_classes= would drop the row's own classes)
        remove.connect("activated", lambda *_: self._remove())
        group.add(remove)
        page.add(group)

        view.set_content(page)
        self.set_child(view)
        self._handlers = [
            self.engine.connect("pair-changed", lambda _e, pid: self.update() if pid == pair_id else None),
            self.engine.connect("status-changed", lambda _e: self.update())]
        self.update()

    def detach(self) -> None:
        """Popped off the view, the page is gone for good: stop following the engine."""
        for handler in self._handlers:
            self.engine.disconnect(handler)
        self._handlers = []

    @staticmethod
    def _link_row(group, title: str, action, subtitle: str = "") -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=escape(subtitle), activatable=True)
        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION))
        row.connect("activated", lambda *_: action())
        group.add(row)
        return row

    def update(self) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        rt = self.engine.runtime.get(self.pair_id)
        if pair is None or rt is None:
            return
        self.status_row.set_title(escape(status_text(rt.status)))
        self.status_row.set_subtitle(escape(status_detail(self.engine, self.pair_id, short=False)))
        self.icon.show(rt.status)
        syncing = syncing_for_real(self.engine, self.pair_id)
        self.progress_row.set_visible(syncing)
        if syncing:
            fraction = progress_text(rt.stats)[1]
            if fraction is None:
                self.progress.pulse()
            else:
                self.progress.set_fraction(fraction)

        signed_in = self.engine.creds is not None
        if rt.status is Status.NEEDS_SETUP:
            label, css, sensitive = _("Align…"), ["suggested-action"], signed_in
        elif rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED):
            label, css, sensitive = _("Review…"), ["suggested-action"], True
        elif rt.status is Status.SYNCING:
            label, css, sensitive = _("Stop"), [], True
        else:
            label, css, sensitive = _("Sync Now"), [], signed_in and not pair.paused and not self.engine.cfg.paused_all
        self.action_button.set_label(label)
        self.action_button.set_css_classes(css)
        self.action_button.set_sensitive(sensitive)

        if self.keep.get_active() == pair.paused:
            self.keep.set_active(not pair.paused)
        n = len(self.engine.conflicts(self.pair_id)) if rt.conflicts else 0
        self.conflicts_row.set_visible(n > 0)
        self.conflicts_row.set_subtitle(escape(ngettext("{n} file changed on both sides",
                                                        "{n} files changed on both sides", n).format(n=n)))
        self.left_out_row.set_visible(bool(rt.refused))
        self.left_out_row.set_subtitle(escape(ngettext("{n} name Koofr does not accept",
                                                       "{n} names Koofr does not accept",
                                                       len(rt.refused)).format(n=len(rt.refused))))
        self.preview_row.set_visible(pair.initialized)
        self.preview_row.set_sensitive(rt.status is not Status.SYNCING and signed_in)
        patterns = [line for line in jobs.read_filters(self.engine.paths, pair).splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.exclusions_row.set_subtitle(escape(ngettext("{n} pattern", "{n} patterns", len(patterns))
                                                .format(n=len(patterns))))

    def _primary(self) -> None:
        rt = self.engine.runtime.get(self.pair_id)
        if rt is None:
            return
        if rt.status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION,
                         Status.CONFLICTS, Status.SKIPPED):
            self.window.review(self.pair_id)
        elif rt.status is Status.SYNCING:
            self.engine.stop(self.pair_id)
        else:
            self.engine.request_sync(self.pair_id, "manual")

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
                parts.append(_("This computer: {details}").format(details=file_details(conflict.local)))
            if conflict.koofr:
                parts.append(_("Koofr: {details}").format(details=file_details(conflict.koofr)))
            row.set_subtitle(GLib.markup_escape_text("\n".join(parts)))
            row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
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

        group = Adw.PreferencesGroup(title=_("Account"))
        signed_in = self.engine.creds is not None
        self.account_row = Adw.ActionRow(title=escape(self.engine.creds.user if signed_in else _("Not signed in")))
        if signed_in:
            sign_out = Gtk.Button(label=_("Sign Out"), valign=Gtk.Align.CENTER)
            sign_out.connect("clicked", lambda *_: window.sign_out(self, done=self.close))
            self.account_row.add_suffix(sign_out)
            self._refresh_quota()
        group.add(self.account_row)
        page.add(group)

        group = Adw.PreferencesGroup(title=pgettext("settings group", "Syncing"))
        metered = Adw.SwitchRow(title=_("Pause on metered connections"), active=cfg.pause_on_metered)
        metered.connect("notify::active", lambda r, _p: self._set("pause_on_metered", r.get_active(), conditions=True))
        group.add(metered)
        saver = Adw.SwitchRow(title=_("Pause in power saver mode"), active=cfg.pause_on_power_saver)
        saver.connect("notify::active", lambda r, _p: self._set("pause_on_power_saver", r.get_active(), conditions=True))
        group.add(saver)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Speed Limits"), description=_("In KB/s; 0 means no limit. Applied from the next sync."))
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

    def _refresh_quota(self) -> None:
        creds = self.engine.creds
        if creds is None or self.engine.rclone is None or self.engine.dev_remote:
            return

        def done(quota, _error):
            if quota and quota.total:
                self.account_row.set_subtitle(_("{used} used of {total}").format(
                    used=GLib.format_size(quota.used or 0), total=GLib.format_size(quota.total)))
        run_in_thread(lambda: account.check(self.engine.rclone, self.engine.paths, creds), done)

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

