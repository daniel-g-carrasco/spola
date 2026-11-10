#!/usr/bin/env python3
"""A newer version is a banner in the window for as long as the window is
there, also when it was found while Spola had no window (in the tray); the
dialog opens only when asked for; a notification when the window is not in
front. The window and the application are too large to build here (they
need the engine): their two methods are run on stand-ins that hold only
what the methods touch."""

import locale
import types
import unittest
from unittest import mock

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola.app import updates  # noqa: E402
from spola.gui import application  # noqa: E402
from spola.gui.window import MainWindow  # noqa: E402

RELEASE = updates.Release("99.0.0", "https://example/release", "Notes", [])


class FakeWindow:
    def __init__(self, active: bool):
        self.active, self.shown = active, []
        self.update_banner = Adw.Banner(button_label="Update…", revealed=False)
        self.update_banner.connect("button-clicked", lambda *_: self._update_asked())
        self._update_asked = lambda: None

    def is_active(self) -> bool:
        return self.active

    show_update = MainWindow.show_update


def app_with(window):
    app = types.SimpleNamespace(window=window, dialogs=[], notes=[])
    app._show_update = lambda release: app.dialogs.append(release.version)
    app.tray = types.SimpleNamespace(notify=lambda title, body, on_click, tag=None: app.notes.append((title, on_click)))
    return app


def check(app, manual: bool) -> None:
    with mock.patch.object(application, "run_in_thread", lambda work, done: done(RELEASE, None)):
        application.SpolaApp.check_updates(app, manual)


class BannerTest(unittest.TestCase):
    def test_the_window_says_it_and_its_button_asks(self):
        window, asked = FakeWindow(active=True), []
        window.show_update("99.0.0", lambda: asked.append(1))
        self.assertTrue(window.update_banner.get_revealed())
        self.assertIn("99.0.0", window.update_banner.get_title())
        window.update_banner.emit("button-clicked")
        self.assertEqual(asked, [1])

    def test_found_by_itself_with_the_window_in_front_a_banner_and_nothing_else(self):
        app = app_with(FakeWindow(active=True))
        check(app, manual=False)
        self.assertTrue(app.window.update_banner.get_revealed())
        self.assertEqual((app.dialogs, app.notes), ([], []))
        app.window.update_banner.emit("button-clicked")
        self.assertEqual(app.dialogs, ["99.0.0"])

    def test_found_by_itself_with_the_window_behind_a_banner_and_a_notification(self):
        app = app_with(FakeWindow(active=False))
        check(app, manual=False)
        self.assertTrue(app.window.update_banner.get_revealed())
        self.assertEqual(app.dialogs, [])
        self.assertEqual(len(app.notes), 1)
        app.notes[0][1]()                               # the notification clicked
        self.assertEqual(app.dialogs, ["99.0.0"])

    def test_found_with_no_window_it_waits_for_the_window(self):
        app = app_with(None)
        check(app, manual=False)
        self.assertEqual(len(app.notes), 1)
        self.assertIs(app.update_found, RELEASE)
        # the window made later shows it (SpolaApp._window)
        made = FakeWindow(active=True)
        with mock.patch.object(application, "MainWindow", lambda _app, _engine: made):
            app.engine = None
            self.assertIs(application.SpolaApp._window(app), made)
        self.assertTrue(made.update_banner.get_revealed())
        made.update_banner.emit("button-clicked")
        self.assertEqual(app.dialogs, ["99.0.0"])

    def test_asked_for_from_the_menu_the_dialog_at_once(self):
        app = app_with(FakeWindow(active=True))
        check(app, manual=True)
        self.assertEqual(app.dialogs, ["99.0.0"])
        self.assertEqual(app.notes, [])
        self.assertTrue(app.window.update_banner.get_revealed())

    def test_nothing_newer_and_nothing_shows(self):
        app = app_with(FakeWindow(active=True))
        old = updates.Release("0.0.1", "https://example/release", "", [])
        with mock.patch.object(application, "run_in_thread", lambda work, done: done(old, None)):
            application.SpolaApp.check_updates(app, manual=False)
        self.assertFalse(app.window.update_banner.get_revealed())
        self.assertEqual((app.dialogs, app.notes), ([], []))


if __name__ == "__main__":
    unittest.main()
