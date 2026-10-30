"""The GApplication: stays alive in the background (hold), owns the engine,
the tray icon and the window; closing the window only hides it."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import (APP_ID, APP_NAME, WINDOWS, __version__, autostart, config, file_managers, portal, recent,  # noqa: E402
               secrets)
from .account import Credentials  # noqa: E402
from .dbus_service import SyncService  # noqa: E402
from .status import StatusFile  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .i18n import _  # noqa: E402
from .log import complain  # noqa: E402
from .paths import Paths, in_flatpak  # noqa: E402
from .tray import MenuItem, TrayIcon  # noqa: E402
from .ui import progress_text, run_in_thread, status_badge, status_text, time_left, tray_icon  # noqa: E402
from .window import MainWindow, PreferencesDialog, about_dialog  # noqa: E402

SOURCE_ICONS = Path(__file__).resolve().parent.parent.parent / "data" / "icons"
UPDATE_CHECK_DELAY_S = 60


def settings_notice_text(notice: config.Notice) -> str:
    """What load() had to do, in the user's words."""
    if notice.kind == "recovered":
        return _("The settings file was damaged: the previous save was used. "
                 "The damaged file is kept at {path}.").format(path=notice.detail)
    if notice.kind == "fresh":
        return _("The settings file was damaged and there was no previous save: Spola starts with no folders. "
                 "Add them again (nothing is copied before you confirm an alignment). "
                 "The damaged file is kept at {path}.").format(path=notice.detail)
    if notice.kind == "missing":
        return _("The settings file was missing: the previous save was used.")
    if notice.kind == "newer":
        return _("The settings were saved by a newer Spola (format {version}): "
                 "what this version does not know is kept untouched.").format(version=notice.detail)
    return f"{notice.kind} {notice.detail}"


def larger_system_font(factor: float = 7 / 6) -> int:
    """Windows: the system font a size larger (Segoe UI 9 -> 10.5 points).
    libadwaita is drawn for an 11-point font, and at 100% the smaller text it
    derives from the system's 9 points (subtitles, captions) is hard to read.
    The factor applies to the size Windows gives, so a larger text size
    chosen in Windows' settings is followed, also when it changes. Returns
    the id of the handler that follows it (0: no settings)."""
    from gi.repository import Pango
    settings = Gtk.Settings.get_default()
    if settings is None:
        return 0
    ours = [""]

    def apply(*_args):
        name = settings.props.gtk_font_name
        if not name or name == ours[0]:
            return
        font = Pango.FontDescription.from_string(name)
        size = font.get_size()
        if size <= 0 or font.get_size_is_absolute():
            return
        half_point = Pango.SCALE // 2
        font.set_size(round(size * factor / half_point) * half_point)
        ours[0] = font.to_string()
        settings.props.gtk_font_name = ours[0]

    apply()
    return settings.connect("notify::gtk-font-name", apply)


def native_windows() -> None:
    """Windows: each window of Spola, as it shows (the main one and every
    dialog, a window of its own with the system's title bar), gets the title
    bar the content has, dark or light, and Spola.exe's icon at the sizes
    the title bar and Alt+Tab draw (a run from the sources has no icon of
    its own to give)."""
    from . import win32
    style = Adw.StyleManager.get_default()
    exe = os.path.normpath(sys.executable) if getattr(sys, "frozen", False) else ""

    def native(*_args):
        win32.dark_title_bars(style.get_dark())
        if exe:
            win32.window_icons(exe)

    def added(model, position, _removed, count):
        for i in range(position, position + count):
            model.get_item(i).connect("map", native)

    toplevels = Gtk.Window.get_toplevels()
    added(toplevels, 0, 0, toplevels.get_n_items())
    toplevels.connect("items-changed", added)
    style.connect("notify::dark", native)


class SpolaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without opening the window"), None)
        # File Explorer's entries (explorer.py), to the running Spola
        self.add_main_option("open-on-koofr", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.add_main_option("share", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.add_main_option("copy-link", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.add_main_option("versions", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.add_main_option("deleted", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.engine: Engine | None = None
        self.window: MainWindow | None = None
        self.tray: TrayIcon | None = None
        self._quitting = False
        self._quit_done = False
        self.explorer_status = None         # Windows: the sync status in File Explorer
        self._last_summary = ""

    # -- start-up -------------------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        if WINDOWS:
            larger_system_font()
            native_windows()
        self.hold()     # keep running with no window: released by app.quit
        GLib.set_application_name(APP_NAME)
        if not in_flatpak() and SOURCE_ICONS.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(SOURCE_ICONS))

        paths = Paths.from_env()
        cfg = config.load(paths.config_file)
        self.engine = Engine(paths, cfg)
        for notice in cfg.notices:
            print(f"spola: settings: {notice.kind} {notice.detail} {notice.reason}".rstrip(), file=sys.stderr, flush=True)
            GLib.idle_add(self._on_attention, self.engine, "", "settings", settings_notice_text(notice))
        self.engine.app = self
        self._sleep_guard()
        self.engine.connect("pair-changed", lambda *_: self._refresh_tray())
        self.engine.connect("pairs-changed", lambda *_: self._refresh_tray())
        self.engine.connect("pairs-changed", self.register_explorer)
        self.engine.connect("status-changed", lambda *_: self._refresh_tray())
        self.engine.connect("attention", self._on_attention)
        self._load_credentials()

        review = Gio.SimpleAction.new("review", GLib.VariantType.new("s"))
        review.connect("activate", lambda _a, param: self._review(param.get_string()))
        self.add_action(review)
        for name, handler in (("quit", self._quit), ("show", lambda: self.activate()),
                              ("sync-all", lambda: self.engine.sync_all("manual")),
                              ("pause-all", lambda: self.engine.set_paused_all(not self.engine.cfg.paused_all)),
                              ("about", lambda: about_dialog(self._window(), self.engine)),
                              ("import-koofr-desktop", lambda: self._window().import_koofr_desktop()),
                              ("check-updates", lambda: self.check_updates(manual=True)),
                              ("preferences", lambda: PreferencesDialog(self._window()).present(self._window()))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("window.close", ["<Control>w"])

        icon_path = str(SOURCE_ICONS) if not in_flatpak() and SOURCE_ICONS.is_dir() else ""
        if WINDOWS:
            # the notification area; no file manager speaks D-Bus there
            from .wintray import WinTray
            self.tray = WinTray(APP_ID, APP_NAME, on_activate=self.activate, icon_theme_path=icon_path,
                                on_quit=self._quit, has_quit=lambda: self._quit_done,
                                on_suspend=self.engine.before_sleep, on_resume=self.engine.after_sleep)
            self.tray.start()
            self.sync_service = None
        else:
            self._icon_path = icon_path
            self.tray = None
            self.apply_tray()
            self.sync_service = SyncService(self.engine, lambda what: self._review(what) if self.engine.cfg.pair(what)
                                            else self.activate(),
                                            open_url=lambda url: Gtk.UriLauncher.new(url).launch(None, None, None, None),
                                            tell=self._tell,
                                            show_versions=self._show_versions, show_share=self._share)
            self.sync_service.start(self.get_dbus_connection())
        self._refresh_tray()
        self.engine.start()
        # what Spola is doing, for the desktop's own widgets (the Noctalia plugin)
        self.status_file = StatusFile(self.engine) if not WINDOWS else None
        self.register_explorer()
        # (SPOLA_DEV_EXPLORER, a developer aid: also from the sources)
        if WINDOWS and (getattr(sys, "frozen", False) or os.environ.get("SPOLA_DEV_EXPLORER")):
            from .explorer_status import ExplorerStatus
            exe = os.path.normpath(sys.executable).replace("/", "\\")
            self.explorer_status = ExplorerStatus(self.engine, f"{exe},0", __version__)
            self.explorer_status.update()
        autostart.apply(self.engine.cfg.autostart, self.get_dbus_connection())
        if self.engine.cfg.nautilus_integration and not WINDOWS:
            try:
                file_managers.install()      # a newer Spola brings a newer extension
            except OSError as e:        # said; the extension already there keeps working
                complain("updating the file manager extension", e)
        if WINDOWS:
            # once a day, a while after start: the syncs come first
            GLib.timeout_add_seconds(UPDATE_CHECK_DELAY_S, self._daily_update_check)
            from . import updates
            run_in_thread(updates.clean_downloads, lambda *_: None)     # the installers of earlier updates

    # -- a newer Spola (Windows: on Linux the Flatpak brings it) ----------------------

    def _daily_update_check(self) -> bool:
        cfg = self.engine.cfg
        today = time.strftime("%Y-%m-%d")
        if cfg.check_updates and cfg.last_update_check != today:
            cfg.last_update_check = today
            self.engine.save_config()
            self.check_updates(manual=False)
        return False

    def check_updates(self, manual: bool) -> None:
        from . import updates

        def done(release, error) -> None:
            if error:
                if manual:
                    self._window().toast(_("The check for updates failed: {error}").format(error=error))
                return
            if not updates.is_newer(release.version):
                if manual:
                    self._window().toast(_("Spola {version} is up to date").format(version=__version__))
                return
            if manual or (self.window is not None and self.window.is_active()):
                self._show_update(release)
            else:
                self.tray.notify(_("Spola {version} is available").format(version=release.version),
                                 _("Click to see what is new and update."), lambda: self._show_update(release),
                                 tag="update")

        run_in_thread(updates.latest_release, done)

    def _show_update(self, release) -> None:
        from .update_dialog import UpdateDialog
        window = self._window()
        window.present()
        UpdateDialog(release).present(window)

    def _sleep_guard(self) -> None:
        """No sleep while a sync runs: logind's lock where it is (every Linux
        desktop; the Inhibit portal does nothing under niri or sway), the
        desktop's inhibit otherwise; and a word before a sleep that comes
        regardless, so that bisync stops gracefully and goes on after."""
        desktop = lambda reason: self.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND, reason)  # noqa: E731
        self.sleep = None
        if WINDOWS:
            self.engine.inhibit, self.engine.uninhibit = desktop, self.uninhibit
            return
        from .sleep import SleepWatch
        self.sleep = SleepWatch(self.engine.before_sleep, self.engine.after_sleep)
        self.sleep.start()

        def inhibit(reason):
            cookie = self.sleep.inhibit(reason)
            if cookie:
                return ("logind", cookie)
            cookie = desktop(reason)
            return ("desktop", cookie) if cookie else 0     # 0: no lock, nothing to release after

        def uninhibit(cookie):
            kind, value = cookie
            (self.sleep.release if kind == "logind" else self.uninhibit)(value)
        self.engine.inhibit, self.engine.uninhibit = inhibit, uninhibit

    def _load_credentials(self) -> None:
        if self.engine.dev_remote:
            self.engine.set_credentials(Credentials(os.environ.get("SPOLA_DEV_USER") or "dev@localhost", ""))
            return
        user = self.engine.cfg.user
        if not user or self.engine.rclone is None:
            return
        # said when it fails: the window then only shows "Not signed in"
        try:
            password = secrets.lookup(user)
        except (GLib.Error, OSError) as e:      # the keyring, the Credential Manager
            complain("reading the Koofr password", e)
            password = None
        if password:
            try:
                self.engine.set_credentials(Credentials.from_password(user, password, self.engine.rclone))
            except Exception as e:
                complain("using the Koofr password", e)

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        for name, handler in (("open-on-koofr", self._open_on_koofr), ("share", self._share),
                              ("copy-link", self._copy_link),
                              ("versions", lambda path: self._versions_of(path, "file")),
                              ("deleted", lambda path: self._versions_of(path, "deleted"))):
            value = options.lookup_value(name, GLib.VariantType.new("s"))
            if value is not None:
                handler(value.get_string())
                return 0
        if not options.contains("background"):
            self.activate()
        return 0

    # -- File Explorer's entries ---------------------------------------------------

    def register_explorer(self, *_args) -> None:
        """The entries for the pairs of now; only from the installed or the
        portable build (a run from the sources has no Spola.exe to call)."""
        if WINDOWS and getattr(sys, "frozen", False):
            from . import explorer
            try:
                explorer.register(sys.executable, self.engine.cfg.pairs if self.engine.cfg.explorer_menu else [])
            except OSError as e:        # said; the entries of before stay
                complain("writing File Explorer's entries", e)

    def _in_pair(self, path: str):
        from . import places
        found = places.locate(self.engine.cfg.pairs, path)
        if found is None:
            self._tell(_("“{name}” is not in a folder synced with Koofr.").format(name=os.path.basename(path) or path))
        return found

    def _open_on_koofr(self, path: str) -> None:
        from . import explorer
        found = self._in_pair(path)
        if found is not None:
            pair, relative = found
            url = explorer.page_url(self.engine.cfg.mount_id, pair, relative, os.path.isdir(path))
            Gio.AppInfo.launch_default_for_uri(url, None)

    def _versions_of(self, path: str, kind: str) -> None:
        """"Previous Versions…" of a file, "Recently Deleted Files…" in a
        folder (a file given for the deleted ones: the folder it is in), as
        the file managers ask over D-Bus."""
        found = self._in_pair(path)
        if found is not None:
            pair, relative = found
            folder = not relative or os.path.isdir(path)
            if kind == "deleted" or folder:
                self._show_versions(pair.id, relative if folder else os.path.dirname(relative), "deleted")
            else:
                self._show_versions(pair.id, relative, "file")

    def _copy_link(self, path: str) -> None:
        """Compatibility for installed menu entries from earlier releases."""
        self._share(path)

    def _share(self, path: str) -> None:
        from . import explorer
        from .rclone import default_excluded
        found = self._in_pair(path)
        if found is None:
            return
        if self.engine.creds is None:
            self._tell(_("Not signed in to Koofr"))
            return
        pair, relative = found
        refusal = explorer.link_refusal(pair, relative, os.path.isdir(path), default_excluded, self.engine.paths)
        if refusal:
            self._tell(refusal)
            return
        from .sharing_dialog import SharingDialog
        window = self._window()
        window.present()
        SharingDialog(window, explorer.remote_path(pair, relative)).present(window)

    def do_activate(self):
        self._window().present()
        if os.environ.get("SPOLA_DEV_OPEN_PREVIEW") and self.engine.cfg.pairs:
            GLib.timeout_add(500, lambda: self._window().open_preview(self.engine.cfg.pairs[0].id) and False)
        if os.environ.get("SPOLA_DEV_OPEN_PREFERENCES"):
            GLib.timeout_add(500, lambda: PreferencesDialog(self._window()).present(self._window()) and False)
        if os.environ.get("SPOLA_DEV_OPEN_EXCLUSIONS") and self.engine.cfg.pairs:
            from .window import ExclusionsDialog
            GLib.timeout_add(500, lambda: ExclusionsDialog(self._window(), self.engine.cfg.pairs[0].id)
                             .present(self._window()) and False)
        if os.environ.get("SPOLA_DEV_OPEN_REVIEW") and self.engine.cfg.pairs:
            tries = [0]

            def review_first():          # waits for a pair that needs a look, then opens it
                waiting = [p for p in self.engine.cfg.pairs
                           if self.engine.runtime[p.id].conflicts or self.engine.runtime[p.id].refused]
                tries[0] += 1
                if not waiting and tries[0] < 80:
                    return True
                self._window().review((waiting or self.engine.cfg.pairs)[0].id)
                return False
            GLib.timeout_add(500, review_first)
        dialog = os.environ.get("SPOLA_DEV_OPEN_DIALOG")      # resolve-all, rename
        if dialog in ("resolve-all", "rename"):
            waited = [0]

            def open_dialog():           # waits for a pair with something to show, then opens the dialog
                from .window import RenameDialog, ResolveAllDialog
                for pair in self.engine.cfg.pairs:
                    rt = self.engine.runtime[pair.id]
                    if rt.run is None and dialog == "resolve-all" and rt.conflicts:
                        ResolveAllDialog(self._window(), pair.id, lambda *_: None).present(self._window())
                        return False
                    if rt.run is None and dialog == "rename" and rt.refused:
                        RenameDialog(self._window(), pair.id, list(rt.refused), lambda *_: None).present(self._window())
                        return False
                waited[0] += 1
                return waited[0] < 80
            GLib.timeout_add(500, open_dialog)
        versions_of = os.environ.get("SPOLA_DEV_OPEN_VERSIONS")      # "Projects:budget-2026.ods"
        if versions_of and ":" in versions_of:
            def open_versions():
                from .window import VersionsDialog
                name, rel = versions_of.split(":", 1)
                pair = next((p for p in self.engine.cfg.pairs if p.name == name), None)
                if pair:
                    VersionsDialog(self._window(), pair.id, rel, "file").present(self._window())
            GLib.timeout_add(500, lambda: open_versions() and False)
        pair_name = os.environ.get("SPOLA_DEV_OPEN_PAIR")
        if pair_name:
            def open_pair():
                pair = next((p for p in self.engine.cfg.pairs if p.name == pair_name), None)
                if pair:
                    self._window().show_pair(pair.id)
            GLib.timeout_add(500, lambda: open_pair() and False)
        add_local = os.environ.get("SPOLA_DEV_OPEN_ADD")
        if add_local is not None:
            def open_add():
                from .add_folder import AddFolderDialog
                dialog = AddFolderDialog(self._window())
                dialog.present(self._window())
                if add_local:
                    dialog.set_local(os.path.join(os.path.expanduser("~"), add_local))
                browse = os.environ.get("SPOLA_DEV_OPEN_ADD_BROWSE")
                if browse:
                    GLib.timeout_add(1500, lambda: dialog.browse(browse) and False)
            GLib.timeout_add(500, lambda: open_add() and False)
        shot = os.environ.get("SPOLA_SCREENSHOT")
        if shot:   # developer aid: save the window as PNG after a few seconds
            GLib.timeout_add(int(float(os.environ.get("SPOLA_SCREENSHOT_DELAY", "3")) * 1000),
                             lambda: self._screenshot(shot) and False)

    def _screenshot(self, path: str) -> None:
        window = self._window()
        width, height = window.get_width(), window.get_height()
        scale = float(os.environ.get("SPOLA_SCREENSHOT_SCALE", "1"))
        snapshot = Gtk.Snapshot()
        snapshot.scale(scale, scale)       # drawn at that scale, not stretched afterwards
        Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            gi.require_version("Graphene", "1.0")
            from gi.repository import Graphene
            area = Graphene.Rect().init(0, 0, width * scale, height * scale)
            window.get_renderer().render_texture(node, area).save_to_png(path)
        if os.environ.get("SPOLA_SCREENSHOT_QUIT", "1") == "1":
            self._quit()

    def _window(self) -> MainWindow:
        if self.window is None:
            self.window = MainWindow(self, self.engine)
        return self.window

    # -- tray, status, notifications ------------------------------------------------------

    def apply_tray(self) -> None:
        """The notification area icon as the preference says: off when a
        widget of the desktop (the Noctalia plugin) shows the status instead."""
        if WINDOWS or self.engine is None:
            return
        if self.engine.cfg.tray_icon and self.tray is None:
            self.tray = TrayIcon(APP_ID, APP_NAME, on_activate=self.activate, icon_theme_path=self._icon_path)
            self.tray.start(self.get_dbus_connection())
            self._last_summary = ""
            self._refresh_tray()
        elif not self.engine.cfg.tray_icon and self.tray is not None:
            self.tray.stop()
            self.tray = None

    def _refresh_tray(self) -> None:
        if self.tray is None or self.engine is None:
            return
        engine = self.engine
        overall = engine.overall() if engine.creds else Status.PAUSED
        reason = "" if engine.creds else _("Not signed in to Koofr")
        reason = reason or (engine.blocked_reason() if overall is Status.PAUSED else "")
        summary = reason or status_text(overall)
        progress = ""
        task = engine.active
        if task is not None and not task.dry_run and engine.cfg.pair(task.pair_id):
            # what other sync clients show on hover and at the top of their
            # menu: which folder, how far, how fast, how long still
            pair, rt = engine.cfg.pair(task.pair_id), engine.runtime[task.pair_id]
            summary = _("Syncing “{name}”").format(name=pair.name)
            text, fraction = progress_text(rt.stats, rt.activity.deletes_queued)
            parts = [text] if text else [_("Comparing the two sides…")]
            if fraction is not None:
                parts.insert(0, f"{int(fraction * 100)}%")
            if (left := time_left((rt.stats or {}).get("eta"))):
                parts.append(left)
            progress = " · ".join(parts)
        # the Windows taskbar has no symbolic recolouring: the coloured badge always
        self.tray.set_icon(status_badge(overall) if WINDOWS else tray_icon(overall))
        self.tray.set_tooltip(APP_NAME, summary + (f"\n{progress}" if progress else ""))
        if summary != self._last_summary:
            self._last_summary = summary
            if not WINDOWS:
                portal.set_status(self.get_dbus_connection(), summary)
        paused = engine.cfg.paused_all
        files = []
        transferring = self._transferring()
        if transferring:
            files.append(MenuItem(_("Transferring Now"), children=transferring, name="transferring"))
        files.append(MenuItem(_("Recently Synced Files"), name="recent", children=self._recent() or [
            MenuItem(_("No recently synced files"))]))
        # every item that does something has a name: its number stays the same
        # while the menu changes, so a click on a menu a moment old (a file just
        # finished) never lands on another item (see tray.Ids)
        self.tray.set_menu([
            MenuItem(summary),
            *([MenuItem(progress)] if progress else []),
            MenuItem(separator=True),
            *files,
            MenuItem(separator=True),
            MenuItem(_("Sync Now"), (lambda: engine.sync_all("manual")) if engine.creds and not paused else None,
                     name="sync-now"),
            MenuItem(_("Resume") if paused else _("Pause"), lambda: engine.set_paused_all(not paused),
                     name="resume" if paused else "pause"),
            MenuItem(_("Open Spola"), self.activate, name="open"),
            MenuItem(separator=True),
            MenuItem(_("Quit"), self._quit, name="quit"),
        ])

    def _transferring(self) -> list[MenuItem]:
        """What rclone is copying right now (a few files at a time)."""
        engine = self.engine
        task = engine.active
        rt = engine.runtime.get(task.pair_id) if task and not task.dry_run else None
        pair = engine.cfg.pair(task.pair_id) if rt else None
        if pair is None:
            return []
        items = []
        for path, direction in recent.transferring(rt.transferring):
            folder = os.path.join(pair.local, os.path.dirname(path))
            items.append(MenuItem(os.path.basename(path), lambda p=folder: self._open_folder(p),
                                  icon="send-to-symbolic" if direction == "up" else "folder-download-symbolic",
                                  name=f"{pair.id}:{path}"))
        return items

    def _recent(self) -> list[MenuItem]:
        items = []
        for f in self.engine.recent.items:
            pair = self.engine.cfg.pair(f.pair_id)
            if pair is not None:
                path = os.path.join(pair.local, f.path)
                items.append(MenuItem(os.path.basename(f.path), lambda p=path: self._reveal(p),
                                      icon="send-to-symbolic" if f.direction == "up" else "folder-download-symbolic",
                                      name=f"{f.pair_id}:{f.path}"))
        return items

    def _reveal(self, path: str) -> None:
        """The file selected in the file manager, as Koofr Desktop does; its
        folder if the file is gone since."""
        if os.path.exists(path):
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).open_containing_folder(None, None, None, None)
        else:
            self._open_folder(os.path.dirname(path))

    def _open_folder(self, path: str) -> None:
        while path and not os.path.isdir(path):
            path = os.path.dirname(path)
        if path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(None, None, None, None)

    def _tell(self, message: str, title: str = APP_NAME) -> None:
        if WINDOWS:
            self.tray.notify(title, message)
            return
        note = Gio.Notification.new(title)
        note.set_body(message)
        self.send_notification("file-manager", note)

    def _show_versions(self, pair_id: str, rel: str, kind: str) -> None:
        """From a file manager's menu: the window on the folder, and the
        versions of the file or what was deleted in the folder."""
        from .window import VersionsDialog
        self.activate()
        window = self._window()
        if self.engine.cfg.pair(pair_id) is None:
            return
        window.show_pair(pair_id)
        VersionsDialog(window, pair_id, rel, kind).present(window)

    def _review(self, pair_id: str) -> None:
        """From a notification: show the window on what needs attention."""
        self.activate()
        if self.engine.cfg.pair(pair_id):
            self._window().review(pair_id)

    def _on_attention(self, _engine, pair_id: str, kind: str, message: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        name = pair.name if pair else APP_NAME
        if kind in ("preview-ready", "verify", "verified") and self.window is not None and self.window.is_active():
            # the window is in front: it says so there, with the button to look
            text = _("The preview of “{name}” is ready").format(name=name) if kind == "preview-ready" \
                else _("“{name}”: {what}").format(name=name, what=message)
            self.window.toast_review(text, pair_id)
            return
        titles = {
            "conflicts": _("Conflicts in “{name}”"),
            "resolved": _("Conflicts resolved in “{name}”"),
            "preview-ready": _("The preview of “{name}” is ready"),
            "confirm": _("“{name}” is waiting for your confirmation"),
            "repair": _("“{name}” needs a repair"),
            "gone": _("“{name}” stopped to keep your files safe"),
            "error": _("“{name}” cannot be synced"),
            "refused": _("Some files in “{name}” are not synced"),
            "settings": _("A word about Spola's settings"),
            "verify": _("“{name}”: differences between this computer and Koofr"),
            "verified": _("“{name}” verified"),
        }
        if WINDOWS:
            # GLib has no notifications on Windows: a toast of Spola's own
            self.tray.notify(titles.get(kind, "{name}").format(name=name), message, lambda: self._review(pair_id),
                             tag=f"{kind}-{pair_id}")
            return
        note = Gio.Notification.new(titles.get(kind, "{name}").format(name=name))
        note.set_body(message)
        note.set_default_action_and_target("app.review", GLib.Variant("s", pair_id))
        if kind in ("confirm", "repair", "error", "gone", "settings"):
            note.set_priority(Gio.NotificationPriority.HIGH)
        self.send_notification(f"{kind}-{pair_id}", note)

    # -- quitting -------------------------------------------------------------------

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self.window:
            self.window.set_visible(False)
        if self.tray:
            self.tray.set_tooltip(APP_NAME, _("Stopping…"))

        def done():
            if self.status_file is not None:
                self.status_file.stop()
            if self.tray:
                self.tray.stop()
            if self.sync_service:
                self.sync_service.stop()
            if self.sleep is not None:
                self.sleep.stop()
            if self.explorer_status:
                self.explorer_status.stop()
            self.release()
            self.quit()
            self._quit_done = True

        if self.window:
            self.window.save_state()

        self.engine.shutdown(done)


def main(argv=None) -> int:
    if WINDOWS:
        # the system title bar on the main window, before GTK starts (see
        # MainWindow): with GTK's own, Windows does not snap the window
        os.environ.setdefault("GTK_CSD", "0")
    return SpolaApp().run(sys.argv if argv is None else argv)
