"""BisyncRun's reader thread: the run's log and its tidying, with a small
script standing in for rclone."""

import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _setup  # noqa: F401
from spola import runner
from spola.runner import BisyncRun, newest_log

SCRIPT = r'''
import json, sys
for i in range(200):
    sys.stderr.write(json.dumps({"level": "info", "msg": "stats", "stats": {"bytes": i, "pad": "x" * 900}}) + "\n")
sys.stderr.write(json.dumps({"level": "info", "msg": "a.txt: Copied (new)"}) + "\n")
sys.stderr.write(json.dumps({"level": "error", "msg": "the end"}) + "\n")
'''


class ReaderTest(unittest.TestCase):
    def run_fake(self, log_dir: Path) -> BisyncRun:
        run = BisyncRun([sys.executable, "-c", SCRIPT], dict(os.environ), log_dir.parent / "lock", log_dir,
                        "/a/", "/b/")
        run.start()
        self.assertIsNotNone(run.wait(30))      # the outcome, and the reader thread over: the logs are tidied
        return run

    def test_the_log_leaves_out_the_statistics_past_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            with mock.patch.object(runner, "MAX_LOG_BYTES", 20_000):
                run = self.run_fake(log_dir)
            log = newest_log(log_dir)
            text = log.read_text()
            self.assertLess(log.stat().st_size, 25_000)
            self.assertIn("the statistics lines are left out", text)
            self.assertIn("the end", text)                          # after the cap, not a statistic: kept
            self.assertIn("Copied (new)", text)
            self.assertLess(sum(1 for line in text.splitlines() if '"stats"' in line), 30)
            self.assertEqual(sum(1 for e in run.events if e.kind.name == "STATS"), 1)   # all 200 read, kept as one

    def test_older_logs_are_compressed_and_the_oldest_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            self.assertIsNone(newest_log(log_dir))
            for i in range(25):
                (log_dir / f"20260101-0000{i:02}.000000-1.jsonl").write_text(json.dumps({"msg": f"old {i}"}) + "\n")
            (log_dir / "20260101-000003.000000-1.jsonl.gz.tmp").write_text("a compression cut short")
            self.run_fake(log_dir)
            names = sorted(os.listdir(log_dir))
            self.assertEqual(len(names), runner.KEEP_LOGS)
            plain = [n for n in names if n.endswith(".jsonl")]
            self.assertEqual(len(plain), 1)                                 # this run's log, readable as it is
            self.assertEqual(newest_log(log_dir), log_dir / plain[0])
            self.assertTrue(all(n.endswith(".jsonl.gz") for n in names if n != plain[0]), names)
            with gzip.open(log_dir / names[0], "rt") as f:
                self.assertIn("old 6", f.read())                            # 0 to 5 pruned, the rest compressed
            self.assertFalse(list(log_dir.glob("*.tmp")))
            # a second run compresses the one before and prunes one more
            self.run_fake(log_dir)
            names = sorted(os.listdir(log_dir))
            self.assertEqual(len(names), runner.KEEP_LOGS)
            self.assertEqual(len([n for n in names if n.endswith(".jsonl")]), 1)
            self.assertNotIn("20260101-000006.000000-1.jsonl.gz", names)


if __name__ == "__main__":
    unittest.main()
