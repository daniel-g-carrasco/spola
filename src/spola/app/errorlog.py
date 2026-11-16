# SPDX-License-Identifier: GPL-3.0-or-later
"""The errors of the windowed app, kept in a file.

A program started from the desktop has nobody reading its stderr: the
Windows build has no console at all (Python starts it with sys.stderr set
to None, or an entry point points the streams at the null device), the
desktop on Linux hands it to the journal, which nobody opens. A traceback
of a button's callback, which PyGObject prints and then carries on, or a
`log.complain`, would leave no trace. Here stderr becomes a tee: what it
wrote before still goes where it went (the console, the journal), and it
also goes to `errors.log` in the state folder, with the date and time
before each burst of lines.

The file does not grow without end: past MAX_BYTES it becomes
`errors.log.1` (the one before is dropped) and a new one starts. Nothing
goes on the network; the file holds what a traceback holds and no more.

The module knows no app and no toolkit: install() is told the file."""

from __future__ import annotations

import io
import os
import re
import sys
import threading
import time
from pathlib import Path

FILE_NAME = "errors.log"
MAX_BYTES = 1_000_000       # then the file becomes errors.log.1 and a new one starts
COLOURS = re.compile(r"\x1b\[[0-9;]*m")
QUIET = 2.0                 # seconds without a line after which a new burst gets the time again


def log_file(state: Path) -> Path:
    return state / FILE_NAME


class ErrorLog(io.TextIOBase):
    """A text stream that appends to `path`, opened at the first write
    (a run without errors leaves no file), with the time before each
    burst, and `echo` (the stream there was before, or None) written first,
    unchanged."""

    def __init__(self, path: Path, echo=None, max_bytes: int = MAX_BYTES, clock=time.time) -> None:
        super().__init__()
        self.path = Path(path)
        self.echo = echo
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._file = None
        self._size = 0
        self._last = None           # when the last line was written
        self._at_line_start = True
        self._broken = False        # the file could not be written: the echo alone, from then on

    @property
    def encoding(self):
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return bool(self.echo is not None and getattr(self.echo, "isatty", lambda: False)())

    def fileno(self) -> int:
        # faulthandler and subprocess ask for one: the echo's, if it has one
        if self.echo is not None:
            return self.echo.fileno()
        raise io.UnsupportedOperation("fileno")

    def write(self, text: str) -> int:
        if self.echo is not None:
            try:
                self.echo.write(text)
                self.echo.flush()
            except (OSError, ValueError):
                # a console that went away: the file still hears it
                self.echo = None
        if text and not self._broken:
            with self._lock:
                try:
                    self._to_file(text)
                except OSError:
                    # stderr is this stream: there is nowhere else to say it;
                    # the echo, when there is one, already has the text
                    self._broken = True
                    self._close()
        return len(text)

    def flush(self) -> None:
        if self.echo is not None:
            try:
                self.echo.flush()
            except (OSError, ValueError):
                self.echo = None

    def close(self) -> None:
        """The file, not the echo (it was not ours to open)."""
        with self._lock:
            self._close()
        super().close()

    def _to_file(self, text: str) -> None:
        now = self._clock()
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._size = self.path.stat().st_size if self.path.exists() else 0
            self._file = open(self.path, "a", encoding="utf-8", errors="replace")
        if self._size >= self.max_bytes:
            self._roll()
        if self._last is None or now - self._last > QUIET:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            text = ("" if self._at_line_start else "\n") + f"--- {stamp} ---\n" + text
        text = COLOURS.sub("", text)        # a console's colours, not for a file
        self._file.write(text)
        self._file.flush()
        self._size += len(text.encode("utf-8", "replace"))     # (the \r Windows adds: not counted)
        self._last = now
        self._at_line_start = text.endswith("\n")

    def _roll(self) -> None:
        self._close()
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        self._file = open(self.path, "a", encoding="utf-8", errors="replace")
        self._size = 0

    def _close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass        # closing a file that could not be written: nothing more to lose
            self._file = None


def _nowhere(stream) -> bool:
    """No stream, or the null device a windowed build may point them at."""
    return stream is None or getattr(stream, "name", None) == os.devnull


def install(path: Path) -> ErrorLog:
    """stderr from now on: the stream it was (unless it was nowhere) and
    the file. Unhandled exceptions (sys.excepthook, threading.excepthook,
    PyGObject's callbacks) all print to sys.stderr, so they land here."""
    before = sys.stderr
    stream = ErrorLog(path, echo=None if _nowhere(before) else before)
    sys.stderr = stream
    return stream
