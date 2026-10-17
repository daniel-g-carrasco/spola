"""The About window's Troubleshooting text, built from a stand-in engine."""

import locale
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola import window  # noqa: E402
from spola.config import Config, Pair  # noqa: E402
from spola.paths import Paths  # noqa: E402


def spin(cond, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        if cond():
            return True
        context.iteration(False)
        time.sleep(0.01)
    return cond()


class AboutTest(unittest.TestCase):
    def test_the_troubleshooting_text_is_there_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "config", Path(tmp) / "state", Path(tmp) / "data")
            (paths.data / "backup" / "p1").mkdir(parents=True)
            (paths.data / "backup" / "p1" / "old.txt").write_text("x" * 100)
            cfg = Config(user="x@example.org", pairs=[Pair("p1", "/h/docs", "/docs")])
            engine = SimpleNamespace(cfg=cfg, paths=paths, rclone=None, creds=None, space=None, runtime={},
                                     blocked_reason=lambda: "")
            parent = Adw.Window()
            parent.present()
            window.about_dialog(parent, engine)
            dialog = parent.get_visible_dialog()
            self.assertIsInstance(dialog, Adw.AboutDialog)
            self.assertIn("Spola ", dialog.get_debug_info())
            self.assertIn("[p1] /h/docs <-> /docs", dialog.get_debug_info())
            self.assertIn("x***@example.org", dialog.get_debug_info())
            self.assertTrue(spin(lambda: "rclone version" in dialog.get_debug_info()), dialog.get_debug_info())
            self.assertIn("backups: 1 files, 100 B", dialog.get_debug_info())
            self.assertEqual(dialog.get_debug_info_filename(), "spola-debug-info.txt")
            dialog.force_close()
            parent.destroy()
