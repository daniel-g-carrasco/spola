"""Adding a synced folder: the folder on this computer first, then the Koofr
folder, picked among suggestions (the same place, the same name) or by
browsing Koofr one level at a time, where a new folder can be made. Then the
usual checks, and the first alignment is shown as a preview.

bisync needs both folders to exist: a suggested folder that is not on Koofr
yet is created only when the user clicks Add, after the checks.
"""

from __future__ import annotations

import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import preflight, remote_folders, secrets  # noqa: E402
from .i18n import _  # noqa: E402
from .rclone import default_excluded  # noqa: E402
from .remote_folders import CachedFolders, KoofrFolders, LocalFolders, blocked, join  # noqa: E402
from .ui import display_path, escape, run_in_thread  # noqa: E402

MAX_PROBLEMS = 12


def _next_icon() -> Gtk.Image:
    return Gtk.Image(icon_name="go-next-symbolic", accessible_role=Gtk.AccessibleRole.PRESENTATION)


def invalid_name(name: str, siblings: list[str]) -> str:
    """Why Koofr would not take this folder name, or ''."""
    if not name.strip():
        return " "
    if "/" in name or "\\" in name or name in (".", ".."):
        return _("A folder name cannot contain “/”.")
    if any(ord(c) > 0xFFFF for c in name):
        return _("Koofr does not accept emoji and other special symbols in names.")
    if name.casefold() in {s.casefold() for s in siblings}:
        return _("There is already a folder with this name.")
    return ""


class AddFolderDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__(title=_("Add Folder"), content_width=480, content_height=640)
        self.window, self.engine = window, window.engine
        self.local: str | None = None
        self.remote: str | None = None
        self.remote_exists = True
        self.browsed = False                 # the Koofr folder comes from the browser, not a suggestion
        self._folders: CachedFolders | None = None
        self._search = 0                     # a newer local choice makes older answers stale
        self.suggestion_rows: list[Gtk.Widget] = []
        self.problem_rows: list[Gtk.Widget] = []
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView()
        self.nav.add(self._first_page())
        self.toasts.set_child(self.nav)
        self.set_child(self.toasts)

    # -- the Koofr side ------------------------------------------------------------------

    def folders(self) -> CachedFolders:
        """Called in worker threads only: the keyring lookup may block."""
        if self._folders is None:
            if self.engine.dev_remote:
                backend = LocalFolders(self.engine.dev_remote)
            else:
                password = secrets.lookup(self.engine.cfg.user)
                if not password:
                    raise RuntimeError(_("The app password is not in the keyring: sign in again."))
                backend = KoofrFolders(self.engine.cfg.user, password, self.engine.cfg.mount_id)
            self._folders = CachedFolders(backend)
        return self._folders

    # -- first page ------------------------------------------------------------------------

    def _first_page(self) -> Adw.NavigationPage:
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
        page.add(Adw.PreferencesGroup(description=_(
            "The two folders are kept identical in both directions. Before anything is copied, a preview "
            "shows what the first alignment does.")))

        group = Adw.PreferencesGroup(title=_("On This Computer"))
        self.local_row = Adw.ActionRow(title=_("Folder"), subtitle=_("None chosen"))
        choose = Gtk.Button(label=_("Choose…"), valign=Gtk.Align.CENTER)
        choose.connect("clicked", lambda *_: self._choose_local())
        self.local_row.add_suffix(choose)
        self.local_row.set_activatable_widget(choose)
        group.add(self.local_row)
        page.add(group)

        self.koofr_group = Adw.PreferencesGroup(title=_("On Koofr"))
        self.searching_row = Adw.ActionRow(title=_("Looking for matching folders…"), visible=False)
        self.searching_row.add_prefix(Adw.Spinner(width_request=16, height_request=16))
        self.koofr_group.add(self.searching_row)
        self.browse_row = Adw.ActionRow(title=_("Other Folder"), subtitle=_("Browse the folders on Koofr"),
                                        activatable=True)
        # the radio buttons' group: an unseen one stands for "nothing chosen"
        self.none_check = Gtk.CheckButton(active=True)
        self.browse_check = Gtk.CheckButton(valign=Gtk.Align.CENTER, can_target=False, focusable=False,
                                            group=self.none_check)
        self.browse_row.add_prefix(self.browse_check)
        self.browse_row.add_suffix(_next_icon())
        self.browse_row.connect("activated", lambda *_: self.browse("/"))
        self.koofr_group.add(self.browse_row)
        page.add(self.koofr_group)

        self.problems = Adw.PreferencesGroup(title=_("Cannot add this folder"), visible=False)
        page.add(self.problems)
        view.set_content(page)
        return Adw.NavigationPage(title=_("Add Folder"), tag="add", child=view)

    def _choose_local(self) -> None:
        dialog = Gtk.FileDialog(title=_("Choose a Folder"), modal=True)

        def done(d, result):
            try:
                folder = d.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.set_local(folder.get_path())

        dialog.select_folder(self.window, None, done)

    def set_local(self, path: str) -> None:
        self.local = os.path.realpath(path)
        self.local_row.set_subtitle(escape(display_path(self.local)))
        problems = preflight.check_local_root(self.local) \
            + [p for p in preflight.check_overlap(self.local, "/", self.engine.cfg) if p.code == "local-overlap"] \
            + preflight.check_koofr_desktop(self.local, Path.home() / ".koofr" / "config.json")
        self._show_problems(problems)
        self._set_suggestions([])
        if not self.browsed:
            self._select(None)
        if problems:
            return
        self._search += 1
        search, local, pairs = self._search, self.local, list(self.engine.cfg.pairs)
        self.searching_row.set_visible(True)

        def done(found, error):
            if search != self._search:
                return
            self.searching_row.set_visible(False)
            if error:
                row = Adw.ActionRow(title=_("Cannot reach Koofr"), subtitle=escape(str(error)), subtitle_lines=3)
                self._set_suggestions([row])
                return
            self._set_suggestions([self._suggestion_row(s) for s in found])
            if self.remote is None and found and found[0].exists:
                self._select(found[0].path, True)          # the obvious match, ready to add

        run_in_thread(lambda: remote_folders.suggestions(self.folders(), local, pairs), done)

    def _suggestion_row(self, suggestion: remote_folders.Suggestion) -> Adw.ActionRow:
        row = Adw.ActionRow(title=escape(suggestion.path), subtitle=escape(suggestion.reason), activatable=True)
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self.none_check)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        row.path, row.exists, row.check = suggestion.path, suggestion.exists, check
        check.connect("toggled", lambda c: self._select(row.path, row.exists, browsed=False, from_row=True)
                      if c.get_active() else None)
        return row

    def _set_suggestions(self, rows: list[Gtk.Widget]) -> None:
        for row in self.suggestion_rows:
            self.koofr_group.remove(row)
        self.suggestion_rows = rows
        # suggestions above "Other Folder": remove it and add it back last
        self.koofr_group.remove(self.browse_row)
        for row in rows:
            self.koofr_group.add(row)
        self.koofr_group.add(self.browse_row)

    def _select(self, path: str | None, exists: bool = True, browsed: bool = False, from_row: bool = False) -> None:
        self.remote, self.remote_exists, self.browsed = path, exists, browsed and path is not None
        if not from_row:
            match = next((r for r in self.suggestion_rows if getattr(r, "path", None) == path), None)
            if path is None:
                self.none_check.set_active(True)
            elif match is not None:
                match.check.set_active(True)
                self.browsed = False
            else:
                self.browse_check.set_active(True)
        self.browse_row.set_title(escape(path) if self.browsed else _("Other Folder"))
        self.browse_row.set_subtitle(_("Chosen on Koofr") if self.browsed else _("Browse the folders on Koofr"))
        self.add_button.set_sensitive(bool(self.local and self.remote))

    # -- browsing Koofr --------------------------------------------------------------------

    def browse(self, path: str) -> None:
        """Open the browser at `path`, with a page for each level above it so
        that Back goes up one folder at a time."""
        self.nav.pop_to_tag("add")
        current = "/"
        self.nav.push(KoofrFolderPage(self, current))
        for part in [p for p in path.split("/") if p]:
            current = join(current, part)
            self.nav.push(KoofrFolderPage(self, current))

    def chosen(self, path: str) -> None:
        self.nav.pop_to_tag("add")
        self._select(path, True, browsed=True)

    # -- adding ----------------------------------------------------------------------------

    def _show_problems(self, problems) -> None:
        for row in self.problem_rows:
            self.problems.remove(row)
        self.problem_rows = []
        for p in problems[:MAX_PROBLEMS]:
            row = Adw.ActionRow(title=escape(p.message), subtitle=escape(p.path), subtitle_lines=2, title_lines=3)
            self.problems.add(row)
            self.problem_rows.append(row)
        if len(problems) > MAX_PROBLEMS:
            row = Adw.ActionRow(title=_("…and {n} more").format(n=len(problems) - MAX_PROBLEMS))
            self.problems.add(row)
            self.problem_rows.append(row)
        self.problems.set_visible(bool(problems))

    def _check_and_add(self) -> None:
        local, remote, exists = self.local, self.remote, self.remote_exists
        if not local or not remote:
            return
        problems = (preflight.check_local_root(local)
                    + preflight.check_overlap(local, remote, self.engine.cfg)
                    + preflight.check_koofr_desktop(local, Path.home() / ".koofr" / "config.json"))
        _kind, reason = blocked(remote, self.engine.cfg.pairs)
        if reason:
            problems.append(preflight.Problem("remote", reason, remote))
        if problems:
            self._show_problems(problems)
            return
        self.add_button.set_sensitive(False)
        self.add_button.set_label(_("Checking names…"))

        def work():
            found = preflight.scan_names(local, remote, skip=default_excluded)
            if found:
                return remote, found
            # only now, with everything checked: the folder bisync needs on Koofr
            return (remote if exists else remote_folders.ensure(self.folders(), remote)), []

        def done(result, error):
            self.add_button.set_label(_("Add"))
            self.add_button.set_sensitive(True)
            if error:
                self._show_problems([preflight.Problem("scan", str(error), local)])
                return
            spelled, found = result
            if found:
                self._show_problems(found)
                return
            pair = self.engine.add_pair(local, spelled)
            self.close()
            self.window.show_pair(pair.id)
            self.window.open_preview(pair.id)

        run_in_thread(work, done)


class KoofrFolderPage(Adw.NavigationPage):
    """One folder on Koofr: its subfolders, Choose, New Folder."""

    def __init__(self, dialog: AddFolderDialog, path: str):
        name = os.path.basename(path.rstrip("/")) or "Koofr"
        super().__init__(title=name)
        self.dialog, self.path = dialog, path
        self.kind, self.reason = blocked(path, dialog.engine.cfg.pairs)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=name, subtitle=path if path != "/" else ""))
        self.choose = Gtk.Button(label=_("Choose"), css_classes=["suggested-action"],
                                 sensitive=not self.kind)
        self.choose.connect("clicked", lambda *_: dialog.chosen(self.path))
        header.pack_end(self.choose)
        view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        spinner = Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER,
                              valign=Gtk.Align.CENTER)
        self.stack.add_named(spinner, "loading")
        self.error = Adw.StatusPage(icon_name="network-error-symbolic", title=_("Cannot Open This Folder"))
        retry = Gtk.Button(label=_("Try Again"), halign=Gtk.Align.CENTER, css_classes=["pill"])
        retry.connect("clicked", lambda *_: self.load())
        self.error.set_child(retry)
        self.stack.add_named(self.error, "error")
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "list")
        self.groups: list[Adw.PreferencesGroup] = []
        view.set_content(self.stack)
        self.set_child(view)
        self.load()

    def load(self) -> None:
        self.stack.set_visible_child_name("loading")
        # hidden folders stay hidden, as in GNOME's file chooser
        run_in_thread(lambda: [n for n in self.dialog.folders().list(self.path) if not n.startswith(".")], self._show)

    def _show(self, names, error) -> None:
        if error:
            self.error.set_description(escape(str(error)))
            self.stack.set_visible_child_name("error")
            return
        for group in self.groups:
            self.page.remove(group)
        group = Adw.PreferencesGroup(description=escape(self.reason) if self.reason else None)
        pairs = self.dialog.engine.cfg.pairs
        for name in names:
            child = join(self.path, name)
            kind, reason = blocked(child, pairs)
            row = Adw.ActionRow(title=escape(name), subtitle=escape(reason), activatable=kind != "synced")
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            if kind == "synced":
                row.add_css_class("dim-label")
            else:
                row.add_suffix(_next_icon())
                row.connect("activated", lambda _r, c=child: self.dialog.nav.push(KoofrFolderPage(self.dialog, c)))
            group.add(row)
        if not names:
            empty = Adw.ActionRow(title=_("No folders inside"))
            empty.add_css_class("dim-label")
            group.add(empty)
        new_group = Adw.PreferencesGroup()
        new = Adw.ButtonRow(title=_("New Folder…"), start_icon_name="folder-new-symbolic")
        new.connect("activated", lambda *_: self._new_folder(names))
        new_group.add(new)
        self.groups = [group, new_group]
        for g in self.groups:
            self.page.add(g)
        self.stack.set_visible_child_name("list")

    def _new_folder(self, siblings: list[str]) -> None:
        entry = Adw.EntryRow(title=_("Name"), activates_default=True)
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        box.append(entry)
        body = (_("A new empty folder in {place} on Koofr.").format(place=self.path) if self.path != "/"
                else _("A new empty folder at the top level of Koofr."))
        alert = Adw.AlertDialog(heading=_("New Folder"), body=body, extra_child=box)
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("create", _("Create"))
        alert.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("create")
        alert.set_close_response("cancel")
        alert.set_response_enabled("create", False)

        def changed(*_args):
            problem = invalid_name(entry.get_text(), siblings)
            alert.set_response_enabled("create", not problem)
            if problem.strip():
                entry.add_css_class("error")
                entry.set_tooltip_text(problem)
            else:
                entry.remove_css_class("error")
                entry.set_tooltip_text(None)

        entry.connect("changed", changed)

        def response(_a, answer):
            name = entry.get_text().strip()
            if answer != "create" or invalid_name(name, siblings):
                return

            def done(_result, error):
                if error:
                    self.dialog.toasts.add_toast(Adw.Toast(title=str(error), timeout=6))
                    return
                self.load()
                self.dialog.nav.push(KoofrFolderPage(self.dialog, join(self.path, name)))

            run_in_thread(lambda: self.dialog.folders().create(self.path, name), done)

        alert.connect("response", response)
        alert.present(self.dialog)
