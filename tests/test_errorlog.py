# SPDX-License-Identifier: GPL-3.0-or-later
"""The windowed app's errors kept in a file (app/errorlog.py): what stderr
receives lands in errors.log with the time, a console still sees the same
text, no stream or the null device counts as nowhere, the file stays under
its limit, and a file that cannot be written or a console that went away
never breaks stderr. That a traceback of a toolkit's callback reaches the
file is an app's test: a common test does not load the toolkit."""

import io
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import _setup

errorlog = _setup.shared("app.errorlog")

STAMP = re.compile(r"^--- \d{4}-\d\d-\d\d \d\d:\d\d:\d\d ---$", re.M)


class GoneConsole(io.StringIO):
    """A console that went away: every write fails."""

    def write(self, text):
        raise OSError("the console is gone")


class ErrorLogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = errorlog.log_file(Path(tmp.name) / "state")
        before = sys.stderr

        def put_back():
            if isinstance(sys.stderr, errorlog.ErrorLog):
                sys.stderr.close()
            sys.stderr = before
        self.addCleanup(put_back)

    def stream(self, **options):
        stream = errorlog.ErrorLog(self.path, **options)
        self.addCleanup(stream.close)
        return stream

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def test_the_file_is_named_in_the_state_folder(self):
        self.assertEqual(errorlog.log_file(Path("state")), Path("state") / "errors.log")

    def test_a_run_without_errors_leaves_no_file(self):
        errorlog.install(self.path)
        self.assertFalse(self.path.exists())

    def test_what_stderr_receives_lands_in_the_file_with_the_time(self):
        null = open(os.devnull, "w")            # as a windowed build pointed at the null device
        self.addCleanup(null.close)
        sys.stderr = null
        stream = errorlog.install(self.path)
        self.assertIsNone(stream.echo)
        print("my-app: something: ValueError: wrong", file=sys.stderr)
        text = self.read()
        self.assertRegex(text, STAMP)
        self.assertIn("my-app: something: ValueError: wrong", text)

    def test_a_console_sees_the_same_text_and_no_more(self):
        console = io.StringIO()
        sys.stderr = console
        errorlog.install(self.path)
        print("first line", file=sys.stderr)
        self.assertEqual("first line\n", console.getvalue())
        self.assertIn("first line", self.read())

    def test_a_console_that_went_away_leaves_the_file(self):
        stream = self.stream(echo=GoneConsole())
        stream.write("after the console\n")
        self.assertIsNone(stream.echo)
        self.assertIn("after the console", self.read())

    def test_a_file_that_cannot_be_written_leaves_the_console(self):
        folder = self.path.parent
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.write_text("a file where the folder should be", encoding="utf-8")
        console = io.StringIO()
        stream = self.stream(echo=console)
        self.assertEqual(stream.write("first\n"), len("first\n"))
        stream.write("second\n")
        self.assertEqual("first\nsecond\n", console.getvalue())
        self.assertTrue(stream._broken)

    def test_a_burst_gets_the_time_once_and_a_later_one_again(self):
        now = [1000.0]
        stream = self.stream(clock=lambda: now[0])
        stream.write("Traceback (most recent call last):\n")
        stream.write("  line\n")
        now[0] += errorlog.QUIET + 1
        stream.write("again\n")
        self.assertEqual(2, len(STAMP.findall(self.read())))

    def test_the_file_stays_under_its_limit_and_keeps_the_one_before(self):
        stream = self.stream(max_bytes=1000)
        for n in range(200):
            stream.write(f"line {n:03d} of an error that repeats\n")
        older = self.path.with_name("errors.log.1")
        self.assertTrue(older.is_file())
        self.assertLess(self.path.stat().st_size, 1100)
        self.assertLess(older.stat().st_size, 1100)
        self.assertIn("line 199", self.read())

    def test_a_console_s_colours_stay_out_of_the_file(self):
        stream = self.stream()
        stream.write("\x1b[1;35mValueError\x1b[0m: wrong\n")
        self.assertIn("ValueError: wrong", self.read())
        self.assertNotIn("\x1b", self.read())

    def test_without_a_console_there_is_no_file_number(self):
        stream = self.stream()
        self.assertFalse(stream.isatty())
        with self.assertRaises(io.UnsupportedOperation):
            stream.fileno()

    def test_an_exception_in_a_thread_lands_in_the_file(self):
        sys.stderr = None                       # as the windowed exe starts
        errorlog.install(self.path)

        def fails():
            raise RuntimeError("a thread that broke")
        thread = threading.Thread(target=fails)
        thread.start()
        thread.join()
        text = self.read()
        self.assertRegex(text, STAMP)
        self.assertIn("RuntimeError: a thread that broke", text)


if __name__ == "__main__":
    unittest.main()
