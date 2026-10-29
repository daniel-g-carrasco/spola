"""Desktop CLI sharing resolves local paths before opening the remote dialog."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _setup  # noqa: F401

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None

from gi.repository import GLib

from spola.app import SpolaApp
from spola.config import Pair


class SharingRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "work"
        self.root.mkdir()
        self.pair = Pair("p1", str(self.root), "/remote/work", initialized=True)
        self.app = SpolaApp()
        self.app.engine = SimpleNamespace(cfg=SimpleNamespace(pairs=[self.pair], mount_id="m1"),
                                          creds=SimpleNamespace(user="u", password="p"), rclone=None, paths=None)
        self.presented, self.told = [], []
        self.window = SimpleNamespace(present=lambda: None)
        self.app._window = lambda: self.window
        self.app._tell = self.told.append
        self.app.activate = lambda: None
        presented = self.presented

        class SharingDialog:
            def __init__(dialog, window, remote):
                dialog.window, dialog.remote = window, remote

            def present(dialog, window):
                self.assertIs(window, dialog.window)
                presented.append((window, dialog.remote))

        self.dialog_module = SimpleNamespace(SharingDialog=SharingDialog)

    def invoke(self, name, path):
        options = GLib.VariantDict.new(None)
        options.insert_value(name, GLib.Variant("s", path))
        with mock.patch.dict("sys.modules", {"spola.sharing_dialog": self.dialog_module}):
            return self.app.do_command_line(SimpleNamespace(get_options_dict=lambda: options))

    def test_new_and_legacy_options_open_read_only_dialog_for_remote_path(self):
        # The selected file is not local yet. Koofr decides whether it exists remotely.
        path = str(self.root / "new.txt")
        for option in ("share", "copy-link"):
            with self.subTest(option=option):
                self.presented.clear()
                self.assertEqual(self.invoke(option, path), 0)
                self.assertEqual(self.presented, [(self.window, "/remote/work/new.txt")])
                self.assertFalse(self.told)

    def test_invalid_selection_does_not_open_sharing(self):
        for initialized, credentials, path in (
                (True, self.app.engine.creds, "/outside/file.txt"),
                (True, None, str(self.root / "new.txt")),
                (True, self.app.engine.creds, str(self.root / ".spola-root")),
                (False, self.app.engine.creds, str(self.root / "new.txt"))):
            with self.subTest(initialized=initialized, path=path, signed_in=credentials is not None):
                self.pair.initialized = initialized
                self.app.engine.creds = credentials
                self.told.clear()
                self.invoke("share", path)
                self.assertFalse(self.presented)
                self.assertEqual(len(self.told), 1)

    def test_custom_exclusions_are_refused_before_dialog_loading(self):
        from spola.paths import Paths
        base = Path(self.tmp.name)
        paths = Paths(base / "config", base / "state", base / "data")
        filters = paths.filters_file(self.pair.id)
        filters.parent.mkdir(parents=True)
        filters.write_text("- confidential/**\n")
        self.app.engine.paths = paths
        self.invoke("share", str(self.root / "confidential" / "secret.txt"))
        self.assertFalse(self.presented)
        self.assertEqual(len(self.told), 1)

    def test_unreadable_exclusions_do_not_open_sharing(self):
        from spola.paths import Paths
        base = Path(self.tmp.name)
        paths = Paths(base / "config", base / "state", base / "data")
        filters = paths.filters_file(self.pair.id)
        filters.parent.mkdir(parents=True)
        filters.write_bytes(b"\xff")
        self.app.engine.paths = paths
        self.invoke("share", str(self.root / "new.txt"))
        self.assertFalse(self.presented)
        self.assertEqual(len(self.told), 1)


if __name__ == "__main__":
    unittest.main()
