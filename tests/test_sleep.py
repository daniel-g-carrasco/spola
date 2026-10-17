#!/usr/bin/env python3
"""logind's sleep lock, as far as a test machine allows: asked and released
without an error whether or not logind grants it (CI has no session)."""

import sys
import unittest

import _setup  # noqa: F401

if sys.platform == "win32":
    raise unittest.SkipTest("logind is Linux only")

from spola.sleep import SleepWatch  # noqa: E402


class SleepTest(unittest.TestCase):
    def test_lock_asked_and_released(self):
        seen = []
        watch = SleepWatch(lambda: seen.append("sleep"), lambda: seen.append("wake"))
        watch.start()
        cookie = watch.inhibit("Spola test")
        self.assertEqual(watch.holding, bool(cookie))
        watch.release(cookie)
        watch.release(cookie)                                   # twice: nothing to release, no error
        self.assertFalse(watch.holding)
        watch.stop()
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
