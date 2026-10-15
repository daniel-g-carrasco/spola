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
from .engine import Engine, Status  # noqa: E402
from .i18n import _  # noqa: E402
from .paths import Paths, in_flatpak  # noqa: E402
from .tray import MenuItem, TrayIcon  # noqa: E402
from .ui import progress_text, run_in_thread, status_badge, status_text, time_left, tray_icon  # noqa: E402
from .window import MainWindow, PreferencesDialog, about_dialog  # noqa: E402

SOURCE_ICONS = Path(__file__).resolve().parent.parent.parent / "data" / "icons"
UPDATE_CHECK_DELAY_S = 60


class SpolaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without opening the window"), None)
        # File Explorer's entries (explorer.py), to the running Spola
        self.add_main_option("open-on-koofr", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
        self.add_main_option("copy-link", 0, GLib.OptionFlags.HIDDEN, GLib.OptionArg.STRING, "", None)
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
        self.hold()     # keep running with no window: released by app.quit
        GLib.set_application_name(APP_NAME)
        if not in_flatpak() and SOURCE_ICONS.is_dir():
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(SOURCE_ICONS))

        paths = Paths.from_env()
        cfg = config.load(paths.config_file)
        self.engine = Engine(paths, cfg)
        self.engine.app = self
        self.engine.inhibit = lambda reason: self.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND, reason)
        self.engine.uninhibit = self.uninhibit
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
                              ("about", lambda: about_dialog(self._window())),
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
                                on_quit=self._quit, has_quit=lambda: self._quit_done)
            self.tray.start()
            self.sync_service = None
        else:
            self.tray = TrayIcon(APP_ID, APP_NAME, on_activate=self.activate, icon_theme_path=icon_path)
            self.tray.start(self.get_dbus_connection())
            self.sync_service = SyncService(self.engine, lambda what: self._review(what) if self.engine.cfg.pair(what)
                                            else self.activate(),
                                            open_url=lambda url: Gtk.UriLauncher.new(url).launch(None, None, None, None),
                                            show_link=self._show_link, tell=self._tell)
            self.sync_service.start(self.get_dbus_connection())
        self._refresh_tray()
        self.engine.start()
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
            except (OSError, FileNotFoundError):
                pass
        if WINDOWS:
            # once a day, a while after start: the syncs come first
            GLib.timeout_add_seconds(UPDATE_CHECK_DELAY_S, self._daily_update_check)

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

    def _load_credentials(self) -> None:
        if self.engine.dev_remote:
            self.engine.set_credentials(Credentials(os.environ.get("SPOLA_DEV_USER") or "dev@localhost", ""))
            return
        user = self.engine.cfg.user
        if not user or self.engine.rclone is None:
            return
        try:
            password = secrets.lookup(user)
        except GLib.Error:
            password = None
        if password:
            try:
                self.engine.set_credentials(Credentials.from_password(user, password, self.engine.rclone))
            except Exception:
                pass

    def do_command_line(self, command_line):
        options = command_line.get_options_dict()
        for name, handler in (("open-on-koofr", self._open_on_koofr), ("copy-link", self._copy_link)):
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
            except OSError:
                pass

    def _in_pair(self, path: str):
        from . import explorer
        found = explorer.locate(self.engine.cfg.pairs, path)
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

    def _copy_link(self, path: str) -> None:
        from . import explorer, koofr_api
        from .rclone import default_excluded, koofr_env, no_window, remote_spec
        found = self._in_pair(path)
        creds, rclone = self.engine.creds, self.engine.rclone
        if found is None:
            return
        if creds is None or rclone is None:
            self._tell(_("Not signed in to Koofr"))
            return
        pair, relative = found
        refusal = explorer.link_refusal(pair, relative, os.path.isdir(path), default_excluded)
        if refusal:
            self._tell(refusal)
            return
        argv = [rclone, "link", remote_spec(explorer.remote_path(pair, relative)),
                "--config", str(self.engine.paths.rclone_config)]
        name = os.path.basename(path.rstrip("\\/")) or path

        def work() -> str:
            import subprocess
            proc = subprocess.run(argv, env=koofr_env(creds.user, creds.obscured), capture_output=True,
                                  text=True, timeout=60, **no_window())
            url = koofr_api.share_page(proc.stdout.strip()) if proc.returncode == 0 else ""
            if not url:
                raise OSError(explorer.link_error(name, proc.stderr))
            return url

        def done(url, error) -> None:
            if error:
                self._tell(str(error))
                return
            Gdk.Display.get_default().get_clipboard().set(url)
            self.tray.notify(_("Koofr link copied"), url)

        run_in_thread(work, done)

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

    def _show_link(self, name: str, url: str) -> None:
        """A share link asked from a file manager that can only run a command
        (Dolphin, Thunar): shown with a Copy button, in front, so the copy
        works on Wayland too."""
        window = self._window()
        window.present()
        entry = Gtk.Entry(text=url, editable=False, hexpand=True)
        dialog = Adw.AlertDialog(heading=_("Koofr Share Link"), body=name, extra_child=entry)
        dialog.add_response("close", _("Close"))
        dialog.add_response("copy", _("Copy"))
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("copy")
        dialog.connect("response", lambda _d, r: window.get_clipboard().set(url) if r == "copy" else None)
        dialog.present(window)

    def _tell(self, message: str) -> None:
        if WINDOWS:
            self.tray.notify(APP_NAME, message)
            return
        note = Gio.Notification.new(APP_NAME)
        note.set_body(message)
        self.send_notification("file-manager", note)

    def _review(self, pair_id: str) -> None:
        """From a notification: show the window on what needs attention."""
        self.activate()
        if self.engine.cfg.pair(pair_id):
            self._window().review(pair_id)

    def _on_attention(self, _engine, pair_id: str, kind: str, message: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        name = pair.name if pair else APP_NAME
        if kind == "preview-ready" and self.window is not None and self.window.is_active():
            # the window is in front: it says so there, with the button to look
            self.window.toast_review(_("The preview of “{name}” is ready").format(name=name), pair_id)
            return
        titles = {
            "conflicts": _("Conflicts in “{name}”"),
            "resolved": _("Conflicts resolved in “{name}”"),
            "preview-ready": _("The preview of “{name}” is ready"),
            "confirm": _("“{name}” is waiting for your confirmation"),
            "repair": _("“{name}” needs a repair"),
            "error": _("“{name}” cannot be synced"),
            "refused": _("Some files in “{name}” are not synced"),
        }
        if WINDOWS:
            # GLib has no notifications on Windows: a toast of Spola's own
            self.tray.notify(titles.get(kind, "{name}").format(name=name), message, lambda: self._review(pair_id),
                             tag=f"{kind}-{pair_id}")
            return
        note = Gio.Notification.new(titles.get(kind, "{name}").format(name=name))
        note.set_body(message)
        note.set_default_action_and_target("app.review", GLib.Variant("s", pair_id))
        if kind in ("confirm", "repair", "error"):
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
            if self.tray:
                self.tray.stop()
            if self.sync_service:
                self.sync_service.stop()
            if self.explorer_status:
                self.explorer_status.stop()
            self.release()
            self.quit()
            self._quit_done = True

        if self.window:
            self.window.save_state()

        self.engine.shutdown(done)


def main(argv=None) -> int:
    return SpolaApp().run(sys.argv if argv is None else argv)
