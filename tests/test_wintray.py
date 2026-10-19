#!/usr/bin/env python3
"""The icon's window on Windows: how Spola answers Windows when it is asked
to quit (Setup before an update, sign-out, shutdown)."""

import sys
import time
import unittest

import _setup  # noqa: F401

if sys.platform != "win32":
    raise unittest.SkipTest("the notification area of Windows")

import locale  # noqa: E402

from gi.repository import GLib  # noqa: E402

_before = locale.setlocale(locale.LC_ALL)
from spola import wintray  # noqa: E402
# GTK, initialised on import, sets the user's locale: the other tests read
# numbers written as in the C locale ("2000", not "2 000")
locale.setlocale(locale.LC_ALL, _before)


class QuitTest(unittest.TestCase):
    def setUp(self):
        self.asked, self.done = [], []

        def on_quit():
            self.asked.append(1)
            # the quit is over a moment later, as when rclone saves its state
            GLib.timeout_add(300, lambda: self.done.append(1) and False)

        self.tray = wintray.WinTray("io.example.test", "Test", on_activate=lambda: None,
                                    on_quit=on_quit, has_quit=lambda: bool(self.done))

    def test_a_sign_out_is_never_refused(self):
        self.assertEqual(self.tray._window_proc(None, wintray.WM_QUERYENDSESSION, 0, 1), 1)
        self.assertEqual(self.asked, [])

    def test_the_end_of_the_session_is_answered_once_the_quit_is_over(self):
        start = time.monotonic()
        self.assertEqual(self.tray._window_proc(None, wintray.WM_ENDSESSION, 1, 1), 0)
        self.assertEqual(self.asked, [1])
        self.assertEqual(self.done, [1])
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
        # a sign-out cancelled by the user (wParam FALSE): nothing to do
        self.assertEqual(self.tray._window_proc(None, wintray.WM_ENDSESSION, 0, 0), 0)
        self.assertEqual(self.asked, [1])

    def test_close_starts_the_quit_from_the_main_loop(self):
        self.assertEqual(self.tray._window_proc(None, wintray.WM_CLOSE, 0, 0), 0)
        self.assertEqual(self.asked, [])
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not self.asked and time.monotonic() < deadline:
            context.iteration(False)
        self.assertEqual(self.asked, [1])


class SleepTest(unittest.TestCase):
    def test_a_sleep_stops_the_sync_and_the_wake_resumes_it(self):
        slept, woke = [], []
        tray = wintray.WinTray("io.example.test", "Test", on_activate=lambda: None,
                               on_suspend=lambda: slept.append(1), on_resume=lambda: woke.append(1))
        self.assertEqual(tray._window_proc(None, wintray.WM_POWERBROADCAST, wintray.PBT_APMSUSPEND, 0), 1)
        self.assertEqual(slept, [1])                    # at once: Windows sleeps soon after the answer
        self.assertEqual(tray._window_proc(None, wintray.WM_POWERBROADCAST, wintray.PBT_APMRESUMEAUTOMATIC, 0), 1)
        self.assertEqual(woke, [])                      # from the main loop
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not woke and time.monotonic() < deadline:
            context.iteration(False)
        self.assertEqual(woke, [1])
        # the power source or the battery changed: nothing to do
        self.assertEqual(tray._window_proc(None, wintray.WM_POWERBROADCAST, 0x000A, 0), 1)
        self.assertEqual((slept, woke), ([1], [1]))


class IconTest(unittest.TestCase):
    def test_a_large_png_for_the_toasts(self):
        import tempfile
        from pathlib import Path
        from gi.repository import GdkPixbuf
        with tempfile.TemporaryDirectory() as tmp:
            tray = wintray.WinTray("io.github.daniel_g_carrasco.spola", "Spola", on_activate=lambda: None,
                                   icon_theme_path=str(_setup.ROOT / "data" / "icons"), cache=Path(tmp))
            png = tray._png("io.github.daniel_g_carrasco.spola", wintray.TOAST_ICON_SIZE)
            self.assertTrue(png.endswith("-256.png"))
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(png)
            self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (256, 256))

    def test_the_svg_also_when_the_theme_has_pngs_of_the_icon(self):
        # the app's own icon is installed as SVG and as PNGs (64, 128, 256):
        # the theme's lookup returns a PNG, the .ico and the toast's icon
        # are drawn from the SVG
        import shutil
        import tempfile
        from pathlib import Path
        from gi.repository import Gdk, Gtk
        name = "io.example.spola-icon-test"
        source = _setup.ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "io.github.daniel_g_carrasco.spola.svg"
        with tempfile.TemporaryDirectory() as tmp:
            for folder in ("scalable", "64x64"):
                (Path(tmp) / "hicolor" / folder / "apps").mkdir(parents=True)
            svg = Path(tmp) / "hicolor" / "scalable" / "apps" / f"{name}.svg"
            shutil.copy(source, svg)
            from gi.repository import GdkPixbuf
            GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), 64, 64).savev(
                str(Path(tmp) / "hicolor" / "64x64" / "apps" / f"{name}.png"), "png", [], [])
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(tmp)
            tray = wintray.WinTray(name, "Test", on_activate=lambda: None, cache=Path(tmp) / "cache")
            self.assertEqual(Path(tray._svg(name)), svg)


if __name__ == "__main__":
    unittest.main()
