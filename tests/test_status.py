"""The status file other programs read (the Noctalia plugin)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _setup  # noqa: F401
from spola import status
from spola.config import Config, Pair
from spola.engine import Status


def fake_engine():
    pairs = [Pair("p1", "/h/work", "/data/work", initialized=True), Pair("p2", "/h/photos", "/data/photos", paused=True)]
    rts = {
        "p1": SimpleNamespace(status=Status.SYNCING, message="Syncing…", task=SimpleNamespace(dry_run=False),
                              run=SimpleNamespace(stopping=False), last_sync=1_800_000_000.0, conflicts=[], refused=[],
                              gone="", safety="", activity=SimpleNamespace(deletes_queued=2),
                              stats={"bytes": 500, "totalBytes": 2000, "transfers": 1, "totalTransfers": 4, "eta": 30,
                                     "speed": 100.0}),
        "p2": SimpleNamespace(status=Status.CONFLICTS, message="", task=None, run=None, last_sync=None,
                              conflicts=["a", "b"], refused=["c"], gone="", safety="", activity=None, stats=None),
    }
    return SimpleNamespace(cfg=Config(user="u", pairs=pairs), runtime=rts, creds=object(), overall=lambda: Status.SYNCING,
                           blocked_reason=lambda: "", active=SimpleNamespace(pair_id="p1", dry_run=False),
                           space=SimpleNamespace(total=1000, used=400, free=600))


class SnapshotTest(unittest.TestCase):
    def test_the_engine_as_plain_data(self):
        data = status.snapshot(fake_engine())
        self.assertEqual((data["format"], data["pid"], data["overall"], data["syncing"]), (1, os.getpid(), "syncing", "p1"))
        self.assertTrue(data["signed_in"])
        self.assertEqual(data["space"], {"total": 1000, "used": 400, "free": 600})
        self.assertEqual(data["needs_you"], [])                     # p2 waits, but it is paused
        p1, p2 = data["pairs"]
        self.assertEqual((p1["status"], p1["syncing"], p1["progress"]["fraction"], p1["progress"]["eta_s"]),
                         ("syncing", True, 0.25, 30))
        self.assertEqual(p1["progress"]["deletes_queued"], 2)
        self.assertEqual((p2["status"], p2["conflicts"], p2["refused"], p2["paused"], p2["progress"]),
                         ("conflicts", 2, 1, True, None))
        json.dumps(data)                                              # plain data throughout
        # not signed in: paused, and said
        engine = fake_engine()
        engine.creds = None
        data = status.snapshot(engine)
        self.assertEqual((data["overall"], data["blocked"], data["signed_in"]), ("paused", "not-signed-in", False))

    def test_a_folder_whose_state_cannot_be_read_is_left_out(self):
        engine = fake_engine()
        engine.runtime["p1"].stats = {"bytes": "many", "totalBytes": 10}   # a bad value: that folder only
        data = status.snapshot(engine)
        self.assertEqual([p["id"] for p in data["pairs"]], ["p2"])

    def test_written_whole_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "status.json"
            status.write(path, {"a": 1})
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertEqual(os.listdir(path.parent), ["status.json"])
            status.remove(path)
            self.assertFalse(path.exists())
            status.remove(path)                                       # already gone: fine
