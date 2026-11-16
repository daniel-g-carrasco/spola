# SPDX-License-Identifier: GPL-3.0-or-later
"""Unexpected failures said out loud, and kept. What goes wrong in a thread,
in a callback or in the main loop must at least reach stderr (the journal,
when it runs as a service; the console, from the sources), with its
traceback, instead of vanishing with the thread or the idle source that
raised it. Never `except Exception: pass`: a failure nobody hears of is a
bug nobody can fix.

Where it is kept, two ways, and an app takes one of them:

- `setup(name)` alone: stderr only, nothing on the disk. For an app with
  errorlog.py, which makes stderr a tee into its errors.log: what this
  module says reaches that file through stderr (the GTK apps).
- `setup(name, path)`: this module keeps its own file too, with the date
  and time before each entry, rotated once to <name>.1 past LOG_KEEP. For
  an app without errorlog.py; with install_excepthook() for the exceptions
  nobody caught and keep_fatal() for a crash of the process itself (the
  Qt apps, whose window may run without a console).

Never both on the same app: with errorlog.py's tee and a file here, every
failure would be written twice, once in each file.

The module knows no app: setup() is told the name the lines start with
(the app's slug) and, if it keeps one, the file."""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

LOG_KEEP = 1 << 20              # bytes: beyond this the file is rotated once, to <name>.1

_name = "app"
_file: Path | None = None
_tell: Callable[[str], None] | None = None


def _rotated(path: Path) -> None:
    """The folder made, and a file past LOG_KEEP moved aside to <name>.1."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_KEEP:
        path.replace(path.with_name(path.name + ".1"))


def setup(name: str, path: Path | None = None) -> None:
    """At start: the name every line starts with (the app's slug) and the
    file the failures are appended to (None: stderr only, nothing written
    on the disk), rotated when it grows past LOG_KEEP."""
    global _name, _file
    _name = name
    _file = Path(path) if path else None
    if _file is None:
        return
    try:
        _rotated(_file)
    except OSError as error:
        print(f"{_name}: log: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        _file = None


def file() -> Path | None:
    """The file kept by this module, or None (stderr only)."""
    return _file


def _write(text: str) -> None:
    print(text, file=sys.stderr, flush=True)
    if _file is None:
        return
    try:
        with _file.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {text}\n")
    except OSError:
        pass                    # the log itself has nowhere else to complain


def note(text: str) -> None:
    """Something the program did on its own that is worth finding in the log (no error)."""
    _write(f"{_name}: {text}")


def complain(where: str, error: BaseException) -> None:
    """A failure, with its traceback: "<name>: <where>: <kind>: <error>"
    and then the traceback, on stderr and in the file if there is one."""
    detail = "".join(traceback.format_exception(error)).rstrip()
    _write(f"{_name}: {where}: {type(error).__name__}: {error}\n{detail}")


def install_excepthook(tell: Callable[[str], None] | None = None) -> None:
    """Every exception nobody caught (in a slot, a timer, a thread) is
    logged and, with a teller, said to the user in one line. Qt's Python
    binding calls sys.excepthook for the exceptions of its slots (checked
    on 6.12, 23/10/2026). An app with errorlog.py does not need it: Python prints
    them to stderr, which errorlog keeps."""
    global _tell
    _tell = tell

    def hook(kind, error, tb):
        complain("unhandled", error.with_traceback(tb) if tb is not None else error)
        if _tell is not None:
            try:
                _tell(f"{kind.__name__}: {error}")
            except Exception as again:          # noqa: BLE001  the teller failing must not hide the error
                _write(f"{_name}: telling: {type(again).__name__}: {again}")

    def thread_hook(args):
        hook(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def tell_with(tell: Callable[[str], None] | None) -> None:
    """The window gives itself as the teller once it exists."""
    global _tell
    _tell = tell


_fatal = None


def keep_fatal(path: Path | None) -> None:
    """A crash of the process itself (an access violation in the toolkit or
    a library: no Python exception, no excepthook) written with the stacks
    of every thread to `path`, after a line saying when this run started. A
    window started without a console (pythonw) would otherwise leave
    nothing behind. Rotated as the log is."""
    global _fatal
    if path is None:
        return
    import faulthandler
    import os
    try:
        path = Path(path)
        _rotated(path)
        _fatal = path.open("a", encoding="utf-8")
        _fatal.write(f"{datetime.now().isoformat(timespec='seconds')} start, pid {os.getpid()}\n")
        _fatal.flush()
        faulthandler.enable(file=_fatal, all_threads=True)
    except OSError as error:
        _write(f"{_name}: fatal log: {type(error).__name__}: {error}")
