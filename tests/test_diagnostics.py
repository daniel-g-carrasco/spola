"""The troubleshooting text of the About window, with a stand-in engine."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _setup  # noqa: F401
from spola import diagnostics
from spola.config import Config, Pair
from spola.paths import Paths


class DiagnosticsTest(unittest.TestCase):
    def test_summary_and_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp) / "config", Path(tmp) / "state", Path(tmp) / "data")
            (paths.state / "logs" / "p1").mkdir(parents=True)
            (paths.state / "logs" / "p1" / "a.jsonl").write_text("x" * 2500)
            cfg = Config(user="daniel@example.com", mount_id="m", pairs=[Pair("p1", "/h/docs", "/docs")],
                         instance_id="abcd")
            rt = SimpleNamespace(status=SimpleNamespace(name="ERROR"), message="the disk is nearly full",
                                 safety="", gone="", retries=2, last_failure="", conflicts=["a"], refused=[],
                                 last_outcome=SimpleNamespace(result=SimpleNamespace(name="FAILED"), exit_code=2,
                                                              message="boom"), last_sync=0)
            engine = SimpleNamespace(cfg=cfg, paths=paths, rclone="/no/rclone", creds=None, space=None,
                                     runtime={"p1": rt}, blocked_reason=lambda: "")
            text = diagnostics.summary(engine)
            self.assertIn("Spola ", text)
            self.assertIn("d***@example.com", text)
            self.assertNotIn("daniel@", text)
            self.assertIn("[p1] /h/docs <-> /docs", text)
            self.assertIn("status: ERROR 'the disk is nearly full', retries 2", text)
            self.assertIn("last run: FAILED, exit 2, 'boom'", text)
            self.assertIn("conflicts: 1", text)
            self.assertNotIn("listings", text)                     # read in the slow part, in a thread
            slow = diagnostics.storage(paths, "/no/rclone", ["p1"])
            self.assertIn("[p1] listings: none", slow)
            self.assertIn("logs: 1 files, 2.5 kB", slow)
            self.assertIn("backups: none", slow)
            self.assertIn("rclone version: ", slow)                 # not found: said, not raised

    def test_helpers(self):
        self.assertEqual(diagnostics.masked("ab@c.d"), "a***@c.d")
        self.assertEqual(diagnostics.masked(""), "")
        self.assertEqual(diagnostics.masked("nomail"), "***")
        self.assertEqual(diagnostics.human(0), "0 B")
        self.assertEqual(diagnostics.human(1500), "1.5 kB")
        self.assertEqual(diagnostics.human(73 * 10**9), "73.0 GB")
        self.assertEqual(diagnostics.human(None), "?")
        self.assertEqual(diagnostics.rclone_version(None), "not found")
