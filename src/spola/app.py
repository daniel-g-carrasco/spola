"""The GApplication: stays alive in the background (hold), owns the engine,
the tray icon and the window; closing the window only hides it."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, autostart, config, file_managers, portal, recent, secrets  # noqa: E402
from .account import Credentials  # noqa: E402
from .dbus_service import SyncService  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .i18n import _  # noqa: E402
from .paths import Paths, in_flatpak  # noqa: E402
from .tray import MenuItem, TrayIcon  # noqa: E402
from .ui import progress_text, status_text, time_left, tray_icon  # noqa: E402
from .window import MainWindow, PreferencesDialog, about_dialog  # noqa: E402

SOURCE_ICONS = Path(__file__).resolve().parent.parent.parent / "data" / "icons"


class SpolaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without opening the window"), None)
        self.engine: Engine | None = None
        self.window: MainWindow | None = None
        self.tray: TrayIcon | None = None
        self._quitting = False
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
                              ("preferences", lambda: PreferencesDialog(self._window()).present(self._window()))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("window.close", ["<Control>w"])

        icon_path = str(SOURCE_ICONS) if not in_flatpak() and SOURCE_ICONS.is_dir() else ""
        self.tray = TrayIcon(APP_ID, APP_NAME, on_activate=self.activate, icon_theme_path=icon_path)
        self.tray.start(self.get_dbus_connection())
        self.sync_service = SyncService(self.engine, lambda what: self._review(what) if self.engine.cfg.pair(what)
                                        else self.activate(),
                                        open_url=lambda url: Gtk.UriLauncher.new(url).launch(None, None, None, None),
                                        show_link=self._show_link, tell=self._tell)
        self.sync_service.start(self.get_dbus_connection())
        self._refresh_tray()
        self.engine.start()
        autostart.apply(self.engine.cfg.autostart, self.get_dbus_connection())
        if self.engine.cfg.nautilus_integration:
            try:
                file_managers.install()      # a newer Spola brings a newer extension
            except (OSError, FileNotFoundError):
                pass

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
        if not command_line.get_options_dict().contains("background"):
            self.activate()
        return 0

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
            text, fraction = progress_text(rt.stats)
            parts = [text] if text else [_("Comparing the two sides…")]
            if fraction is not None:
                parts.insert(0, f"{int(fraction * 100)}%")
            if (left := time_left((rt.stats or {}).get("eta"))):
                parts.append(left)
            progress = " · ".join(parts)
        self.tray.set_icon(tray_icon(overall))
        self.tray.set_tooltip(APP_NAME, summary + (f"\n{progress}" if progress else ""))
        if summary != self._last_summary:
            self._last_summary = summary
            portal.set_status(self.get_dbus_connection(), summary)
        paused = engine.cfg.paused_all
        files = []
        transferring = self._transferring()
        if transferring:
            files.append(MenuItem(_("Transferring Now"), children=transferring))
        files.append(MenuItem(_("Recently Synced Files"), children=self._recent() or [
            MenuItem(_("No recently synced files"))]))
        self.tray.set_menu([
            MenuItem(summary),
            *([MenuItem(progress)] if progress else []),
            MenuItem(separator=True),
            *files,
            MenuItem(separator=True),
            MenuItem(_("Sync Now"), (lambda: engine.sync_all("manual")) if engine.creds and not paused else None),
            MenuItem(_("Resume") if paused else _("Pause"), lambda: engine.set_paused_all(not paused)),
            MenuItem(_("Open Spola"), self.activate),
            MenuItem(separator=True),
            MenuItem(_("Quit"), self._quit),
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
                                  icon="send-to-symbolic" if direction == "up" else "folder-download-symbolic"))
        return items

    def _recent(self) -> list[MenuItem]:
        items = []
        for f in self.engine.recent.items:
            pair = self.engine.cfg.pair(f.pair_id)
            if pair is not None:
                path = os.path.join(pair.local, f.path)
                items.append(MenuItem(os.path.basename(f.path), lambda p=path: self._reveal(p),
                                      icon="send-to-symbolic" if f.direction == "up" else "folder-download-symbolic"))
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
        titles = {
            "conflicts": _("Conflicts in “{name}”"),
            "resolved": _("Conflicts resolved in “{name}”"),
            "preview-ready": _("The preview of “{name}” is ready"),
            "confirm": _("“{name}” is waiting for your confirmation"),
            "repair": _("“{name}” needs a repair"),
            "error": _("“{name}” cannot be synced"),
            "refused": _("Some files in “{name}” are not synced"),
        }
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
            self.sync_service.stop()
            self.release()
            self.quit()

        if self.window:
            self.window.save_state()

        self.engine.shutdown(done)


def main(argv=None) -> int:
    return SpolaApp().run(sys.argv if argv is None else argv)
