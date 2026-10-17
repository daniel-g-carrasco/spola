"""Main window: the synced folders grouped by where they are, a page per
folder, the sign-in page, and the dialogs that review a folder."""

from __future__ import annotations

import json
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, KOOFR_APP_PASSWORDS_URL, WINDOWS  # noqa: E402
from . import account, activity, autostart, conflicts, file_managers, filetree, jobs, koofr_api  # noqa: E402
from . import diagnostics, plan_tree, preflight, secrets  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .grouping import expected_remote, group_pairs  # noqa: E402
from .i18n import _, count, ngettext, pgettext  # noqa: E402
from .rclone import Mode, Outcome, Result, describe, windows_reserved_names_matter  # noqa: E402
from .runner import newest_log  # noqa: E402
from .ui import (display_path, escape, file_details, progress_text, reveal, run_in_thread,  # noqa: E402
                 pair_status_text, place_name, status_badge, status_css, status_detail, status_icon, status_line,
                 status_text, syncing_for_real, when)

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


def header_bar() -> Adw.HeaderBar:
    """A page's header bar. On Windows a toolbar under the system title bar,
    which has the window buttons."""
    if WINDOWS:
        return Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
    return Adw.HeaderBar()


# On Windows a plain GTK window, so that it keeps the system title bar (an
# AdwApplicationWindow always draws its own, and with it Windows does not
# snap the window to the screen edges, nor on Win+arrows); its content is
# libadwaita's all the same. The dialogs then open as windows of their own.
_WindowBase = Gtk.ApplicationWindow if WINDOWS else Adw.ApplicationWindow


class MainWindow(_WindowBase):
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
        if WINDOWS:
            self.set_child(self.toasts)
            from .win32 import dark_title_bars
            style = Adw.StyleManager.get_default()
            self.connect("map", lambda *_: dark_title_bars(style.get_dark()))
            style.connect("notify::dark", lambda *_: dark_title_bars(style.get_dark()))
        else:
            self.set_content(self.toasts)

        engine.connect("pair-changed", lambda _e, pid: self._update_row(pid))
        engine.connect("pairs-changed", lambda _e: self._rebuild_rows())
        engine.connect("status-changed", lambda _e: self.update_overall())
        engine.connect("run-finished", self._on_run_finished)
        self.nav.connect("popped", lambda _n, page: page.detach() if isinstance(page, PairPage) else None)
        self._rebuild_rows()
        self.show_page()

    def show_page(self) -> None:
        signed_in = self.engine.creds is not None
        if not signed_in:
            self.nav.pop_to_tag("folders")
        empty = signed_in and not self.engine.cfg.pairs
        self.stack.set_visible_child_name("list" if signed_in and self.engine.cfg.pairs
                                          else "empty" if signed_in else "login")
        self.add_button.set_visible(signed_in)
        if empty:
            from .import_dialog import koofr_desktop_has_folders
            self.import_button.set_visible(koofr_desktop_has_folders())
        self.update_overall()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=escape(text), timeout=5))     # (a toast reads markup)

    def toast_review(self, text: str, pair_id: str) -> None:
        """A toast with a Review button, for news about one folder while the
        window is in front (a notification would say it outside)."""
        self.toasts.add_toast(Adw.Toast(title=escape(text), timeout=8, button_label=_("Review"),
                                        action_name="app.review", action_target=GLib.Variant("s", pair_id)))

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
        header = header_bar()
        if WINDOWS:
            header.set_title_widget(Gtk.Box())      # "Spola" is in the system title bar already
        self.add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add Folder"))
        self.add_button.connect("clicked", lambda *_: self.add_folder())
        header.pack_start(self.add_button)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Sync All Now"), "app.sync-all")
        section.append(_("Pause All"), "app.pause-all")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Import from Koofr Desktop…"), "app.import-koofr-desktop")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Preferences"), "app.preferences")
        if WINDOWS:     # on Linux the Flatpak brings a newer Spola
            section.append(_("Check for Updates…"), "app.check-updates")
        section.append(_("About Spola"), "app.about")
        section.append(_("Quit"), "app.quit")
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text=_("Main Menu"), primary=True))
        view.add_top_bar(header)
        self.banner = AttentionBanner(self)
        view.add_top_bar(self.banner.widget)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._login_page(), "login")
        empty = Adw.StatusPage(icon_name="folder-symbolic", title=_("No Synced Folders"),
                               description=_("Choose a folder on this computer and the Koofr folder "
                                             "to keep identical to it."))
        add = Gtk.Button(label=_("Add Folder"), halign=Gtk.Align.CENTER, css_classes=["suggested-action", "pill"])
        add.connect("clicked", lambda *_: self.add_folder())
        # coming from Koofr Desktop: its folders, taken over in one go
        self.import_button = Gtk.Button(label=_("Import from Koofr Desktop"), halign=Gtk.Align.CENTER,
                                        css_classes=["pill"], visible=False)
        self.import_button.connect("clicked", lambda *_: self.import_koofr_desktop())
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        buttons.append(add)
        buttons.append(self.import_button)
        empty.set_child(buttons)
        self.stack.add_named(empty, "empty")
        self.list_page = Adw.PreferencesPage()
        self.overall = OverallStatus(self)
        group = Adw.PreferencesGroup()
        group.add(self.overall)
        self.list_page.add(group)
        # the files of the folder that syncs now, under its status
        self.activity = ActivityList(self.engine, lambda: self.engine.active.pair_id if self.engine.active else None)
        self.list_page.add(self.activity)
        self.stack.add_named(self.list_page, "list")
        view.set_content(self.stack)
        return Adw.NavigationPage(title=APP_NAME, tag="folders", child=view)

    def update_overall(self) -> None:
        self.overall.update()
        self.banner.update()

    def add_folder(self) -> None:
        from .add_folder import AddFolderDialog
        AddFolderDialog(self).present(self)

    def import_koofr_desktop(self) -> None:
        if self.engine.creds is None:
            self.toast(_("Sign in to Koofr first."))
            return
        from .import_dialog import ImportDialog
        ImportDialog(self).present(self)

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
        self.update_overall()
        self.activity.update()

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
        if rt.status is Status.NEEDS_REPAIR and rt.gone:
            self.ask_gone(pair_id)
        elif rt.status is Status.NEEDS_REPAIR and self.engine.kept_listings(pair_id):
            self.ask_repair(pair_id)
        elif rt.status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
            self.open_preview(pair_id)
        elif verification_differences(rt.preview):
            VerifyDialog(self, pair_id).present(self)
        elif rt.conflicts:
            ConflictsDialog(self, pair_id).present(self)
        elif rt.refused:
            LeftOutDialog(self, pair_id).present(self)

    def ask_repair(self, pair_id: str) -> None:
        """Spola lost track of the last synced state, but a copy of it was
        kept after the last successful sync: going back to it and syncing
        normally keeps every safety check; a new alignment is the other way."""
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime.get(pair_id)
        kept = self.engine.kept_listings(pair_id) if pair is not None else []
        if pair is None or rt is None or not kept:
            return
        name, stamp = kept[0]
        dialog = Adw.AlertDialog(
            heading=_("Repair “{name}”?").format(name=pair.name),
            body=_("Spola lost track of the last synced state ({reason}).\n\n"
                   "It kept a copy of the state known at {time}, after the last successful sync: Spola can go "
                   "back to it and sync normally from there, the changes since then handled as usual, with the "
                   "same safety checks. Or a new alignment copies what is missing on each side and deletes "
                   "nothing; where both sides have a different version, the newer one wins.").format(
                reason=rt.message, time=when(stamp)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("restore", _("Go Back to That State"))
        dialog.add_response("align", _("New Alignment…"))
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("restore")

        def chosen(_d, response):
            if response == "restore":
                if not self.engine.restore_listings(pair_id, name):
                    self.toast(_("The kept state could not be put back: a new alignment is needed."))
            elif response == "align":
                self.open_preview(pair_id)
        dialog.connect("response", chosen)
        dialog.present(self)

    def ask_gone(self, pair_id: str) -> None:
        """One side has nothing left (gone from Koofr, emptied there or here):
        a repair cannot help as such, the user chooses what happened."""
        pair, rt = self.engine.cfg.pair(pair_id), self.engine.runtime.get(pair_id)
        if pair is None or rt is None or not rt.gone:
            return
        heading = {
            "koofr-gone": _("“{name}” Is No Longer on Koofr"),
            "koofr-empty": _("“{name}” Is Empty on Koofr"),
            "here-empty": _("“{name}” Is Empty on This Computer"),
        }[rt.gone].format(name=pair.name)
        body = rt.message + "\n\n" + (
            _("Spola stopped so as not to delete the same files here. If they were deleted on purpose, stop "
              "syncing this folder; otherwise bring them back.") if rt.gone != "here-empty" else
            _("Spola stopped so as not to delete the same files on Koofr. If they were deleted on purpose, stop "
              "syncing this folder; otherwise copy them here again."))
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Not Now"))
        dialog.add_response("remove", _("Stop Syncing"))
        if rt.gone == "here-empty":
            dialog.add_response("download", _("Copy Koofr's Files Here…"))
            dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("download")
        else:
            dialog.add_response("upload", _("Upload This Computer's Files…"))
            dialog.add_response("trash", _("Restore from Koofr's Trash…"))
            dialog.set_response_appearance("trash", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("trash")
        dialog.set_close_response("cancel")

        def chosen(_dialog, response):
            if response == "remove":
                self.engine.remove_pair(pair_id)          # no file is deleted, neither here nor on Koofr
            elif response == "trash":
                TrashDialog(self, pair_id).present(self)
            elif response == "download":
                self.open_preview(pair_id)
            elif response == "upload" and rt.gone == "koofr-gone":
                self.engine.make_koofr_folder(pair_id, lambda error: self.toast(error) if error
                                              else self.open_preview(pair_id))
            elif response == "upload":
                self.open_preview(pair_id)
        dialog.connect("response", chosen)
        dialog.present(self)

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
        elif rt.status is Status.NEEDS_REPAIR and rt.gone == "here-empty":
            PreviewDialog(self, pair_id, Mode.RESYNC, False, _("Copy Koofr's Files Here"),
                          _("The files on Koofr are copied to this computer again. Nothing is deleted."),
                          _("Copy")).present(self)
        elif rt.status is Status.NEEDS_REPAIR and rt.gone:
            PreviewDialog(self, pair_id, Mode.RESYNC, False, _("Upload This Computer's Files"),
                          _("The files of this computer go up to Koofr again. Nothing is deleted."),
                          _("Upload")).present(self)
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


# what makes a folder wait for the user, the most serious first
LOOK = (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.ERROR, Status.CONFLICTS,
        Status.SKIPPED, Status.NEEDS_SETUP)


def waiting_for_user(engine: Engine) -> list[str]:
    """The folders that wait for the user (not paused ones), the most
    serious first."""
    found = [(LOOK.index(engine.runtime[p.id].status), p.name.casefold(), p.id) for p in engine.cfg.pairs
             if p.id in engine.runtime and not p.paused and engine.runtime[p.id].status in LOOK]
    return [pair_id for _rank, _name, pair_id in sorted(found)]


class AttentionBanner:
    """What waits for the user, under the header bar: the folder, or how
    many, and the one button that deals with it; when nothing does, a Koofr
    account close to full. A banner of its own, so
    that the status card keeps saying what Spola is doing (a folder waiting
    for its alignment must not hide the others syncing), and so that it stays
    in view while the list scrolls. (Adw.Banner cannot be subclassed.)"""

    def __init__(self, window: MainWindow):
        self.window, self.engine = window, window.engine
        self.widget = Adw.Banner(revealed=False)
        if hasattr(self.widget, "set_use_markup"):             # libadwaita 1.4: the titles are escaped markup
            self.widget.set_use_markup(True)
        if hasattr(self.widget, "set_button_style"):           # libadwaita 1.7
            self.widget.set_button_style(Adw.BannerButtonStyle.SUGGESTED)
        self._do = None
        self.widget.connect("button-clicked", lambda *_: self._do() if self._do else None)

    def update(self) -> None:
        engine = self.engine
        waiting = waiting_for_user(engine) if engine.creds is not None else []
        if not waiting and engine.creds is not None and engine.low_space:
            # nothing to review, but the account is filling up: worth knowing
            # before a sync stops on a full Koofr (folders waiting come first)
            space = engine.space
            self.widget.set_title(escape(_("Space is running out on Koofr: {free} left of {total}").format(
                free=GLib.format_size(space.free), total=GLib.format_size(space.total))))
            self.widget.set_button_label(_("Open Koofr"))
            self._do = lambda: Gtk.UriLauncher.new(koofr_api.web_url(None, "/")).launch(self.window, None, None, None)
            self.widget.set_revealed(True)
            return
        self.widget.set_revealed(bool(waiting))
        if not waiting:
            return
        background = self._background_preview(waiting)
        if background is not None:
            # the preview the user sent to the background: it goes on, then it is ready
            pair_id, running = background
            name = engine.cfg.pair(pair_id).name
            self.widget.set_title(escape((_("Preparing the preview of “{name}”…") if running
                                          else _("The preview of “{name}” is ready")).format(name=name)))
            self.widget.set_button_label(_("Show") if running else _("Review"))
            self._do = lambda: self.window.review(pair_id)
            return
        pair_id = waiting[0]
        pair, status = engine.cfg.pair(pair_id), engine.runtime[pair_id].status
        if len(waiting) > 1:
            title = ngettext("{n} folder needs your attention", "{n} folders need your attention",
                             len(waiting)).format(n=len(waiting))
        elif status is Status.NEEDS_REPAIR and engine.runtime[pair_id].gone:
            title = {
                "koofr-gone": _("“{name}” is no longer on Koofr"),
                "koofr-empty": _("“{name}” is empty on Koofr"),
                "here-empty": _("“{name}” is empty on this computer"),
            }[engine.runtime[pair_id].gone].format(name=pair.name)
        else:
            title = {
                Status.NEEDS_REPAIR: _("“{name}” needs a repair"),
                Status.NEEDS_CONFIRMATION: _("“{name}” is waiting for your confirmation"),
                Status.NEEDS_SETUP: _("“{name}” is waiting for its first alignment"),
                Status.CONFLICTS: _("Conflicts in “{name}”"),
                Status.SKIPPED: _("Some files in “{name}” are not synced"),
                Status.ERROR: _("“{name}” cannot be synced"),
            }[status].format(name=pair.name)
        self.widget.set_title(escape(title))
        if status is Status.ERROR and len(waiting) == 1:
            self.widget.set_button_label(_("Try Again"))
            self._do = lambda: engine.request_sync(pair_id, "manual")
        else:
            # the most serious first; the next one after it
            self.widget.set_button_label(_("Review"))
            self._do = lambda: self.window.review(pair_id)


    def _background_preview(self, waiting: list[str]) -> tuple[str, bool] | None:
        """(pair id, still running) of a review the user asked for and left
        to run with its dialog closed, or that is ready and not seen yet: the
        running ones first."""
        found = []
        for pair_id in waiting:
            preview = self.engine.runtime[pair_id].preview
            if (preview is None or preview.reason != "preview" or preview.listeners
                    or self.engine.review_args(pair_id) != (preview.mode, preview.force)):
                continue
            if preview.running:
                found.append((0, pair_id, True))
            elif preview.fresh() and not preview.seen:
                found.append((1, pair_id, False))
        return min(found)[1:] if found else None


class OverallStatus(Gtk.Box):
    """What Spola is doing, at the top of the list: a coloured icon, the
    state, one line of detail, the progress while syncing and, when it
    matters, one action. What waits for the user is in the banner above."""

    def __init__(self, window: MainWindow):
        super().__init__(spacing=16, css_classes=["card"])
        self.window, self.engine = window, window.engine
        self.icon = Gtk.Image(pixel_size=48, valign=Gtk.Align.CENTER, margin_start=18, margin_top=18,
                              margin_bottom=18, accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self.append(self.icon)
        self.texts = texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                                     hexpand=True, margin_top=14, margin_bottom=14)
        # One line each and the bar's room always kept: the card keeps its
        # height whatever the state, so the list below never jumps
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["title-3"])
        self.detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label"])
        self.progress = Gtk.ProgressBar(margin_top=6, opacity=0, can_target=False)
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
        self.detail.set_label(detail or " ")
        self.set_tooltip_text(f"{title}\n{detail}" if detail else title)
        self.action.set_visible(bool(action))
        self.texts.set_margin_end(0 if action else 18)      # the progress bar keeps off the edge
        self.action.set_label(action)
        self.action.set_css_classes(["suggested-action"] if suggested else [])
        self._do = do

    def update(self) -> None:
        engine, cfg = self.engine, self.engine.cfg
        pairs = [p for p in cfg.pairs if p.id in engine.runtime]
        self.progress.set_opacity(0)
        if not pairs or engine.creds is None:
            return
        task = engine.active
        if task is not None and not task.dry_run and cfg.pair(task.pair_id):
            pair, rt = cfg.pair(task.pair_id), engine.runtime[task.pair_id]
            text, fraction = progress_text(rt.stats, rt.activity.deletes_queued)
            self._show(Status.SYNCING, _("Syncing “{name}”").format(name=pair.name), text or _("Comparing the two sides…"))
            self.progress.set_opacity(1)
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
        waiting = waiting_for_user(engine)
        others = [p for p in active if p.id not in waiting]
        if not others:
            # every folder waits for the user: the banner says what to do
            names = [cfg.pair(pid).name for pid in waiting]
            self._show(engine.runtime[waiting[0]].status, _("Waiting for You"),
                       ", ".join(names[:3]) + (" \u2026" if len(names) > 3 else ""))
            return
        synced = [engine.runtime[p.id].last_sync for p in others if engine.runtime[p.id].last_sync]
        detail = ngettext("{n} folder", "{n} folders", len(others)).format(n=len(others))
        if synced:
            detail += " · " + _("last sync {time}").format(time=when(max(synced)))
        paused = len(pairs) - len(active)
        if paused:
            detail += " · " + ngettext("{n} paused", "{n} paused", paused).format(n=paused)
        self._show(Status.OK, _("Everything Else Is Up to Date") if waiting else _("Everything Is Up to Date"), detail)


class ActivityList(Adw.PreferencesGroup):
    """The files of the run in progress, four rows at a time: the copies
    going on (with their progress), the latest done, the next waiting. The
    rows are always the same four, so the page below does not jump while
    files come and go; the group shows only while a folder really syncs."""

    ROWS = 4
    # the theme's own: they follow the user's icon theme (Adwaita's go-up and
    # go-down are chevrons, which read as "expand", not as a transfer)
    ICONS = {activity.DOWNLOAD: "folder-download-symbolic", activity.UPLOAD: "send-to-symbolic",
             activity.DELETE_HERE: "user-trash-symbolic", activity.DELETE_KOOFR: "user-trash-symbolic",
             activity.CONFLICT: "dialog-warning-symbolic",
             activity.MOVE_KOOFR: "go-jump-symbolic", activity.MOVE_HERE: "go-jump-symbolic"}

    def __init__(self, engine: Engine, pair_id_of):
        super().__init__(title=pgettext("activity", "Now Syncing"), visible=False)
        self.engine, self.pair_id_of = engine, pair_id_of
        self.slots = []
        for _i in range(self.ROWS):
            row = Adw.ActionRow(title_lines=1, subtitle_lines=1)
            icon = Gtk.Image(accessible_role=Gtk.AccessibleRole.PRESENTATION)
            row.add_prefix(icon)
            done = Gtk.Image(icon_name="object-select-symbolic", css_classes=["dim-label"],
                             accessible_role=Gtk.AccessibleRole.PRESENTATION)
            row.add_suffix(done)
            self.add(row)
            self.slots.append((row, icon, done))

    @staticmethod
    def describe(op: activity.Op) -> str:
        size = GLib.format_size(op.size) if op.size else ""
        if op.state == activity.ACTIVE and op.kind in (activity.DOWNLOAD, activity.UPLOAD):
            fraction = op.fraction
            doing = _("Downloading") if op.kind == activity.DOWNLOAD else _("Uploading")
            if fraction is None:
                return doing
            return _("{doing} · {percent}% of {size}").format(doing=doing, percent=int(fraction * 100), size=size)
        texts = {
            (activity.DOWNLOAD, activity.WAITING): _("Waiting to download"),
            (activity.DOWNLOAD, activity.DONE): _("Downloaded"),
            (activity.DOWNLOAD, activity.FAILED): _("Not downloaded"),
            (activity.UPLOAD, activity.WAITING): _("Waiting to upload"),
            (activity.UPLOAD, activity.DONE): _("Uploaded"),
            (activity.UPLOAD, activity.FAILED): _("Not uploaded"),
            (activity.DELETE_HERE, activity.WAITING): _("Deleted on Koofr: to Spola's backup"),
            (activity.DELETE_HERE, activity.DONE): _("Deleted on Koofr: moved to Spola's backup"),
            (activity.DELETE_HERE, activity.FAILED): _("Could not be moved to Spola's backup"),
            (activity.DELETE_KOOFR, activity.WAITING): _("Deleted here: to Koofr's trash"),
            (activity.DELETE_KOOFR, activity.DONE): _("Deleted here: moved to Koofr's trash"),
            (activity.DELETE_KOOFR, activity.FAILED): _("Not deleted on Koofr"),
            (activity.MOVE_KOOFR, activity.DONE): _("Moved here: moved on Koofr too"),
            (activity.MOVE_HERE, activity.DONE): _("Moved on Koofr: moved here too"),
        }
        if op.kind == activity.CONFLICT:
            return _("Changed on both sides: both versions kept")
        return texts.get((op.kind, op.state), "")

    def update(self) -> None:
        pair_id = self.pair_id_of()
        rt = self.engine.runtime.get(pair_id) if pair_id else None
        ops = rt.activity.rows(self.ROWS) if rt is not None and syncing_for_real(self.engine, pair_id) else []
        self.set_visible(bool(ops))
        for i, (row, icon, done) in enumerate(self.slots):
            op = ops[i] if i < len(ops) else None
            row.set_visible(op is not None)
            if op is None:
                continue
            row.set_title(escape(os.path.basename(op.path) or op.path))
            row.set_subtitle(escape(self.describe(op)))
            row.set_tooltip_text(f"{op.origin} → {op.path}" if op.origin else op.path)
            icon.set_from_icon_name(self.ICONS.get(op.kind, "text-x-generic-symbolic"))
            icon.set_css_classes(["error"] if op.state == activity.FAILED else
                                 ["warning"] if op.kind == activity.CONFLICT else
                                 ["dim-label"] if op.state in (activity.DONE, activity.WAITING) else [])
            done.set_visible(op.state == activity.DONE)


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
        header = header_bar()
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
        self.activity = ActivityList(self.engine, lambda: self.pair_id)
        page.add(self.activity)

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
        rules = [("", _("As in Preferences")), (conflicts.ASK, _("Ask Me")),
                 (conflicts.NEWER, _("Keep the Newer Version")), (conflicts.BOTH, _("Keep Both Versions")),
                 (conflicts.LOCAL, _("Keep This Computer's Version")), (conflicts.KOOFR, _("Keep Koofr's Version"))]
        rule = Adw.ComboRow(title=_("When a File Changes on Both Sides"))
        rule.set_model(Gtk.StringList.new([label for _code, label in rules]))
        rule.set_selected(next((i for i, (code, _l) in enumerate(rules) if code == pair.conflict_rule), 0))
        rule.connect("notify::selected",
                     lambda r, _p: self.engine.set_conflict_rule(self.pair_id, rules[r.get_selected()][0]))
        group.add(rule)
        self.conflicts_row = self._link_row(group, _("Conflicts"),
                                            lambda: ConflictsDialog(self.window, self.pair_id).present(self.window))
        self.left_out_row = self._link_row(group, _("Files Not Synced"),
                                           lambda: LeftOutDialog(self.window, self.pair_id).present(self.window))
        self.preview_row = self._link_row(group, _("Preview Next Sync"), self._preview,
                                          _("What the next sync would do, without changing anything"))
        self.verify_row = self._link_row(group, _("Verify Both Sides"),
                                         lambda: VerifyDialog(self.window, self.pair_id).present(self.window),
                                         _("Compare sizes and checksums here and on Koofr. Nothing is changed."))
        self.exclusions_row = self._link_row(group, _("Exclusions"),
                                             lambda: ExclusionsDialog(self.window, self.pair_id).present(self.window))
        self.trash_row = self._link_row(group, _("Restore from Koofr's Trash"),
                                        lambda: TrashDialog(self.window, self.pair_id).present(self.window),
                                        _("Files of this folder deleted on Koofr in the last 30 days"))
        self.backups_row = self._link_row(group, _("Local Backups"),
                                          lambda: BackupsDialog(self.window, self.pair_id).present(self.window),
                                          _("Files replaced or deleted here by past syncs, kept for a while"))
        self.log_row = self._link_row(group, _("Last Sync Log"), self._open_log,
                                      _("What rclone reported during the last sync, for troubleshooting"))
        if windows_reserved_names_matter():
            group.add(Adw.ActionRow(title=_("Names Windows 10 Reserves"), subtitle_lines=0, subtitle=escape(_(
                "Files and folders named CON, PRN, AUX, NUL, COM1 to COM9 or LPT1 to LPT9, with any extension, "
                "stay on Koofr only: Windows 10 takes these names for devices."))))
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
        self.status_row.set_title(escape(pair_status_text(self.engine, self.pair_id)))
        self.status_row.set_subtitle(escape(status_detail(self.engine, self.pair_id, short=False)))
        self.activity.update()
        self.icon.show(rt.status)
        syncing = syncing_for_real(self.engine, self.pair_id)
        self.progress_row.set_visible(syncing)
        if syncing:
            fraction = progress_text(rt.stats, rt.activity.deletes_queued)[1]
            if fraction is None:
                self.progress.pulse()
            else:
                self.progress.set_fraction(fraction)

        signed_in = self.engine.creds is not None
        if rt.status is Status.NEEDS_SETUP:
            label, css, sensitive = _("Align…"), ["suggested-action"], signed_in
        elif rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION, Status.CONFLICTS, Status.SKIPPED):
            label, css, sensitive = _("Review…"), ["suggested-action"], True
        elif rt.status is Status.SYNCING and rt.run is not None and rt.run.stopping:
            label, css, sensitive = _("Stopping…"), [], False
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
        self.trash_row.set_visible(pair.initialized)
        self.trash_row.set_sensitive(signed_in)
        self.preview_row.set_sensitive(rt.status is not Status.SYNCING and signed_in)
        self.verify_row.set_visible(pair.initialized)
        self.verify_row.set_sensitive(signed_in)
        self.verify_row.set_subtitle(escape(verification_summary(rt.preview)
                                            or _("Compare sizes and checksums here and on Koofr. Nothing is changed.")))
        self.log_row.set_visible(newest_log(self.engine.paths.log_dir(self.pair_id)) is not None)
        self.backups_row.set_visible(self.engine.paths.backup_dir(self.pair_id).is_dir())
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

    def _open_log(self) -> None:
        log = newest_log(self.engine.paths.log_dir(self.pair_id))
        if log is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(log)))

        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                # no application for a .jsonl file: the folder, with the file selected
                launcher.open_containing_folder(self.window, None, None, None)

        launcher.launch(self.window, None, done)

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
        header = Adw.HeaderBar()
        self.resolve_all = Gtk.Button(label=_("Resolve All…"))
        self.resolve_all.connect("clicked", lambda *_: ResolveAllDialog(self.window, self.pair_id,
                                                                        self._resolved).present(self))
        header.pack_start(self.resolve_all)
        view.add_top_bar(header)
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
        self.resolve_all.set_visible(bool(items))
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
            self.toasts.add_toast(Adw.Toast(title=escape(str(e)), timeout=6))

    def _resolved(self, done: int, errors: list[str]) -> None:
        self._fill()
        tell(self.toasts, ngettext("{n} conflict resolved", "{n} conflicts resolved", done).format(n=done) if done else "",
             errors)


def tell(toasts: Adw.ToastOverlay, done: str, errors: list[str]) -> None:
    """The outcome of an action on many files: what was done, the first
    problem and how many more."""
    if done:
        toasts.add_toast(Adw.Toast(title=escape(done), timeout=4))
    if errors:
        more = len(errors) - 1
        text = errors[0] if not more else ngettext("{error} (and {n} more)", "{error} (and {n} more)", more).format(
            error=errors[0], n=more)
        toasts.add_toast(Adw.Toast(title=escape(text), timeout=8))


class ResolveAllDialog(Adw.Dialog):
    """Every conflict of a folder with one rule. What happens to each file is
    listed before anything is done; what the rule cannot decide is left to
    the user."""

    ICONS = {conflicts.LOCAL: "computer-symbolic", conflicts.KOOFR: "folder-remote-symbolic",
             conflicts.BOTH: "edit-copy-symbolic", None: "dialog-question-symbolic"}

    def __init__(self, window: MainWindow, pair_id: str, on_done):
        super().__init__(title=_("Resolve All Conflicts"), content_width=560, content_height=620)
        self.engine, self.pair_id, self.on_done = window.engine, pair_id, on_done
        self.decisions: list[conflicts.Decision] = []
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.confirm = Gtk.Button(label=_("Resolve"))
        self.confirm.connect("clicked", lambda *_: self._resolve())
        header.pack_end(self.confirm)
        view.add_top_bar(header)

        self.page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Which Version to Keep"), description=_(
            "A version that is not kept goes to the wastebasket here and to Koofr's trash, "
            "where you can get it back."))
        first = None
        for rule, title, subtitle in ((conflicts.NEWER, _("The Newer One"), _("By modification date")),
                                      (conflicts.LOCAL, _("This Computer's"), ""),
                                      (conflicts.KOOFR, _("Koofr's"), ""),
                                      (conflicts.BOTH, _("Both, Renamed"), _("Nothing goes to the wastebasket"))):
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=first)
            first = first or check
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable_widget=check)
            row.add_prefix(check)
            check.connect("toggled", lambda c, r=rule: self._show(r) if c.get_active() else None)
            group.add(row)
        self.page.add(group)
        self.list_group: Adw.PreferencesGroup | None = None
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        self.set_child(view)
        first.set_active(True)

    def _show(self, rule: str) -> None:
        self.decisions = [conflicts.decide(c, rule) for c in self.engine.conflicts(self.pair_id)]
        if self.list_group is not None:
            self.page.remove(self.list_group)
        decided = sum(1 for d in self.decisions if d.choice)
        left = len(self.decisions) - decided
        self.list_group = Adw.PreferencesGroup(title=_("What Happens"))
        if left:
            self.list_group.set_description(ngettext(
                "{n} file is left to you: the rule cannot decide it.",
                "{n} files are left to you: the rule cannot decide them.", left).format(n=left))
        for decision in self.decisions[:PREVIEW_LIMIT]:
            row = Adw.ActionRow(title=escape(decision.conflict.original), title_lines=2,
                                subtitle=escape(decision.reason), subtitle_lines=2)
            row.add_prefix(Gtk.Image(icon_name=self.ICONS[decision.choice]))
            self.list_group.add(row)
        if len(self.decisions) > PREVIEW_LIMIT:
            self.list_group.add(Adw.ActionRow(title=_("…and {n} more").format(n=len(self.decisions) - PREVIEW_LIMIT)))
        self.page.add(self.list_group)
        self.confirm.set_sensitive(decided > 0)
        drops = any(d.choice in (conflicts.LOCAL, conflicts.KOOFR) for d in self.decisions)
        self.confirm.set_css_classes(["destructive-action" if drops else "suggested-action"])

    def _resolve(self) -> None:
        try:
            done, errors = self.engine.resolve_conflicts(self.pair_id, self.decisions)
        except conflicts.ResolveError as e:
            self.toasts.add_toast(Adw.Toast(title=escape(str(e)), timeout=6))
            return
        self.close()
        self.on_done(done, errors)


class TrashDialog(Adw.Dialog):
    """What was deleted on Koofr in this folder and is still in Koofr's
    trash, one row per deletion (the files deleted together): the one chosen
    comes back, never the whole trash. Closing it lets the restore go on."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Restore from Koofr's Trash"), content_width=560, content_height=520)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        self.chosen = None
        self.running = self.closed = False
        self.connect("closed", lambda *_: setattr(self, "closed", True))
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.restore = Gtk.Button(label=_("Restore"), css_classes=["suggested-action"], sensitive=False)
        self.restore.connect("clicked", lambda *_: self._restore())
        header.pack_end(self.restore)
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        self.set_child(view)
        self.group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=_("Reading Koofr's trash…"))
        row.add_prefix(Adw.Spinner())
        self.group.add(row)
        self.page.add(self.group)
        self.engine.trash_deletions(pair_id, self._listed)

    def _replace(self, group: Adw.PreferencesGroup) -> None:
        self.page.remove(self.group)
        self.group = group
        self.page.add(group)

    def _listed(self, found: list, error: str) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        if pair is None:
            return
        group = Adw.PreferencesGroup(description=_(
            "Files of this folder deleted on Koofr and still in its trash, grouped by when they were deleted. "
            "Only the deletion you choose comes back."))
        if error:
            group.add(Adw.ActionRow(title=_("Koofr's trash could not be read"), subtitle=escape(error)))
        elif not found:
            group.add(Adw.ActionRow(title=_("Nothing of this folder is in Koofr's trash.")))
        first = None
        for deletion in found:
            moment = GLib.DateTime.new_from_unix_local(deletion.deleted // 1000).format("%-d %b %Y, %H:%M")
            n = len(deletion.entries)
            parts = [ngettext("{n} file", "{n} files", n).format(n=count(n)), GLib.format_size(deletion.size)]
            folders = deletion.folders(pair.remote)
            if folders:
                parts.append(", ".join(folders) + (" …" if len(folders) == 3 else ""))
            row = Adw.ActionRow(title=moment, subtitle=escape(" · ".join(parts)), activatable=True)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=first)
            first = first or check
            check.connect("toggled", lambda c, d=deletion: self._choose(d) if c.get_active() else None)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            group.add(row)
        self._replace(group)

    def _choose(self, deletion) -> None:
        self.chosen = deletion
        self.restore.set_sensitive(not self.running)

    def _restore(self) -> None:
        deletion = self.chosen
        if deletion is None:
            return
        started = self.engine.restore_from_trash(self.pair_id, deletion.entries, self._progress, self._done)
        if not started:
            tell(self.toasts, _("Wait for the sync of this folder to finish, then try again."), [])
            return
        self.running = True
        self.restore.set_sensitive(False)
        group = Adw.PreferencesGroup(description=_(
            "Koofr restores about a thousand files a minute. You can close this window: the restore goes on, "
            "and the folder does not sync until it is done."))
        self.progress_row = Adw.ActionRow(title=_("Restoring…"))
        self.progress_row.add_prefix(Adw.Spinner())
        group.add(self.progress_row)
        self.bar = Gtk.ProgressBar(margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        group.add(Adw.PreferencesRow(child=self.bar, activatable=False, focusable=False))
        stop = Adw.ButtonRow(title=_("Stop"))
        stop.connect("activated", lambda *_: self.engine.cancel_restore(self.pair_id))
        group.add(stop)
        self._replace(group)

    def _progress(self, done: int, total: int) -> None:
        if self.running and total:
            self.bar.set_fraction(done / total)
            self.progress_row.set_title(escape(_("Restoring: {n} of {total} files").format(
                n=count(done), total=count(total))))

    def _done(self, result, reuploads: int, error: str) -> None:
        self.running = False
        lines = []
        if error:
            lines.append(_("The restore stopped: {error}").format(error=error))
        if result is not None:
            lines.append(ngettext("{n} file restored.", "{n} files restored.", result.restored)
                         .format(n=count(result.restored)))
            if result.left:
                lines.append(ngettext("{n} file is still in the trash: try again.",
                                      "{n} files are still in the trash: try again.", len(result.left))
                             .format(n=count(len(result.left))))
            if result.cancelled:
                lines.append(_("Stopped: the rest is still in the trash."))
        if reuploads:
            lines.append(ngettext(
                "Koofr gives restored files today's date. The {n} file identical to the one on this computer is "
                "uploaded again with its own date: if it is the whole folder, Spola asks you to confirm first.",
                "Koofr gives restored files today's date. The {n} files identical to those on this computer are "
                "uploaded again with their own dates: if they are the whole folder, Spola asks you to confirm "
                "first.", reuploads).format(n=count(reuploads)))
        group = Adw.PreferencesGroup()
        for line in lines:
            group.add(Adw.ActionRow(title=escape(line), title_lines=0))
        self._replace(group)
        if self.closed and lines:       # closed meanwhile: the window says it
            self.window.toast(lines[0])


def moment(timestamp: float) -> str:
    """"16 Oct 23:11": a backup's run, two of one day told apart."""
    return GLib.DateTime.new_from_unix_local(int(timestamp)).format("%-d %b %H:%M")


class BackupsDialog(Adw.Dialog):
    """This pair's local backups, one row per sync that replaced or deleted
    files here: open a run as a tree, put a file back (the one in its place
    now kept in a new backup first), delete a run or all of them."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Local Backups"), content_width=560, content_height=560)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.delete_all = Gtk.Button(label=_("Delete All…"), sensitive=False)
        self.delete_all.connect("clicked", lambda *_: self._delete_all())
        header.pack_start(self.delete_all)
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.group: Adw.PreferencesGroup | None = None
        self.runs: list = []
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(title=_("Local Backups"), child=view))
        self.set_child(self.nav)
        self._fill()

    def _fill(self) -> None:
        if self.group is not None:
            self.page.remove(self.group)
        self.group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=_("Looking at the backups…"))
        row.add_prefix(Adw.Spinner())
        self.group.add(row)
        self.page.add(self.group)
        paths, pair_id = self.engine.paths, self.pair_id
        run_in_thread(lambda: jobs.backup_runs(paths, pair_id), self._listed)

    def _listed(self, runs, error) -> None:
        if self.group is not None:
            self.page.remove(self.group)
        self.runs = runs or []
        days = self.engine.cfg.backup_days
        self.group = Adw.PreferencesGroup(description=escape(ngettext(
            "A sync that replaces or deletes a file here keeps the old copy for {n} day, in a folder of its own "
            "for each sync. Nothing on Koofr is touched from here.",
            "A sync that replaces or deletes a file here keeps the old copy for {n} days, in a folder of its own "
            "for each sync. Nothing on Koofr is touched from here.", days).format(n=count(days))))
        if error is not None:
            self.group.add(Adw.ActionRow(title=escape(str(error)), title_lines=3))
        elif not self.runs:
            self.group.add(Adw.ActionRow(title=_("No backup at the moment.")))
        for run in self.runs:
            row = Adw.ActionRow(title=escape(moment(run.time)), subtitle=escape(plan_tree.amount(run.files, run.size)),
                                activatable=True)
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text=_("Delete This Backup"))
            delete.connect("clicked", lambda _b, r=run: self._delete(r))
            row.add_suffix(delete)
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION))
            row.connect("activated", lambda _r, r=run: self._open(r))
            self.group.add(row)
        self.delete_all.set_sensitive(bool(self.runs))
        self.page.add(self.group)

    def _open(self, run) -> None:
        files = jobs.backup_files(self.engine.paths, self.pair_id, run.name)
        nodes = filetree.build([(path, "", path) for path, _size in files], dict(files))
        self.nav.push(plan_tree.PlanTreePage(moment(run.time), nodes, lambda node, r=run: self._restore(r, node.path),
                                             _("Put it back in its place")))

    def _restore(self, run, rel: str) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        if pair is None:
            return
        there = os.path.exists(os.path.join(pair.local, rel))
        dialog = Adw.AlertDialog(
            heading=_("Put “{name}” back?").format(name=os.path.basename(rel)),
            body=(_("The copy of {time} goes back to its place in the folder. The file there now is kept in a new "
                    "backup first. The next sync uploads it to Koofr.") if there else
                  _("The copy of {time} goes back to its place in the folder. The next sync uploads it to Koofr."))
            .format(time=moment(run.time)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("restore", _("Put It Back"))
        dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)

        def chosen(_d, response):
            if response != "restore":
                return
            try:
                jobs.restore_backup(self.engine.paths, pair, run.name, rel)
            except (OSError, ValueError) as e:
                tell(self.toasts, "", [str(e)])
                return
            self.engine.invalidate_preview(self.pair_id)
            tell(self.toasts, _("Put back: {path}").format(path=rel), [])
        dialog.connect("response", chosen)
        dialog.present(self)

    def _delete(self, run) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete the backup of {time}?").format(time=moment(run.time)),
            body=_("{amount} of files replaced or deleted by that sync. Nothing else is touched.").format(
                amount=plan_tree.amount(run.files, run.size)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        paths, pair_id = self.engine.paths, self.pair_id
        dialog.connect("response", lambda _d, r: run_in_thread(
            lambda: jobs.delete_backup_run(paths, pair_id, run.name), lambda _r, _e: self._fill()) if r == "delete"
            else None)
        dialog.present(self)

    def _delete_all(self) -> None:
        files, size = sum(r.files for r in self.runs), sum(r.size for r in self.runs)
        dialog = Adw.AlertDialog(
            heading=_("Delete every backup of this folder?"),
            body=_("{amount} of files replaced or deleted by past syncs. Nothing else is touched.").format(
                amount=plan_tree.amount(files, size)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete All"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        paths, pair_id, runs = self.engine.paths, self.pair_id, list(self.runs)

        def wipe():
            for run in runs:
                jobs.delete_backup_run(paths, pair_id, run.name)
        dialog.connect("response", lambda _d, r: run_in_thread(wipe, lambda _r, _e: self._fill()) if r == "delete"
                       else None)
        dialog.present(self)


def verification_differences(preview) -> int:
    """How many files a kept verification found different, 0 for none or
    for no verification."""
    if preview is None or preview.mode is not Mode.CHECK or preview.outcome is None:
        return 0
    plan = preview.outcome.plan
    return len(plan.upload) + len(plan.download) + len(plan.conflicts) + len(plan.error_paths)


def verification_summary(preview) -> str:
    """"14:20 · the same on both sides", or how many differences; "" when
    there is no verification to tell about."""
    if preview is None or preview.mode is not Mode.CHECK or preview.outcome is None:
        return ""
    n = verification_differences(preview)
    what = ngettext("{n} difference", "{n} differences", n).format(n=count(n)) if n else _("the same on both sides")
    return f"{when(preview.finished)} · {what}"


class VerifyDialog(Adw.Dialog):
    """Both sides compared by size and checksum (verify.py): the same, or
    what differs, each kind on a tree page of its own. Nothing is changed;
    the preview of the next sync says what it would do about them."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Verify Both Sides"), content_width=560, content_height=560)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        self.waiting = False
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.again = Gtk.Button(label=_("Verify Again"), sensitive=False)
        self.again.connect("clicked", lambda *_: self._start(refresh=True))
        header.pack_start(self.again)
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.spinner_group = Adw.PreferencesGroup()
        self.progress_row = Adw.ActionRow(title=_("Comparing…"), subtitle_lines=0)
        self.progress_row.add_prefix(Adw.Spinner())
        self.spinner_group.add(self.progress_row)
        self.result_group: Adw.PreferencesGroup | None = None
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(title=_("Verify Both Sides"), child=view))
        self.set_child(self.nav)
        self._handler = self.engine.connect("pair-changed",
                                            lambda _e, pid: self._changed() if pid == pair_id else None)
        self.connect("closed", lambda *_: self.engine.disconnect(self._handler))
        self._start()

    def _start(self, refresh: bool = False) -> None:
        if self.result_group is not None:
            self.page.remove(self.result_group)
            self.result_group = None
        if self.spinner_group.get_parent() is None:
            self.page.add(self.spinner_group)
        self.again.set_sensitive(False)
        self.waiting = True
        self.engine.preview(self.pair_id, Mode.CHECK, False, self._done, reason="verify", refresh=refresh)
        self._changed()

    def _changed(self) -> None:
        rt = self.engine.runtime.get(self.pair_id)
        preview = rt.preview if rt else None
        if not self.waiting or preview is None or preview.mode is not Mode.CHECK or not preview.running:
            return
        if rt.task is not preview.task:
            text = (_("Waiting for this folder's sync to finish…") if rt.task is not None
                    else _("Waiting for another preview to finish…"))
        elif rt.run is None:
            text = _("Looking at the files on this computer…")
        else:
            checks = (preview.stats or {}).get("checks") or 0
            text = ngettext("{n} file compared", "{n} files compared", checks).format(n=count(checks))
        self.progress_row.set_subtitle(escape(text))

    def _done(self, outcome: Outcome | None, error: str) -> None:
        self.waiting = False
        self.again.set_sensitive(True)
        if self.spinner_group.get_parent() is not None:
            self.page.remove(self.spinner_group)
        group = Adw.PreferencesGroup()
        if outcome is None:
            row = Adw.ActionRow(title=escape(error or _("The verification failed")), title_lines=3)
            row.add_prefix(Gtk.Image(icon_name="dialog-error-symbolic"))
            group.add(row)
        else:
            plan = outcome.plan
            kinds = ((_("Only on this computer"), plan.upload, False), (_("Only on Koofr"), plan.download, True),
                     (_("Different"), plan.conflicts, False), (_("Could not be compared"), plan.error_paths, False))
            if not any(items for _t, items, _k in kinds):
                row = Adw.ActionRow(title=escape(ngettext("{n} file, the same on both sides",
                                                          "{n} files, the same on both sides", outcome.identical)
                                                 .format(n=count(outcome.identical))),
                                    subtitle=escape(_("Sizes and checksums match. Nothing was changed.")))
                row.add_prefix(Gtk.Image(icon_name="emblem-ok-symbolic"))
                group.add(row)
            else:
                group.set_description(escape(ngettext(
                    "{n} file is the same on both sides; these differ. Nothing was changed: the preview of "
                    "the next sync says what it would do about them.",
                    "{n} files are the same on both sides; these differ. Nothing was changed: the preview of "
                    "the next sync says what it would do about them.", outcome.identical)
                    .format(n=count(outcome.identical))))
                for title, items, on_koofr in kinds:
                    if not items:
                        continue
                    nodes = filetree.build([(path, "", path) for path in items])
                    files, _size = filetree.totals(nodes)
                    row = Adw.ActionRow(title=title, subtitle=escape(plan_tree.amount(files, 0)), activatable=True)
                    row.add_suffix(Gtk.Image(icon_name="go-next-symbolic",
                                             accessible_role=Gtk.AccessibleRole.PRESENTATION))
                    row.connect("activated", lambda _r, t=title, n=nodes, k=on_koofr: self.nav.push(
                        plan_tree.PlanTreePage(t, n, lambda node, k=k: self._show(node.show, k),
                                               _("Open on Koofr") if k else _("Show in the file manager"))))
                    group.add(row)
            rt = self.engine.runtime.get(self.pair_id)
            if rt is not None and rt.preview is not None and rt.preview.stale:
                group.add(Adw.ActionRow(title=escape(_("Files changed since: verify again for the current picture.")),
                                        title_lines=2))
        self.result_group = group
        self.page.add(group)

    def _show(self, path: str, on_koofr: bool) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        if on_koofr:
            url = koofr_api.folder_url(self.engine.cfg.mount_id, pair.remote, path)
            Gtk.UriLauncher.new(url).launch(self.window, None, None, None)
        else:
            reveal(self.window, os.path.join(pair.local, path))


class LeftOutDialog(Adw.Dialog):
    """The files a run leaves out because Koofr refuses their names."""

    def __init__(self, window: MainWindow, pair_id: str):
        super().__init__(title=_("Files Not Synced"), content_width=560, content_height=480)
        self.window, self.engine, self.pair_id = window, window.engine, pair_id
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.rename_all = Gtk.Button(label=_("Rename All…"))
        self.rename_all.connect("clicked", lambda *_: self._rename_all())
        header.pack_start(self.rename_all)
        view.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.group: Adw.PreferencesGroup | None = None
        self.shown: tuple | None = None
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        self.set_child(view)
        self._handler = self.engine.connect("pair-changed", lambda _e, pid: self._fill() if pid == pair_id else None)
        self.connect("closed", lambda *_: self.engine.disconnect(self._handler))
        self._fill()

    def _fill(self) -> None:
        pair, rt = self.engine.cfg.pair(self.pair_id), self.engine.runtime.get(self.pair_id)
        if pair is None:
            return
        items = list(rt.refused) if rt else []
        if tuple(items) == self.shown:        # (the pair changes every second while it syncs)
            return
        self.shown = tuple(items)
        if self.group is not None:
            self.page.remove(self.group)
        self.group = Adw.PreferencesGroup(description=_(
            "Koofr does not accept these names, so Spola leaves them out and syncs the rest. "
            "Rename them and they are synced at the next run. Nothing is deleted."))
        if not items:
            self.group.add(Adw.ActionRow(title=_("Every name is accepted now.")))
        for refused in items:
            row = Adw.ActionRow(title=GLib.markup_escape_text(refused.path), title_lines=2, activatable=True,
                                subtitle=GLib.markup_escape_text(preflight.refused_reason(refused.code, refused.other)),
                                subtitle_lines=3, tooltip_text=_("Show in the file manager"))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if refused.is_dir else "action-unavailable-symbolic"))
            row.connect("activated", lambda _r, p=refused.path: reveal(self.window, os.path.join(pair.local, p)))
            self.group.add(row)
        self.page.add(self.group)
        self.rename_all.set_visible(any(new for _item, new in preflight.rename_plan(pair.local, items, pair.remote)))

    def _rename_all(self) -> None:
        RenameDialog(self.window, self.pair_id, list(self.shown or ()), self._renamed).present(self)

    def _renamed(self, done: int, errors: list[str]) -> None:
        self.shown = None
        self._fill()
        tell(self.toasts, ngettext("{n} file renamed", "{n} files renamed", done).format(n=done) if done else "",
             errors)


class RenameDialog(Adw.Dialog):
    """Names Koofr accepts for the files it refuses: listed first, renamed on
    this computer once confirmed, synced at the next run."""

    def __init__(self, window: MainWindow, pair_id: str, refused: list, on_done):
        super().__init__(title=_("Rename Files"), content_width=560, content_height=560)
        self.engine, self.pair_id, self.on_done = window.engine, pair_id, on_done
        pair = self.engine.cfg.pair(pair_id)
        self.plan = preflight.rename_plan(pair.local, refused, pair.remote)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        confirm = Gtk.Button(label=_("Rename"), css_classes=["suggested-action"],
                             sensitive=any(new for _item, new in self.plan))
        confirm.connect("clicked", lambda *_: self._rename())
        header.pack_end(confirm)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description=_(
            "Spola renames them on this computer so that Koofr accepts them, then syncs them. Nothing is deleted."))
        for item, new in self.plan[:PREVIEW_LIMIT]:
            if new:
                subtitle, icon = _("New name: “{name}”").format(name=os.path.basename(new)), "document-edit-symbolic"
            elif item.code == "long-path":
                subtitle, icon = _("No name to suggest: the path is too long, shorten it yourself."), "dialog-question-symbolic"
            else:
                subtitle, icon = _("No name to suggest: rename it yourself."), "dialog-question-symbolic"
            row = Adw.ActionRow(title=escape(item.path), title_lines=2, subtitle=escape(subtitle), subtitle_lines=2)
            row.add_prefix(Gtk.Image(icon_name=icon))
            group.add(row)
        if len(self.plan) > PREVIEW_LIMIT:
            group.add(Adw.ActionRow(title=_("…and {n} more").format(n=len(self.plan) - PREVIEW_LIMIT)))
        page.add(group)
        self.toasts = Adw.ToastOverlay(child=page)
        view.set_content(self.toasts)
        self.set_child(view)

    def _rename(self) -> None:
        try:
            done, errors = self.engine.rename_refused(self.pair_id, [(item.path, new) for item, new in self.plan if new])
        except conflicts.ResolveError as e:
            self.toasts.add_toast(Adw.Toast(title=escape(str(e)), timeout=6))
            return
        self.close()
        self.on_done(done, errors)


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
        awake = Adw.SwitchRow(title=_("Keep the computer awake while syncing"),
                              subtitle=_("No sleep, lid closed or not, until the sync is done"), active=cfg.keep_awake)
        awake.connect("notify::active", lambda r, _p: self._set("keep_awake", r.get_active()))
        group.add(awake)
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
        if WINDOWS:
            updates = Adw.SwitchRow(title=_("Check for updates"), subtitle=_("Once a day, from Spola's releases"),
                                    active=cfg.check_updates)
            updates.connect("notify::active", lambda r, _p: self._set("check_updates", r.get_active()))
            group.add(updates)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("File Manager"))
        if WINDOWS:
            status = Adw.SwitchRow(title=_("Show sync status in File Explorer"),
                                   subtitle=_("A tick on what is in sync, arrows on what waits for a sync, as for "
                                              "OneDrive; the synced folders in the navigation pane"),
                                   active=cfg.explorer_status)
            status.connect("notify::active", lambda r, _p: self._set_explorer_status(r.get_active()))
            group.add(status)
            menu = Adw.SwitchRow(title=_("Koofr entries in File Explorer's menu"),
                                 subtitle=_("“Open on Koofr” and “Copy Koofr Share Link”, in the synced folders"),
                                 active=cfg.explorer_menu)
            menu.connect("notify::active", lambda r, _p: self._set_explorer_menu(r.get_active()))
            group.add(menu)
        else:
            try:
                available = bool(file_managers.source_dir())
            except FileNotFoundError:
                available = False
            files = Adw.SwitchRow(title=_("Show sync status in the file manager"),
                                  subtitle=_("Emblems and a Koofr menu in Files, Nemo and Caja; the Koofr menu in "
                                             "Dolphin and Thunar. Restart the file manager to apply."),
                                  active=file_managers.installed(), sensitive=available)
            files.connect("notify::active", lambda r, _p: self._set_files(r.get_active()))
            group.add(files)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Backups"))
        days = Adw.SpinRow.new_with_range(1, 365, 1)
        days.set_title(_("Keep replaced and deleted files for"))
        days.set_subtitle(_("Days, in Spola’s local backup"))
        days.set_value(cfg.backup_days)
        # applied once the value settles, and, when it goes down and would
        # delete backups, after a word about them
        self._days_source = 0

        def days_changed(row, _p):
            if self._days_source:
                GLib.source_remove(self._days_source)
            self._days_source = GLib.timeout_add(800, lambda: self._backup_days(int(row.get_value()), row) or False)
        days.connect("notify::value", days_changed)
        group.add(days)
        page.add(group)

        group = Adw.PreferencesGroup(title=_("Conflicts"))
        rules = [(conflicts.ASK, _("Ask Me")), (conflicts.NEWER, _("Keep the Newer Version")),
                 (conflicts.BOTH, _("Keep Both Versions"))]
        rule = Adw.ComboRow(title=_("When a file changes on both sides"),
                            subtitle=_("Applied to new conflicts after each sync; a folder can have its own rule. "
                                       "A version that is not kept goes to the wastebasket."))
        rule.set_model(Gtk.StringList.new([label for _code, label in rules]))
        rule.set_selected(next((i for i, (code, _l) in enumerate(rules) if code == cfg.conflict_rule), 0))
        rule.connect("notify::selected", lambda r, _p: self._set("conflict_rule", rules[r.get_selected()][0]))
        group.add(rule)
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

    def _backup_days(self, days: int, row) -> None:
        self._days_source = 0
        cfg, paths = self.engine.cfg, self.engine.paths
        if days >= cfg.backup_days:
            self._set("backup_days", days)
            return
        runs = size = 0
        for pair in cfg.pairs:
            n, b = jobs.backups_older_than(paths, pair.id, days)
            runs, size = runs + n, size + b
        if not runs:
            self._set("backup_days", days)
            return
        dialog = Adw.AlertDialog(
            heading=_("Delete Older Backups?"),
            body=ngettext("{n} backup older than {days} days is deleted at once: {size} of files replaced or "
                          "deleted by past syncs.",
                          "{n} backups older than {days} days are deleted at once: {size} of files replaced or "
                          "deleted by past syncs.", runs).format(n=count(runs), days=days, size=GLib.format_size(size)))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def chosen(_d, response):
            if response == "delete":
                self._set("backup_days", days)
                self.engine.prune_now()
            else:
                row.set_value(cfg.backup_days)
        dialog.connect("response", chosen)
        dialog.present(self)

    def _set(self, name: str, value, conditions: bool = False) -> None:
        setattr(self.engine.cfg, name, value)
        self.engine.save_config()
        if conditions:
            self.engine.conditions_changed()

    def _set_explorer_menu(self, enabled: bool) -> None:
        self._set("explorer_menu", enabled)
        app = getattr(self.engine, "app", None)
        if app is not None and hasattr(app, "register_explorer"):
            app.register_explorer()         # written, or taken out, at once

    def _set_explorer_status(self, enabled: bool) -> None:
        self._set("explorer_status", enabled)
        status = getattr(getattr(self.engine, "app", None), "explorer_status", None)
        if status is not None:
            status.update()         # registered and marked, or taken out, at once

    def _set_files(self, enabled: bool) -> None:
        try:
            if enabled:
                file_managers.install()
            else:
                file_managers.uninstall()
        except OSError as e:
            self.add_toast(Adw.Toast(title=str(e), timeout=6))
            return
        self._set("nautilus_integration", enabled)
        # (no process check: inside the Flatpak the host's file manager is not visible)
        self.add_toast(Adw.Toast(title=_("Restart the file manager to apply (it keeps running until then)"),
                                 timeout=8))

    def _set_autostart(self, enabled: bool) -> None:
        self._set("autostart", enabled)
        app = getattr(self.engine, "app", None)
        autostart.apply(enabled, app.get_dbus_connection() if app else None)


class PreviewDialog(Adw.Dialog):
    """What a run would do, from a dry run the engine keeps: the dialog can be
    closed while it runs (it goes on, and Spola says when it is ready),
    opened again at once, or stopped for good with Cancel. `confirm_label`
    None means information only."""

    def __init__(self, window: MainWindow, pair_id: str, mode: Mode, force: bool,
                 title: str, explanation: str, confirm_label: str | None):
        super().__init__(title=title, content_width=560, content_height=600)
        self.window, self.engine = window, window.engine
        self.pair_id, self.mode, self.force = pair_id, mode, force
        self.waiting = self.closing = self.short_of_space = False
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=confirm_label is None,
                               show_start_title_buttons=False)
        if confirm_label:
            cancel = Gtk.Button(label=_("Cancel"))
            cancel.connect("clicked", lambda *_: self._cancel())
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
        # while it runs: how far it is, and the way out that lets it go on
        self.spinner_group = Adw.PreferencesGroup()
        self.spinner_row = Adw.ActionRow(title=_("Comparing the two folders…"),
                                         subtitle=_("Nothing is changed while the preview runs."), subtitle_lines=2)
        self.spinner_row.add_prefix(Adw.Spinner())
        self.spinner_group.add(self.spinner_row)
        background = Adw.ButtonRow(title=_("Continue in Background"))
        background.connect("activated", lambda *_: self.close())
        self.spinner_group.add(background)
        self.info_group: Adw.PreferencesGroup | None = None
        self.result_group: Adw.PreferencesGroup | None = None
        self.toasts = Adw.ToastOverlay(child=self.page)
        view.set_content(self.toasts)
        # each kind of change opens on a page of its own, as a tree of folders
        self.nav = Adw.NavigationView()
        self.nav.add(Adw.NavigationPage(title=title, child=view))
        self.set_child(self.nav)
        self.shown: Outcome | None = None          # the outcome on screen
        self.outdated_group: Adw.PreferencesGroup | None = None
        self._handler = self.engine.connect("pair-changed",
                                            lambda _e, pid: self._changed() if pid == pair_id else None)
        # the preview ages too: after its time, Confirm would act on an old picture
        self._tick = GLib.timeout_add_seconds(30, lambda: self._check_current() or True)
        self.connect("closed", lambda *_: self._closed())
        self._start()

    def _start(self, refresh: bool = False) -> None:
        for group in (self.info_group, self.result_group, self.outdated_group):
            if group is not None:
                self.page.remove(group)
        self.info_group = self.result_group = self.outdated_group = None
        self.shown = None
        if self.spinner_group.get_parent() is None:
            self.page.add(self.spinner_group)
        if self.confirm is not None:
            self.confirm.set_sensitive(False)
            self.confirm.set_css_classes(["suggested-action"])
        self.waiting = True
        self.engine.preview(self.pair_id, self.mode, self.force, self._done, refresh=refresh)
        self._progress()

    def _progress(self) -> None:
        """While it runs: waiting its turn, looking at this computer, then how
        many files and folders it has listed on both sides."""
        rt = self.engine.runtime.get(self.pair_id)
        preview = rt.preview if rt else None
        if not self.waiting or preview is None or not preview.running:
            return
        if rt.task is not preview.task:
            text = (_("Waiting for this folder's sync to finish…") if rt.task is not None
                    else _("Waiting for another preview to finish…"))
        elif rt.run is None:
            text = _("Looking at the files on this computer…")
        else:
            listed = int((preview.stats or {}).get("listed") or 0)
            text = (ngettext("{n} file or folder compared so far", "{n} files and folders compared so far",
                             listed).format(n=count(listed)) if listed else _("Listing the files on both sides…"))
        self.spinner_row.set_subtitle(escape(text + "\n" + _("Nothing is changed while the preview runs.")))

    def _changed(self) -> None:
        if self.waiting:
            self._progress()
        else:
            self._check_current()

    def _check_current(self) -> None:
        """Shown and finished: is the preview on screen still the one the
        engine keeps, fresh and not overtaken by a change? Otherwise Confirm
        goes off, and the way to go on is to prepare it again."""
        if self.confirm is None or self.waiting or self.closing or self.shown is None or self.outdated_group:
            return
        rt = self.engine.runtime.get(self.pair_id)
        preview = rt.preview if rt else None
        if preview is not None and preview.outcome is self.shown and preview.fresh() and not preview.stale:
            return
        self.confirm.set_sensitive(False)
        group = self.outdated_group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=escape(_("Something Changed Since This Preview")),
                            subtitle=escape(_("What the sync would do may differ now: look at it again before confirming.")),
                            subtitle_lines=3)
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"]))
        refresh = Gtk.Button(label=_("Refresh"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        refresh.connect("clicked", lambda *_: self._start(refresh=True))
        row.add_suffix(refresh)
        group.add(row)
        self.page.add(group)

    def _closed(self) -> None:
        GLib.source_remove(self._tick)
        self.engine.disconnect(self._handler)
        if self.waiting and not self.closing:
            # closed while it runs: it goes on, and tells the user when it is ready
            self.engine.forget_listener(self.pair_id, self._done)

    def _cancel(self) -> None:
        self.closing = True
        if self.waiting:
            self.engine.cancel_preview(self.pair_id)
        self.close()

    def _done(self, outcome: Outcome | None, error: str) -> None:
        if self.closing:
            return
        self.waiting = False
        if self.spinner_group.get_parent() is not None:
            self.page.remove(self.spinner_group)
        group = self.result_group = Adw.PreferencesGroup()
        if outcome is None:
            self.page.add(group)
            group.add(Adw.ActionRow(title=_("The preview failed"), subtitle=GLib.markup_escape_text(error)))
            return
        plan = outcome.plan
        self.shown = outcome
        self._info(outcome)
        self.page.add(group)
        if outcome.result is not Result.OK and not plan.empty:
            group.set_description(_("Note: {message}").format(message=outcome.message))
        elif outcome.result is not Result.OK:
            group.add(Adw.ActionRow(title=_("The preview stopped"),
                                    subtitle=GLib.markup_escape_text(outcome.message or ""), subtitle_lines=4))
            return
        # the last field: the file is only on Koofr for now, so a click opens its folder there.
        # Moves are (from, to) pairs.
        rows = [
            # the theme's own transfer icons (Adwaita's go-up and go-down are
            # chevrons, which next to a row's chevron read as "expand")
            (plan.upload, _("Copied to Koofr"), "send-to-symbolic", False),
            (plan.download, _("Copied to this computer"), "folder-download-symbolic", True),
            (plan.moved_remote, _("Moved or renamed here: moved the same way on Koofr"), "go-jump-symbolic", False),
            (plan.moved_local, _("Moved or renamed on Koofr: moved the same way here"), "go-jump-symbolic", False),
            (plan.replace_local, _("Replaced here by the newer Koofr version (the old one goes to the backup)"),
             "document-revert-symbolic", False),
            (plan.delete_local, _("Deleted here (moved to Spola's backup)"), "user-trash-symbolic", False),
            (plan.delete_remote, _("Deleted on Koofr"), "user-trash-symbolic", True),
            (plan.conflicts, _("Changed on both sides: both versions are kept"), "dialog-warning-symbolic", False),
            (plan.refused, _("Left out: Koofr does not accept these names (rename them to sync them)"),
             "action-unavailable-symbolic", False),
        ]
        if plan.empty:
            group.add(Adw.ActionRow(title=_("Nothing to do: the two folders already match.")))
        elif self.force and (plan.moved_remote or plan.moved_local) and not group.get_description():
            group.set_description(_("Moved and renamed files count as deletions for the safety check, "
                                    "but they are moved the same way on the other side, not deleted."))
        rt = self.engine.runtime.get(self.pair_id)
        why = {r.path: preflight.refused_reason(r.code, r.other) for r in (rt.pending_refused if rt else [])}
        # the files of the last run, here and on Koofr: how much of each folder a change touches
        preview = rt.preview if rt else None
        bases = preview.bases if preview is not None and preview.outcome is outcome else None
        on_side = {id(plan.upload): 0, id(plan.download): 1, id(plan.moved_remote): 0, id(plan.moved_local): 1,
                   id(plan.replace_local): 0, id(plan.delete_local): 0, id(plan.delete_remote): 1,
                   id(plan.conflicts): 0, id(plan.refused): 0}
        for paths, title, icon, on_koofr in rows:
            if not paths:
                continue
            entries = []
            for entry in paths:
                if isinstance(entry, tuple):
                    # a move: shown where it goes, with where it was; here the
                    # file is still where it was if Koofr moved it, already
                    # moved if this computer did
                    old, path = entry
                    here = path if paths is plan.moved_remote else old
                    entries.append((path, _("From “{path}”").format(path=old), here))
                else:
                    # a left-out name says why: most of them look perfectly normal
                    entries.append((entry, why.get(entry, "") if paths is plan.refused else "", entry))
            base = bases[on_side[id(paths)]] if bases is not None else None
            nodes = filetree.build(entries, plan.sizes, plan.folders, base)
            files, size = filetree.totals(nodes)
            if paths is plan.upload or paths is plan.download:
                size = size or (plan.upload_bytes if paths is plan.upload else plan.download_bytes)
            subtitle = plan_tree.amount(files, size)
            if base is not None:
                part = filetree.share(files, filetree.whole([e[0] for e in entries], base))
                subtitle += f" · {part}" if part else ""
            row = Adw.ActionRow(title=title, subtitle=escape(subtitle), title_lines=2, activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION))
            row.connect("activated", lambda _r, t=title, n=nodes, k=on_koofr: self.nav.push(plan_tree.PlanTreePage(
                t, n, lambda node, k=k: self._show(node.show, k),
                _("Open on Koofr") if k else _("Show in the file manager"))))
            group.add(row)
        pair = self.engine.cfg.pair(self.pair_id)
        if plan.refused and rt is not None and pair is not None and any(
                new for _item, new in preflight.rename_plan(pair.local, rt.pending_refused, pair.remote)):
            rename = Adw.ButtonRow(title=_("Rename Them…"))
            refused = list(rt.pending_refused)
            rename.connect("activated", lambda *_: RenameDialog(self.window, self.pair_id, refused,
                                                                self._renamed).present(self))
            group.add(rename)
        if self.confirm is not None:
            self.confirm.set_sensitive(True)
            if plan.delete_remote or plan.delete_local:
                self.confirm.set_css_classes(["destructive-action"])
            elif self.short_of_space:
                self.confirm.set_css_classes([])       # possible, no longer the obvious choice

    def _info(self, outcome: Outcome) -> None:
        """Above the lists: whether Koofr has room for what goes up, and how
        old the preview is when it was kept from earlier."""
        self.short_of_space = False
        rt = self.engine.runtime.get(self.pair_id)
        preview = rt.preview if rt else None
        group, rows = Adw.PreferencesGroup(), 0
        plan = outcome.plan
        free = preview.free_bytes if preview is not None and preview.outcome is outcome else None
        if plan.upload_bytes and free is not None:
            needed, room = GLib.format_size(plan.upload_bytes), GLib.format_size(free)
            row = Adw.ActionRow(subtitle_lines=4)
            if plan.upload_bytes > free:
                self.short_of_space = True
                row.set_title(escape(_("Not Enough Space on Koofr")))
                row.set_subtitle(escape(_(
                    "{needed} to upload, {free} free. Leave some folders out (Exclusions) or make room on Koofr "
                    "first: otherwise the sync stops when Koofr is full.").format(needed=needed, free=room)))
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["error"]))
            else:
                row.set_title(escape(_("Space on Koofr")))
                row.set_subtitle(escape(_("{needed} to upload, {free} free").format(needed=needed, free=room)))
                row.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic"))
            group.add(row)
            rows += 1
        if preview is not None and preview.outcome is outcome and time.time() - preview.finished > 60:
            row = Adw.ActionRow(title=escape(_("Prepared at {time}").format(time=when(preview.finished))),
                                subtitle=escape(_("Changes made since then are not in this list.")))
            refresh = Gtk.Button(label=_("Refresh"), valign=Gtk.Align.CENTER)
            refresh.connect("clicked", lambda *_: self._start(refresh=True))
            row.add_suffix(refresh)
            group.add(row)
            rows += 1
        if rows:
            self.info_group = group
            self.page.add(group)

    def _renamed(self, done: int, errors: list[str]) -> None:
        """The names changed: what the run would do changed too."""
        tell(self.toasts, ngettext("{n} file renamed", "{n} files renamed", done).format(n=count(done)) if done else "",
             errors)
        self._start(refresh=True)

    def _show(self, path: str, on_koofr: bool) -> None:
        pair = self.engine.cfg.pair(self.pair_id)
        if on_koofr:
            url = koofr_api.folder_url(self.engine.cfg.mount_id, pair.remote, path)
            Gtk.UriLauncher.new(url).launch(self.window, None, None, None)
        else:
            reveal(self.window, os.path.join(pair.local, path))

    def _confirm(self) -> None:
        if not self.engine.confirm(self.pair_id, self.mode, self.force):
            # the preview was overtaken while the dialog was open: again, before confirming
            tell(self.toasts, _("The preview is no longer current: it is prepared again."), [])
            self._start(refresh=True)
            return
        pair = self.engine.cfg.pair(self.pair_id)
        if pair is not None and self.engine.confirmed_waiting(self.pair_id) and self.engine.active is not None:
            # another folder is syncing: the confirmation is not lost, it waits
            self.window.toast(_("“{name}” confirmed: it starts as soon as the sync in progress is done")
                              .format(name=pair.name))
        self.close()


def about_dialog(parent, engine: Engine | None = None, present: bool = True) -> Adw.AboutDialog:
    from . import WEBSITE, __version__
    dialog = Adw.AboutDialog(
        application_name=APP_NAME, application_icon=APP_ID, version=__version__,
        developer_name="Daniel Grasso", license_type=Gtk.License.MIT_X11, website=WEBSITE,
        comments=_("Keep your folders in sync with Koofr.\nNot affiliated with Koofr."))
    dialog.add_credit_section(_("Sync engine"), ["rclone https://rclone.org"])
    if engine is not None:
        # Troubleshooting: what a report needs, with its Save button; the
        # slow part (rclone's version, the room the backups take) follows
        head = diagnostics.summary(engine)
        dialog.set_debug_info_filename("spola-debug-info.txt")
        dialog.set_debug_info(head + "\n" + _("Measuring the space taken by the logs and backups…"))
        paths, rclone = engine.paths, engine.rclone
        run_in_thread(lambda: diagnostics.storage(paths, rclone),
                      lambda text, error: dialog.set_debug_info(head + "\n" + (text if error is None else str(error))))
    if present:
        dialog.present(parent)
    return dialog

