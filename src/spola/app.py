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

from . import APP_ID, APP_NAME, config, portal, secrets  # noqa: E402
from .account import Credentials  # noqa: E402
from .engine import Engine, Status  # noqa: E402
from .i18n import _  # noqa: E402
from .paths import Paths, in_flatpak  # noqa: E402
from .tray import MenuItem, TrayIcon  # noqa: E402
from .window import MainWindow, about_dialog, status_icon, status_text  # noqa: E402

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
        self.engine.inhibit = lambda reason: self.inhibit(None, Gtk.ApplicationInhibitFlags.SUSPEND, reason)
        self.engine.uninhibit = self.uninhibit
        self.engine.connect("pair-changed", lambda *_: self._refresh_tray())
        self.engine.connect("pairs-changed", lambda *_: self._refresh_tray())
        self.engine.connect("status-changed", lambda *_: self._refresh_tray())
        self.engine.connect("attention", self._on_attention)
        self._load_credentials()

        for name, handler in (("quit", self._quit), ("show", lambda: self.activate()),
                              ("sync-all", lambda: self.engine.sync_all("manual")),
                              ("pause-all", lambda: self.engine.set_paused_all(not self.engine.cfg.paused_all)),
                              ("about", lambda: about_dialog(self._window()))):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        icon_path = str(SOURCE_ICONS) if not in_flatpak() and SOURCE_ICONS.is_dir() else ""
        self.tray = TrayIcon(APP_ID, APP_NAME, on_activate=self.activate, icon_theme_path=icon_path)
        self.tray.start(self.get_dbus_connection())
        self._refresh_tray()
        self.engine.start()
        portal.request_background(self.get_dbus_connection(),
                                  _("Spola keeps your folders in sync with Koofr in the background."),
                                  True, ["spola", "--background"], lambda *_: None)

    def _load_credentials(self) -> None:
        if self.engine.dev_remote:
            self.engine.set_credentials(Credentials("dev@localhost", ""))
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
        shot = os.environ.get("SPOLA_SCREENSHOT")
        if shot:   # developer aid: save the window as PNG after a few seconds
            GLib.timeout_add(int(float(os.environ.get("SPOLA_SCREENSHOT_DELAY", "3")) * 1000),
                             lambda: self._screenshot(shot) and False)

    def _screenshot(self, path: str) -> None:
        window = self._window()
        width, height = window.get_width(), window.get_height()
        snapshot = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            window.get_renderer().render_texture(node, None).save_to_png(path)
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
        self.tray.set_icon(status_icon(overall))
        self.tray.set_tooltip(APP_NAME, summary)
        if summary != self._last_summary:
            self._last_summary = summary
            portal.set_status(self.get_dbus_connection(), summary)
        paused = engine.cfg.paused_all
        self.tray.set_menu([
            MenuItem(summary),
            MenuItem(separator=True),
            MenuItem(_("Sync Now"), (lambda: engine.sync_all("manual")) if engine.creds and not paused else None),
            MenuItem(_("Resume") if paused else _("Pause"), lambda: engine.set_paused_all(not paused)),
            MenuItem(_("Open Spola"), self.activate),
            MenuItem(separator=True),
            MenuItem(_("Quit"), self._quit),
        ])

    def _on_attention(self, _engine, pair_id: str, kind: str, message: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        name = pair.name if pair else APP_NAME
        titles = {
            "conflicts": _("Conflicts in “{name}”"),
            "confirm": _("“{name}” is waiting for your confirmation"),
            "repair": _("“{name}” needs a repair"),
            "error": _("“{name}” cannot be synced"),
            "refused": _("Some files in “{name}” are not synced"),
        }
        note = Gio.Notification.new(titles.get(kind, "{name}").format(name=name))
        note.set_body(message)
        note.set_default_action("app.show")
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
            self.release()
            self.quit()

        self.engine.shutdown(done)


def main(argv=None) -> int:
    return SpolaApp().run(sys.argv if argv is None else argv)
