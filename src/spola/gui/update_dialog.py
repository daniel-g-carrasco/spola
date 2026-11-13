"""A newer Spola: what it brings, and its installer downloaded and started
(the installed build), or its page opened (the portable build).

The layout is libadwaita's own: "Later" and the one action in the header
bar, the app's icon with the version and what happens on a status page,
what is new as boxed lists under the headings of the changelog, the
download as a progress bar in a list of its own, what went wrong in a
banner. No label is selectable, and the focus starts on the action."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from spola import APP_ID, APP_NAME, __version__  # noqa: E402
from spola.app import updates  # noqa: E402
from spola.i18n import _  # noqa: E402


class UpdateDialog(Adw.Dialog):
    def __init__(self, release: updates.Release):
        super().__init__(title=_("Update Available"), content_width=480, content_height=580)
        self.release = release
        self.installer = updates.installer_of(release) if updates.installed_build() else None
        self._cancel = threading.Event()
        self.connect("closed", lambda *_: self._cancel.set())

        view = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self.later = Gtk.Button(label=_("Later"))
        self.later.connect("clicked", lambda *_: self.close())
        header.pack_start(self.later)
        if self.installer is not None:
            self.action = Gtk.Button(label=_("Install Update"), css_classes=["suggested-action"])
            self.action.connect("clicked", lambda *_: self._download())
        else:
            self.action = Gtk.Button(label=_("Open Release Page"), css_classes=["suggested-action"])
            self.action.connect("clicked", lambda *_: self._open_page())
        header.pack_end(self.action)
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        what = _("You have version {version}.").format(version=__version__)
        if self.installer is not None:
            size = getattr(self.installer, "size", 0)
            if size:
                what += " " + _("The download is {size} MB.").format(size=round(size / 1e6))
            what += "\n" + _("Spola closes while the new version is installed, its syncs stopped properly, "
                             "and opens again.")
        self.page = Adw.StatusPage(icon_name=APP_ID, description=what, css_classes=["compact"],
                                   title=_("{app} {version} Is Available").format(app=APP_NAME, version=release.version))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        # the download: a list of its own, shown once it starts
        self.progress_group = Adw.PreferencesGroup(visible=False)
        self.progress = Gtk.ProgressBar(show_text=True, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        frame = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        frame.append(Gtk.ListBoxRow(child=self.progress, activatable=False, focusable=False))
        self.progress_group.add(frame)
        content.append(self.progress_group)
        self.note_rows: list[Adw.ActionRow] = []
        for heading, items in updates.notes_sections(release.notes):
            group = Adw.PreferencesGroup(title=heading or _("What Is New"))
            for item in items:
                row = Adw.ActionRow(title=item, title_lines=0, use_markup=False)
                group.add(row)
                self.note_rows.append(row)
            content.append(group)
        if self.installer is not None:
            group = Adw.PreferencesGroup()
            page = Adw.ActionRow(title=_("Release Page"), subtitle=_("This version on GitHub, with its files"),
                                 activatable=True)
            page.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic"))
            page.connect("activated", lambda *_: Gio.AppInfo.launch_default_for_uri(self.release.url, None))
            group.add(page)
            content.append(group)
        self.page.set_child(Adw.Clamp(child=content, maximum_size=440))
        view.set_content(self.page)
        self.set_child(view)
        # the focus on the action: nothing else takes it, and nothing is selected
        self.set_default_widget(self.action)
        self.set_focus(self.action)

    def _open_page(self) -> None:
        Gio.AppInfo.launch_default_for_uri(self.release.url, None)
        self.close()

    def _download(self) -> None:
        self.action.set_sensitive(False)
        self.banner.set_revealed(False)
        self.later.set_label(_("Cancel"))
        self.progress_group.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text(_("Downloading…"))

        def progress(done: int, total: int) -> None:
            GLib.idle_add(self._progress, done, total)

        def work() -> None:
            try:
                dest = updates.download_folder() / self.installer.name
                updates.download(self.installer, dest, progress, self._cancel)
            except Exception as e:      # whatever it is, said: never a dialog stuck on "Downloading…"
                GLib.idle_add(self._failed, _("Download failed: {error}").format(error=e))
                return
            GLib.idle_add(self._downloaded, dest)

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _progress(self, done: int, total: int) -> bool:
        if total:
            self.progress.set_fraction(min(done / total, 1.0))
            self.progress.set_text(_("Downloading: {done} of {total} MB").format(
                done=f"{done / 1e6:.1f}", total=f"{total / 1e6:.1f}"))
        else:
            self.progress.pulse()
        return False

    def _failed(self, message: str) -> bool:
        self.progress_group.set_visible(False)
        self.banner.set_title(message)
        self.banner.set_revealed(True)
        self.later.set_label(_("Later"))
        self.action.set_sensitive(True)
        return False

    def _downloaded(self, installer: Path) -> bool:
        if self._cancel.is_set():
            # "Later" once the last chunk was in: not started (the file goes
            # with the other downloads, at the next start)
            return False
        self.progress.set_fraction(1.0)
        self.progress.set_text(_("Starting the installer…"))
        try:
            # Setup asks Spola to quit (rclone stopped with its state saved),
            # replaces the files and starts it again; its progress shows
            args = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
            if updates.per_user_install():
                args.append("/CURRENTUSER")         # as it was installed: no elevation
            subprocess.Popen(args, close_fds=True)
        except OSError as e:
            self._failed(_("Cannot start the installer: {error}").format(error=e.strerror or e))
        return False
