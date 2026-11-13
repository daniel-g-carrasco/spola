"""Native sharing controls. Opening reads; every write has an explicit action.

Koofr remains the source of truth, including after a partially failed write.
The worker may finish after closing, but its reply cannot touch a closed UI.
"""

from __future__ import annotations

import datetime as dt
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from spola import WINDOWS  # noqa: E402
from spola.app import sharing  # noqa: E402
from spola.i18n import _, ngettext  # noqa: E402
from .ui import escape, run_in_thread  # noqa: E402


def _button(label, callback, *, suggested=False, destructive=False):
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if suggested:
        button.add_css_class("suggested-action")
    if destructive:
        button.add_css_class("destructive-action")
    button.connect("clicked", lambda *_: callback())
    return button


def _combo(title, choices):
    return Adw.ComboRow(title=title, model=Gtk.StringList.new(choices))


def _link_description(link, expiry=True):
    """Who can open this link and what it allows, in words a recipient would
    also understand; every fact comes from Koofr's own answer. `expiry`:
    said here too (the item's own link has a row for it)."""
    if not link.active:
        return (_("This link has expired.") if link.valid_to and link.valid_to <= time.time() * 1000 else
                _("This link is not active yet."))
    if link.password_required and not link.has_password:
        text = _("Koofr requires a password for this link: set one before sharing it.")
    elif link.has_password and link.downloadable:
        text = _("Anyone with the link and password can view and download.")
    elif link.has_password:
        text = _("Anyone with the link and password can view.")
    elif link.downloadable:
        text = _("Anyone with the link can view and download. No account is needed.")
    else:
        text = _("Anyone with the link can view. No account is needed.")
    if not link.downloadable:
        text += "\n" + _("The download button is hidden; recipients can still save or record content.")
    if expiry:
        text += "\n" + (_("Expires: {date}").format(date=_date(link.valid_to)) if link.valid_to else _("No expiration date."))
    if link.password_required and link.has_password:
        text += "\n" + _("Koofr requires a password for this link.")
    return text


def _date(timestamp):
    return dt.datetime.fromtimestamp(timestamp / 1000).strftime("%x %H:%M")


def _role_title(permissions):
    return {"viewer": _("Can view"), "editor": _("Can edit"), "custom": _("Custom permissions")}.get(
        sharing.role_for_permissions(permissions), _("Custom permissions"))


class PermissionEditor(Gtk.Box):
    """Presets for ordinary sharing; individual grants remain available."""

    def __init__(self, is_dir, permissions=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.original = dict({"READ": True} if permissions is None else permissions)
        self.is_dir = is_dir
        self.group = Adw.PreferencesGroup(description=(
            _("Editing allows adding, replacing and deleting files throughout this folder.") if is_dir else
            _("A file shared individually is read-only. Share its folder to allow editing.")))
        self.role = _combo(_("Permission"), [_("Can view"), _("Can edit"), _("Custom permissions")])
        self.role.set_selected({"viewer": 0, "editor": 1}.get(sharing.role_for_permissions(self.original), 2))
        self.role.set_sensitive(is_dir)
        self.group.add(self.role)
        self.append(self.group)
        self.custom = Adw.PreferencesGroup(title=_("Custom permissions"))
        self.switches = {}
        for key, title in (("READ", _("View and download files")), ("WRITE", _("Modify and delete files")), ("COMMENT", _("Comment and label")),
                           ("CREATE_LINK", _("Create download links")), ("CREATE_RECEIVER", _("Create upload links")),
                           ("MOUNT", _("Manage people and permissions")), ("CREATE_ACTION", _("Create actions"))):
            row = Adw.SwitchRow(title=title, active=bool(self.original.get(key)), sensitive=is_dir)
            self.switches[key] = row
            self.custom.add(row)
        self.append(self.custom)
        self.role.connect("notify::selected", self._selected)
        self._selected()

    def _selected(self, *_args):
        selected = self.role.get_selected()
        if selected < 2:
            preset = sharing.permissions_for_role("viewer" if selected == 0 else "editor", self.original)
            for key, row in self.switches.items():
                row.set_active(preset.get(key, False))
        self.custom.set_visible(self.is_dir and selected == 2)

    def permissions(self):
        if not self.is_dir:
            return dict(self.original)
        selected = self.role.get_selected()
        if selected < 2:
            return sharing.permissions_for_role("viewer" if selected == 0 else "editor", self.original)
        result = dict(self.original)
        result.update({key: row.get_active() for key, row in self.switches.items()})
        return result


class SharingDialog(Adw.Dialog):
    """One item's sharing, as Koofr has it now. The layout is libadwaita's
    own: a loading page, an error page with its retry, then boxed lists
    (the public link first, with one evident action: create it, or copy
    it), a banner for what a change left unsaid, button rows for the
    actions. No label is selectable and the focus starts on the main
    action: nothing is highlighted before the user selects it."""

    def __init__(self, window, remote_path):
        super().__init__(title=_("Share"), content_width=480, content_height=640)
        self.window, self.engine, self.path = window, window.engine, remote_path
        self.creds = self.engine.creds
        self.client = (sharing.Client(self.creds.user, self.creds.password, self.engine.cfg.mount_id)
                       if self.creds else None)
        self.snapshot = None
        self._closed, self._busy = False, False
        self._focused = False
        self.member_rows = []
        self.inherited_link_rows = []
        self.connect("closed", self._on_closed)
        if WINDOWS:
            self.connect("realize", self._prepare_window)

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        name = remote_path.rsplit("/", 1)[-1] or "/"
        header.set_title_widget(Adw.WindowTitle(title=_("Share"), subtitle=name, tooltip_text=remote_path))
        self.spinner = Adw.Spinner(visible=False)
        header.pack_start(self.spinner)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh sharing settings"))
        refresh.connect("clicked", lambda *_: self._reload())
        header.pack_end(refresh)
        self.refresh_button = refresh
        view.add_top_bar(header)
        # what a change left unsaid: it stays until the next successful read
        self.banner = Adw.Banner(button_label=_("Try Again"))
        self.banner.connect("button-clicked", lambda *_: self._reload())
        view.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Adw.StatusPage(title=_("Reading the Sharing Settings"),
                                 description=_("Spola asks Koofr who can open “{name}”. Nothing is changed.")
                                 .format(name=name))
        loading.set_child(Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER))
        self.stack.add_named(loading, "loading")
        self.failed = Adw.StatusPage(icon_name="dialog-error-symbolic", title=_("Sharing Settings Not Available"))
        self.retry_button = Gtk.Button(label=_("Try Again"), halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        self.retry_button.connect("clicked", lambda *_: self._reload())
        self.failed.set_child(self.retry_button)
        self.stack.add_named(self.failed, "failed")

        self.page = Adw.PreferencesPage()
        # -- the public link: the first thing, with the one main action
        self.link_group = Adw.PreferencesGroup(title=_("Public Link"))
        self.create_button = Adw.ButtonRow(title=_("Create Public Link"), start_icon_name="list-add-symbolic")
        self.create_button.add_css_class("suggested-action")
        self.create_button.connect("activated", lambda *_: self._create())
        self.link_group.add(self.create_button)
        self.url_row = Adw.ActionRow(title_lines=1, subtitle_lines=1, use_markup=False)
        self.copy_button = Gtk.Button(label=_("Copy Link"), valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self.copy_button.connect("clicked", lambda *_: self._copy_link())
        self.url_row.add_suffix(self.copy_button)
        self.link_group.add(self.url_row)
        # shown as dots until asked for: a property row, not an entry (nothing to type, nothing selected)
        self.password_row = Adw.ActionRow(title=_("Password"), css_classes=["property"], use_markup=False)
        self.password_shown = Gtk.ToggleButton(icon_name="view-reveal-symbolic", valign=Gtk.Align.CENTER,
                                               css_classes=["flat"], tooltip_text=_("Show Password"))
        self.password_shown.connect("toggled", lambda *_: self._show_password())
        self.password_row.add_suffix(self.password_shown)
        self.password_copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                        tooltip_text=_("Copy Password"))
        self.password_copy.connect("clicked", lambda *_: self._copy_password())
        self.password_row.add_suffix(self.password_copy)
        self.link_group.add(self.password_row)
        self.expiry_row = Adw.ActionRow(title=_("Expires"), css_classes=["property"], use_markup=False)
        self.link_group.add(self.expiry_row)
        self.settings_row = Adw.ActionRow(title=_("Link Settings"), activatable=True,
                                          subtitle=_("Password, expiration, download button"))
        self.settings_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.settings_row.connect("activated", lambda *_: self._settings())
        self.link_group.add(self.settings_row)
        self.page.add(self.link_group)
        # -- the links of the folders above: they open this item too
        self.parent_links_group = Adw.PreferencesGroup(title=_("Links on Parent Folders"), visible=False)
        self.page.add(self.parent_links_group)
        # -- people
        self.people_group = Adw.PreferencesGroup(title=_("People"))
        self.invite_button = Adw.ButtonRow(title=_("Add People…"), start_icon_name="list-add-symbolic")
        self.invite_button.connect("activated", lambda *_: self._invite())
        self.people_group.add(self.invite_button)
        self.page.add(self.people_group)
        # -- what cannot be undone, last and apart
        self.link_actions = Adw.PreferencesGroup()
        self.revoke_button = Adw.ButtonRow(title=_("Revoke Link…"))
        self.revoke_button.add_css_class("destructive-action")
        self.revoke_button.connect("activated", lambda *_: self._revoke())
        self.link_actions.add(self.revoke_button)
        self.page.add(self.link_actions)
        self.stack.add_named(self.page, "content")

        view.set_content(self.stack)
        self.toasts = Adw.ToastOverlay(child=view)
        self.set_child(self.toasts)
        self._render()
        self._reload()

    def _prepare_window(self, _dialog):
        root = self.get_root()
        if isinstance(root, Gtk.Window) and root is not self.window:
            root.set_resizable(True)
            root.set_default_size(self.get_content_width(), self.get_content_height())

    def _on_closed(self, *_args):
        self._closed = True

    def _current_account(self):
        return self.creds is not None and self.engine.creds is self.creds

    def _set_busy(self, busy):
        self._busy = busy
        # the first read has its own page; a change keeps what is shown, with a spinner in the header
        self.spinner.set_visible(busy and self.snapshot is not None)
        self.refresh_button.set_sensitive(not busy)
        for group in (self.link_group, self.parent_links_group, self.people_group, self.link_actions):
            group.set_sensitive(not busy and self.snapshot is not None)
        self.copy_button.set_sensitive(not busy and bool(self.snapshot and self.snapshot.link and self.snapshot.link.active))
        self.create_button.set_sensitive(not busy and bool(self.snapshot and self.snapshot.can_create_link))
        self._show_page()

    def _show_page(self):
        if self.snapshot is not None:
            self.stack.set_visible_child_name("content")
        elif self._busy:
            self.stack.set_visible_child_name("loading")
        else:
            self.stack.set_visible_child_name("failed")

    def _error(self, message, retry=False):
        """What went wrong: on the page that replaces the settings when
        there are none to show, else in the banner above them."""
        self._error_text = message
        self.failed.set_description(escape(message) if message else "")
        self.retry_button.set_visible(retry)
        self.banner.set_title(message.replace("\n", " "))
        self.banner.set_button_label(_("Try Again") if retry else "")
        self.banner.set_revealed(bool(message) and self.snapshot is not None)

    def error_text(self) -> str:
        return getattr(self, "_error_text", "")

    def link_text(self) -> str:
        """What the window says about the public link."""
        return self.link_group.get_description() or ""

    def _reload(self):
        if self._closed or self._busy:
            return
        if not self._current_account():
            self.snapshot = None
            self._render()
            self._error(_("The account changed. Close this window and open sharing again."))
            return
        self._error("")
        self._set_busy(True)

        def done(state, error):
            if self._closed:
                return
            self._busy = False
            if not self._current_account():
                state, error = None, _("The account changed. Close this window and open sharing again.")
            self.snapshot = state if not error else None
            self._render()
            if error:
                self._error(str(error), retry=self._current_account())
        run_in_thread(lambda: self.client.load(self.path), done)

    def _change(self, action):
        if self._closed or self._busy or not self._current_account():
            return
        self._error("")
        self._set_busy(True)

        def work():
            failure = None
            try:
                action()
            except Exception as error:
                failure = error
            # A multi-request operation might have partly succeeded. Never
            # infer the effective permissions from the requested values.
            try:
                return self.client.load(self.path), failure, None
            except Exception as error:
                return None, failure, error

        def done(result, error):
            if self._closed:
                return
            self._busy = False
            if not self._current_account():
                self.snapshot = None
                self._render()
                self._error(_("The account changed. Close this window and open sharing again."))
                return
            state, failure, read_error = result if result else (None, None, error)
            self.snapshot = state
            self._render()
            if failure or read_error:
                parts = [str(e) for e in (failure, read_error) if e]
                if read_error:
                    parts.append(_("The current sharing settings could not be verified. Refresh before copying a link."))
                self._error("\n".join(parts), retry=bool(read_error))
        run_in_thread(work, done)

    def _render(self):
        state = self.snapshot
        self._show_page()
        if getattr(self, "_error_text", ""):
            self.banner.set_revealed(state is not None)
        # -- people
        for row in self.member_rows:
            self.people_group.remove(row)
        self.member_rows.clear()
        self.people_group.remove(self.invite_button)
        self.people_group.set_description(_("People you invite open it with their own Koofr account."))
        if state:
            if not state.access and not state.inherited:
                self.people_group.set_description(_("Nobody else has been added. People you invite open it with "
                                                    "their own Koofr account."))
            for access in ([state.access] if state.access else []) + list(state.inherited):
                inherited = access in state.inherited
                for member in access.members:
                    role = _("Owner") if member.owner else _role_title(member.permissions)
                    subtitle = member.email if member.email and member.email != member.name else ""
                    if inherited:
                        subtitle += ("\n" if subtitle else "") + _("Access from {path}").format(path=access.path)
                    row = Adw.ActionRow(title=member.name or member.email, subtitle=subtitle, subtitle_lines=0,
                                        use_markup=False)
                    row.add_prefix(Adw.Avatar(size=32, text=member.name or member.email, show_initials=True))
                    row.add_suffix(Gtk.Label(label=role, css_classes=["dim-label"]))
                    if access.can_manage and not inherited and not member.owner and not member.is_group:
                        row.set_activatable(True)
                        row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                        row.connect("activated", lambda _r, a=access, m=member: MemberDialog(self, a, m).present(self))
                    self.people_group.add(row)
                    self.member_rows.append(row)
        self.people_group.add(self.invite_button)          # last, under the people already there
        self.invite_button.set_visible(bool(state and state.can_invite))
        # -- the public link
        link = state.link if state else None
        manage = bool(link and state.can_create_link)
        self.create_button.set_visible(bool(state and not link and state.can_create_link))
        self.url_row.set_visible(link is not None)
        self.password_row.set_visible(bool(link and link.has_password))
        self.password_text = link.password if link else ""
        self.password_shown.set_sensitive(bool(self.password_text))
        self.password_copy.set_sensitive(bool(self.password_text))
        self._show_password()
        self.expiry_row.set_visible(bool(link and link.active))
        self.settings_row.set_visible(manage)
        self.link_actions.set_visible(manage)
        if link:
            self.url_row.set_title(link.url)
            self.url_row.set_subtitle(ngettext("Opened {n} time", "Opened {n} times", link.counter).format(n=link.counter)
                                      if link.counter else _("Not opened yet"))
            self.expiry_row.set_subtitle(_date(link.valid_to) if link.valid_to else _("Never"))
            description = _link_description(link, expiry=False)
            others = len(state.other_links)
            if others:
                description += "\n" + ngettext("{n} other link leads to this item; Revoke Link removes it too.",
                                               "{n} other links lead to this item; Revoke Link removes them too.",
                                               others).format(n=others)
            self.link_group.set_description(escape(description))
        elif state and any(parent.active for parent in state.inherited_links):
            self.link_group.set_description(escape(_(
                "No link of its own, but the link of a folder above opens this item too: see below.")))
        elif state and state.can_create_link:
            self.link_group.set_description(escape(_(
                "No public link yet. With one, anyone who has it can view and download without an account. "
                "Koofr may require a password.")))
        else:
            self.link_group.set_description(escape(_("No public link. You do not have permission to create one "
                                                     "for this item.")) if state else "")
        for row in self.inherited_link_rows:
            self.parent_links_group.remove(row)
        self.inherited_link_rows.clear()
        if state:
            for inherited_link in state.inherited_links:
                if inherited_link.active:
                    description = (_("This folder's link opens this item too. Manage it from that folder.")
                                   + "\n" + _link_description(inherited_link))
                else:
                    description = _("This folder's link is inactive. Manage it from that folder.")
                row = Adw.ActionRow(title=inherited_link.path, subtitle=description, subtitle_lines=0, title_lines=0,
                                    use_markup=False)
                row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                self.parent_links_group.add(row)
                self.inherited_link_rows.append(row)
        self.parent_links_group.set_visible(bool(self.inherited_link_rows))
        self._set_busy(self._busy)
        if state and not self._focused:
            # the main action has the focus: no text is selected until the user selects it
            self._focused = True
            main = self.copy_button if link else (self.create_button if state.can_create_link else self.refresh_button)
            GLib.idle_add(lambda: (main.grab_focus() if not self._closed else None) and False)

    def _show_password(self):
        shown = self.password_shown.get_active() and bool(self.password_text)
        self.password_shown.set_icon_name("view-conceal-symbolic" if shown else "view-reveal-symbolic")
        self.password_shown.set_tooltip_text(_("Hide Password") if shown else _("Show Password"))
        # a password Koofr has but does not give back is said, not shown as empty
        self.password_row.set_subtitle(self.password_text if shown else
                                       ("\u2022" * 8 if self.password_text else _("Set, not shown by Koofr")))

    def _copy_text(self, text):
        self.window.get_clipboard().set(text)
        self.toasts.add_toast(Adw.Toast(title=_("Copied")))

    def _copy_link(self):
        if not self._busy and self._current_account() and self.snapshot and self.snapshot.link and self.snapshot.link.active:
            self._copy_text(self.snapshot.link.url)

    def _copy_password(self):
        if not self._busy and self._current_account() and self.snapshot and self.snapshot.link and self.snapshot.link.password:
            self._copy_text(self.snapshot.link.password)

    def _create(self):
        if self.snapshot and not self.snapshot.link and self.snapshot.can_create_link:
            self._change(lambda: self.client.create_link(self.path))

    def _settings(self):
        if self.snapshot and self.snapshot.link and self.snapshot.can_create_link:
            LinkSettingsDialog(self, self.snapshot.link).present(self)

    def _invite(self):
        if self.snapshot and self.snapshot.can_invite:
            MemberDialog(self).present(self)

    def _revoke(self):
        if not self.snapshot or not self.snapshot.link or not self.snapshot.can_create_link:
            return
        links = self.snapshot.links
        body = _("People will no longer be able to use this link. Access through invitations or other shared folders remains.")
        if len(links) > 1:
            body = ngettext("{n} link leads to this item: all are revoked.", "{n} links lead to this item: all are revoked.",
                            len(links)).format(n=len(links)) + " " + body
        dialog = Adw.AlertDialog(heading=_("Revoke this link?"), body=body)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("revoke", _("Revoke Link"))
        dialog.set_response_appearance("revoke", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self._change(lambda: self.client.revoke_links(links))
                       if response == "revoke" else None)
        dialog.present(self)


class _EditorDialog(Adw.Dialog):
    def __init__(self, parent, title, action):
        super().__init__(title=title, content_width=500, content_height=560)
        self.parent = parent
        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.pack_start(_button(_("Cancel"), self.close))
        self.save_button = _button(action, self._save, suggested=True)
        header.pack_end(self.save_button)
        view.add_top_bar(header)
        self.error = Adw.Banner()           # what is wrong with the values, above them
        view.add_top_bar(self.error)
        self.page = Adw.PreferencesPage()
        view.set_content(self.page)
        self.set_child(view)
        if WINDOWS:
            self.connect("realize", self._prepare_window)

    def _prepare_window(self, _dialog):
        root = self.get_root()
        if isinstance(root, Gtk.Window) and root is not self.parent.get_root():
            root.set_resizable(True)
            root.set_default_size(self.get_content_width(), self.get_content_height())

    def _invalid(self, message):
        self.error.set_title(message)
        self.error.set_revealed(True)


class LinkSettingsDialog(_EditorDialog):
    def __init__(self, parent, link):
        super().__init__(parent, _("Link Settings"), _("Save"))
        self.link = link
        group = Adw.PreferencesGroup(title=_("Password"), description=(
            _("Koofr requires password protection for this link.") if link.password_required else
            _("A password protects the link, but does not restrict it to specific people.")))
        choices = [_("Keep current password setting"), _("Generate a new password"), _("Set a custom password")]
        if not link.password_required:
            choices.append(_("Remove password"))
        self.password_mode = _combo(_("Password protection"), choices)
        group.add(self.password_mode)
        self.password = Adw.PasswordEntryRow(title=_("Custom password"), visible=False)
        group.add(self.password)
        self.password_mode.connect("notify::selected", lambda *_: self.password.set_visible(self.password_mode.get_selected() == 2))
        self.page.add(group)
        group = Adw.PreferencesGroup(title=_("Expiration"), description=_("Dates and times use this computer’s time zone."))
        self.expiry = _combo(_("Expires"), [_("Keep current expiration"), _("In one hour"), _("In one day"),
                                           _("In one week"), _("Choose date and time"), _("Never")])
        group.add(self.expiry)
        initial = dt.datetime.fromtimestamp(link.valid_to / 1000) if link.valid_to else dt.datetime.now() + dt.timedelta(days=7)
        self.date_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False, margin_top=12)
        self.calendar = Gtk.Calendar(halign=Gtk.Align.CENTER)
        self.calendar.select_day(GLib.DateTime.new_local(initial.year, initial.month, initial.day, 12, 0, 0))
        self.date_box.append(self.calendar)
        time_row = Adw.ActionRow(title=_("Time"))
        self.hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for spinner, value, label in ((self.hour, initial.hour, _("Hour")), (self.minute, initial.minute, _("Minute"))):
            spinner.set_value(value)
            spinner.set_numeric(True)
            spinner.set_wrap(True)
            spinner.set_valign(Gtk.Align.CENTER)
            spinner.set_tooltip_text(label)
            spinner.update_property([Gtk.AccessibleProperty.LABEL], [label])
            time_row.add_suffix(spinner)
        time_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        time_list.append(time_row)
        self.date_box.append(time_list)
        group.add(self.date_box)
        self.expiry.connect("notify::selected", lambda *_: self.date_box.set_visible(self.expiry.get_selected() == 4))
        self.page.add(group)
        group = Adw.PreferencesGroup(description=_("Koofr may restrict some options according to your account plan."))
        self.hide_download = Adw.SwitchRow(title=_("Hide download button"), active=not link.downloadable,
                                           subtitle=_("This does not prevent saving or recording the content."))
        group.add(self.hide_download)
        self.page.add(group)

    def changes(self):
        mode = ("keep", "reset", "set", "remove")[self.password_mode.get_selected()]
        changes = {"password_mode": mode}
        if mode == "set":
            password = self.password.get_text()
            if not 6 <= len(password) <= 16 or not password.isascii() or not password.isalnum():
                raise ValueError(_("Use 6–16 letters and numbers for the password."))
            changes["password"] = password
        choice = self.expiry.get_selected()
        if choice in (1, 2, 3):
            changes["valid_to"] = int((time.time() + {1: 3600, 2: 86400, 3: 604800}[choice]) * 1000)
        elif choice == 4:
            try:
                self.hour.update()
                self.minute.update()
                selected = self.calendar.get_date()
                date = dt.datetime(selected.get_year(), selected.get_month(), selected.get_day_of_month(),
                                   self.hour.get_value_as_int(), self.minute.get_value_as_int())
                timestamp = date.timestamp()
                if timestamp <= time.time() or dt.datetime.fromtimestamp(timestamp) != date:
                    raise ValueError
            except (ValueError, OverflowError, OSError):
                raise ValueError(_("Choose a future date and time in this computer’s time zone.")) from None
            changes["valid_to"] = int(timestamp * 1000)
        elif choice == 5:
            changes["valid_to"] = None
        downloadable = not self.hide_download.get_active()
        if downloadable != self.link.downloadable:
            changes["downloadable"] = downloadable
        return changes

    def _save(self):
        try:
            changes = self.changes()
        except ValueError as error:
            self._invalid(str(error))
            return
        self.close()
        self.parent._change(lambda: self.parent.client.update_link(self.link, **changes))


class MemberDialog(_EditorDialog):
    def __init__(self, parent, access=None, member=None):
        super().__init__(parent, _("Change Access") if member else _("Add People"), _("Save") if member else _("Send Invitation"))
        self.access, self.member = access, member
        group = Adw.PreferencesGroup(description=(
            _("Permissions apply to the whole shared folder.") if parent.snapshot.is_dir else
            _("This invitation grants read-only access to this file.")))
        self.email = Adw.EntryRow(title=_("Email address"))
        if member:
            self.email.set_text(member.email)
            self.email.set_editable(False)
        group.add(self.email)
        self.page.add(group)
        self.editor = PermissionEditor(parent.snapshot.is_dir, member.permissions if member else None)
        group = Adw.PreferencesGroup()
        group.add(self.editor)
        self.page.add(group)
        if not member:
            self.page.add(Adw.PreferencesGroup(description=_(
                "Koofr sends an invitation. The recipient needs a Koofr account to access this item.")))
        else:
            group = Adw.PreferencesGroup()
            remove = Adw.ButtonRow(title=_("Remove Access…"))
            remove.add_css_class("destructive-action")
            remove.connect("activated", lambda *_: self._remove())
            group.add(remove)
            self.page.add(group)

    def _save(self):
        email = self.email.get_text().strip()
        if not self.member and (email.count("@") != 1 or not all(email.split("@")) or any(c.isspace() for c in email)):
            self._invalid(_("Enter the recipient’s email address."))
            return
        permissions = self.editor.permissions()
        if self.member:
            action = lambda: self.parent.client.update_member(self.access, self.member.id, permissions)
        else:
            action = lambda: self.parent.client.invite(self.parent.path, email, permissions)
        self.close()
        self.parent._change(action)

    def _remove(self):
        dialog = Adw.AlertDialog(heading=_("Remove this person’s access?"), body=_(
            "Access through this invitation will end. Public links and access through other folders remain available."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("remove", _("Remove Access"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def response(_dialog, choice):
            if choice == "remove":
                self.close()
                self.parent._change(lambda: self.parent.client.remove_member(self.access, self.member.id))
        dialog.connect("response", response)
        dialog.present(self)
