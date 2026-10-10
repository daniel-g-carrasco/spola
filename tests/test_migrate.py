#!/usr/bin/env python3
"""Taking over Koofr Desktop's folder pairs from its config.json."""

import json
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.config import Config, Pair
from spola.migrate import KoofrDesktopActive, import_koofr_desktop


class ImportTest(unittest.TestCase):
    def test_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for d in ("data/work", "data/imports", "data/photos", "already", "nested/inside"):
                (t / d).mkdir(parents=True)
            (t / "data/photos" / "foto \U0001F600.jpg").write_text("x")
            conf = t / "config.json"
            conf.write_text(json.dumps({"users": {"u": {"syncs": [
                {"localPath": f"{tmp}/data/work", "remotePath": "/data/work/", "paused": False},
                {"localPath": f"{tmp}/data/imports", "remotePath": "/data/imports", "paused": True},
                {"localPath": f"{tmp}/data/photos", "remotePath": "/data/photos", "paused": False},
                {"localPath": f"{tmp}/already", "remotePath": "/already", "paused": False},
                {"localPath": f"{tmp}/nested/inside", "remotePath": "/data/work/sub", "paused": False},
                {"localPath": f"{tmp}/missing", "remotePath": "/missing", "paused": False},
                {"localPath": f"{tmp}/data/work", "remotePath": "/", "paused": False},
            ]}}}))
            cfg = Config(pairs=[Pair("p0", f"{tmp}/already", "/already")])
            with self.assertRaises(KoofrDesktopActive):
                import_koofr_desktop(cfg, conf, active=True)
            report = import_koofr_desktop(cfg, conf, active=False, home=tmp)
            added = {i.pair.local: i for i in report.added}
            self.assertEqual(sorted(added), [f"{tmp}/data/imports", f"{tmp}/data/photos", f"{tmp}/data/work"])
            self.assertTrue(added[f"{tmp}/data/imports"].pair.paused)
            self.assertFalse(added[f"{tmp}/data/work"].pair.paused)
            self.assertEqual(added[f"{tmp}/data/work"].pair.remote, "/data/work")
            self.assertFalse(added[f"{tmp}/data/work"].pair.initialized)
            self.assertIsNotNone(added[f"{tmp}/data/work"].pair.root_ino)
            self.assertEqual([w.code for w in added[f"{tmp}/data/photos"].warnings], ["emoji"])
            self.assertEqual(sorted(local for local, _why in report.skipped),
                             sorted([f"{tmp}/already", f"{tmp}/nested/inside", f"{tmp}/missing", f"{tmp}/data/work"]))
            self.assertEqual(len(cfg.pairs), 4)


if __name__ == "__main__":
    unittest.main()
