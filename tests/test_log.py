#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""What fails is said and, where the app asks, written down and told: stderr
alone (an app with errorlog.py), or the log file in the state folder and
the hook for the exceptions nobody caught."""

import contextlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import _setup

log = _setup.shared("app.log")


class StderrOnly(unittest.TestCase):
    """setup(name) alone, as an app with errorlog.py calls it."""

    def setUp(self):
        self.addCleanup(log.setup, "app")

    def test_a_complaint_says_the_name_where_and_what_then_its_traceback(self):
        log.setup("my-app")
        said = io.StringIO()
        try:
            raise ValueError("the thing")
        except ValueError as error:
            with contextlib.redirect_stderr(said):
                log.complain("somewhere", error)
        lines = said.getvalue().splitlines()
        self.assertEqual(lines[0], "my-app: somewhere: ValueError: the thing")
        self.assertEqual(lines[1], "Traceback (most recent call last):")
        self.assertEqual(lines[-1], "ValueError: the thing")

    def test_without_a_file_nothing_is_written_on_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            here = os.getcwd()
            os.chdir(tmp)                       # a relative file would land here
            try:
                log.setup("my-app")
                with contextlib.redirect_stderr(io.StringIO()):
                    log.complain("quiet", ValueError("x"))
                    log.note("something done")
            finally:
                os.chdir(here)                  # (Windows deletes no folder that is the current one)
            self.assertIsNone(log.file())
            self.assertEqual(os.listdir(tmp), [])


class File(unittest.TestCase):
    def setUp(self):
        self.hook = sys.excepthook
        self.thread_hook = threading.excepthook
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "state" / "my-app.log"
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))    # stderr is not this test's output

    def tearDown(self):
        sys.excepthook, threading.excepthook = self.hook, self.thread_hook
        log.setup("app")
        log.tell_with(None)

    def test_a_complaint_goes_to_the_file_with_its_traceback(self):
        log.setup("my-app", self.file)
        try:
            raise ValueError("the thing")
        except ValueError as error:
            log.complain("somewhere", error)
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("my-app: somewhere: ValueError: the thing", text)
        self.assertIn("Traceback", text)
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T")

    def test_a_note_goes_to_the_file_too(self):
        log.setup("my-app", self.file)
        log.note("the stores were trimmed")
        self.assertIn("my-app: the stores were trimmed", self.file.read_text(encoding="utf-8"))
        self.assertEqual(log.file(), self.file)

    def test_a_big_log_is_rotated_once(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("x" * (log.LOG_KEEP + 1), encoding="utf-8")
        log.setup("my-app", self.file)
        self.assertTrue(self.file.with_name("my-app.log.1").exists())
        self.assertFalse(self.file.exists())

    def test_an_uncaught_exception_is_logged_and_told_in_one_line(self):
        told = []
        log.setup("my-app", self.file)
        log.install_excepthook(told.append)
        try:
            raise RuntimeError("boom")
        except RuntimeError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        self.assertEqual(told, ["RuntimeError: boom"])
        self.assertIn("unhandled: RuntimeError: boom", self.file.read_text(encoding="utf-8"))

    def test_a_teller_that_fails_does_not_hide_the_error(self):
        def bad(_text):
            raise OSError("no window")
        log.setup("my-app", self.file)
        log.install_excepthook(bad)
        sys.excepthook(ValueError, ValueError("first"), None)
        text = self.file.read_text(encoding="utf-8")
        self.assertIn("unhandled: ValueError: first", text)
        self.assertIn("telling: OSError: no window", text)

    def test_without_a_file_a_complaint_still_reaches_stderr(self):
        log.setup("my-app", None)
        log.complain("quiet", ValueError("x"))           # no exception, no file
        self.assertIsNone(log.file())


if __name__ == "__main__":
    unittest.main()
