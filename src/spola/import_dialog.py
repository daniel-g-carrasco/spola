"""Taking over Koofr Desktop's folders, in two steps.

First Koofr Desktop is stopped and kept from starting at login: two sync
engines on the same files lose files, so the second step opens only when it
is neither running nor set to start. Spola cannot quit it for the user (it
answers neither a close nor the end of a session): the user quits it from
its icon, and the page sees it go. Its start at login Spola turns off on
Windows, as Task Manager does.

Then the folders are chosen. Each one taken over waits for its first
alignment, never syncing on its own, and the engine prepares the preview of
that alignment at once, one folder at a time: nothing is copied before the
user has looked at it and confirmed.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import WINDOWS, migrate, preflight  # noqa: E402
from .i18n import _, ngettext  # noqa: E402
from .ui import display_path, escape  # noqa: E402

POLL_S = 2


def koofr_desktop_has_folders() -> bool:
    return bool(preflight.koofr_desktop_syncs(preflight.koofr_desktop_config()))


class ImportDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=_("Import from Koofr Desktop"), content_width=480, content_height=640)
        self.window, self.engine = window, window.engine
        self.state: preflight.KoofrDesktopState | None = None
        self.checks: list[tuple[Gtk.CheckButton, migrate.Candidate]] = []
        self.nav = Adw.NavigationView()
        self.nav.add(self._stop_page())
        self.toasts = Adw.ToastOverlay(child=self.nav)
        self.set_child(self.toasts)
        self._poll = GLib.timeout_add_seconds(POLL_S, self._refresh)
        self.connect("closed", lambda *_: self._stop_polling())
        self._refresh()

    def _stop_polling(self) -> None:
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0

    # -- step 1: Koofr Desktop stopped ------------------------------------------------------

    def _stop_page(self) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.next_button = Gtk.Button(label=_("Next"), css_classes=["suggested-action"], sensitive=False)
        self.next_button.connect("clicked", lambda *_: self._to_folders())
        header.pack_end(self.next_button)
        view.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.add(Adw.PreferencesGroup(description=_(
            "Koofr Desktop and Spola must never sync the same folders: two sync engines on the same files "
            "lose files. First quit Koofr Desktop and keep it from starting at login; then choose the "
            "folders Spola takes over.")))
        group = Adw.PreferencesGroup(title=_("Koofr Desktop"))
        self.running_icon = Gtk.Image()
        self.running_row = Adw.ActionRow(subtitle_lines=0)
        self.running_row.add_prefix(self.running_icon)
        group.add(self.running_row)
        self.login_icon = Gtk.Image()
        self.login_row = Adw.ActionRow(subtitle_lines=0)
        self.login_row.add_prefix(self.login_icon)
        self.turn_off_button = Gtk.Button(label=_("Turn Off"), valign=Gtk.Align.CENTER)
        self.turn_off_button.connect("clicked", lambda *_: self._turn_off_login())
        self.login_row.add_suffix(self.turn_off_button)
        group.add(self.login_row)
        page.add(group)
        view.set_content(page)
        return Adw.NavigationPage(title=_("Import from Koofr Desktop"), tag="stop", child=view)

    def _refresh(self) -> bool:
        state = preflight.koofr_desktop_state()
        if state != self.state:
            self.state = state
            self._show_state(state)
        return True

    def _show_state(self, state: preflight.KoofrDesktopState) -> None:
        def mark(icon: Gtk.Image, done: bool) -> None:
            # object-select: the tick Adwaita has (no emblem-ok in its theme)
            icon.set_from_icon_name("object-select-symbolic" if done else "dialog-warning-symbolic")
            icon.set_css_classes(["success"] if done else ["warning"])

        mark(self.running_icon, not state.running)
        if state.running:
            self.running_row.set_title(_("Koofr Desktop is running"))
            self.running_row.set_subtitle(
                _("Quit it from its icon in the notification area: right click, then Quit.") if WINDOWS
                else _("Quit it from its icon or its window."))
        else:
            self.running_row.set_title(_("Koofr Desktop is not running"))
            self.running_row.set_subtitle("")
        mark(self.login_icon, not state.at_login)
        self.turn_off_button.set_visible(state.at_login and preflight.CAN_TURN_OFF_KOOFR_DESKTOP_LOGIN)
        if state.at_login:
            self.login_row.set_title(_("Koofr Desktop starts at login"))
            self.login_row.set_subtitle(
                _("It would sync its folders again at the next login.") if preflight.CAN_TURN_OFF_KOOFR_DESKTOP_LOGIN
                else _("Remove it from the apps started at login, in the settings of your desktop."))
        else:
            self.login_row.set_title(_("Koofr Desktop does not start at login"))
            self.login_row.set_subtitle("")
        self.next_button.set_sensitive(not state.active)

    def _turn_off_login(self) -> None:
        try:
            preflight.turn_off_koofr_desktop_login()
        except OSError:
            self.toasts.add_toast(Adw.Toast(title=escape(
                _("It is set for every user of this computer: turn it off in Task Manager, under Startup apps.")),
                timeout=8))
        self.state = None
        self._refresh()

    # -- step 2: the folders ---------------------------------------------------------------

    def _to_folders(self) -> None:
        self._refresh()
        if self.state is None or self.state.active:
            return
        self.nav.push(self._folders_page(migrate.candidates(self.engine.cfg)))

    def _folders_page(self, candidates: list[migrate.Candidate]) -> Adw.NavigationPage:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        self.import_button = Gtk.Button(label=_("Import"), css_classes=["suggested-action"])
        self.import_button.connect("clicked", lambda *_: self._import())
        header.pack_end(self.import_button)
        view.add_top_bar(header)

        self.checks = []
        if not candidates:
            view.set_content(Adw.StatusPage(icon_name="folder-symbolic", title=_("No Folders to Import"),
                                            description=_("Koofr Desktop has no synced folders.")))
            self.import_button.set_sensitive(False)
            return Adw.NavigationPage(title=_("Folders"), tag="folders", child=view)

        page = Adw.PreferencesPage()
        page.add(Adw.PreferencesGroup(description=_(
            "None of them syncs before you confirm the preview of its first alignment: Spola prepares "
            "them right away, one folder at a time.")))
        group = Adw.PreferencesGroup(title=_("Folders of Koofr Desktop"))
        for candidate in candidates:
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=not candidate.problem,
                                    sensitive=not candidate.problem)
            check.connect("toggled", lambda *_: self._update_import_button())
            details = [_("Koofr: {path}").format(path=candidate.remote)]
            if candidate.problem:
                details.append(candidate.problem)
            elif candidate.paused:
                details.append(_("paused in Koofr Desktop: it stays paused"))
            row = Adw.ActionRow(title=escape(display_path(candidate.local)), subtitle=escape("\n".join(details)),
                                subtitle_lines=0, activatable=not candidate.problem)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            group.add(row)
            self.checks.append((check, candidate))
        page.add(group)
        view.set_content(page)
        self._update_import_button()
        return Adw.NavigationPage(title=_("Folders"), tag="folders", child=view)

    def _update_import_button(self) -> None:
        self.import_button.set_sensitive(any(check.get_active() for check, _c in self.checks))

    def _import(self) -> None:
        if preflight.koofr_desktop_active():
            # started again meanwhile: back to the first step
            self.state = None
            self._refresh()
            self.nav.pop_to_tag("stop")
            return
        added, skipped = 0, []
        for check, candidate in self.checks:
            if not check.get_active():
                continue
            # checked again: the pairs may have changed since the list was made
            problem = migrate.problem_of(self.engine.cfg, candidate.local, candidate.remote)
            if problem:
                skipped.append(f"{display_path(candidate.local)}: {problem}")
                continue
            self.engine.add_pair(candidate.local, candidate.remote, paused=candidate.paused)
            added += 1
        self.close()
        if added:
            self.window.toast(ngettext("{n} folder imported: Spola prepares the preview of its first alignment",
                                       "{n} folders imported: Spola prepares the previews of their first alignment",
                                       added).format(n=added))
        for line in skipped:
            self.window.toast(line)
