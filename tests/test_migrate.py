#!/usr/bin/env python3
"""Taking over Koofr Desktop's folder pairs from its config.json."""

import json
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola.config import Config, Pair
from spola.migrate import KoofrDesktopActive, candidates, import_koofr_desktop, problem_of


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

    def test_other_mounts_are_left_out(self):
        # Koofr Desktop syncs also folders shared with the account and the
        # services connected to Koofr: another mount, not the same path in
        # the account's own storage
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for d in ("own", "shared", "unknown"):
                (t / d).mkdir()
            conf = t / "config.json"
            conf.write_text(json.dumps({"users": {"u": {"syncs": [
                {"localPath": f"{tmp}/own", "remotePath": "/own", "mountId": "primary"},
                {"localPath": f"{tmp}/shared", "remotePath": "/shared", "mountId": "other"},
                {"localPath": f"{tmp}/unknown", "remotePath": "/unknown"},
            ]}}}))
            report = import_koofr_desktop(Config(mount_id="primary"), conf, active=False, home=tmp)
            self.assertEqual(sorted(Path(i.pair.local).name for i in report.added), ["own", "unknown"])
            self.assertEqual([Path(local).name for local, _why in report.skipped], ["shared"])
            # the primary mount not known yet: as before
            report = import_koofr_desktop(Config(), conf, active=False, home=tmp)
            self.assertEqual(len(report.added), 3)

    def test_candidates_before_anything_is_added(self):
        # the window shows Koofr Desktop's pairs first, each with what keeps
        # it out; of two that overlap, the first can be taken
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for d in ("work", "work/inner", "photos", "mine"):
                (t / d).mkdir(parents=True)
            conf = t / "config.json"
            conf.write_text(json.dumps({"users": {"u": {"syncs": [
                {"localPath": f"{tmp}/work", "remotePath": "/work"},
                {"localPath": f"{tmp}/work/inner", "remotePath": "/inner"},
                {"localPath": f"{tmp}/photos", "remotePath": "/photos", "paused": True},
                {"localPath": f"{tmp}/mine", "remotePath": "/mine"},
            ]}}}))
            cfg = Config(pairs=[Pair("p0", f"{tmp}/mine", "/mine")])
            found = {Path(c.local).name: c for c in candidates(cfg, conf, home=tmp)}
            self.assertEqual(found["work"].problem, "")
            self.assertNotEqual(found["inner"].problem, "")         # inside "work", taken first
            self.assertTrue(found["photos"].paused)
            self.assertEqual(found["photos"].problem, "")
            self.assertNotEqual(found["mine"].problem, "")          # already in Spola
            self.assertEqual(len(cfg.pairs), 1)                     # nothing added yet
            self.assertEqual(problem_of(cfg, found["work"].local, "/work", home=tmp), "")


if __name__ == "__main__":
    unittest.main()
