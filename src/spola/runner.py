"""Run one rclone bisync for one pair, as a child process.

No GTK here: callbacks arrive on the reader thread, and the GTK side moves
them to the main loop with GLib.idle_add. The CLI uses the same class and
simply waits.

A pair is protected by our own flock (bisync's lock file has a known race,
rclone issue #9793), held for the whole life of the child. Cancelling sends
SIGINT, which bisync turns into a graceful stop (finish or cancel transfers,
save state); a second cancel sends a second SIGINT, which stops at once and
leaves the next run to --recover. On Windows the lock is msvcrt's and the
signal a Ctrl+Break (see win32.py).
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import WINDOWS
from .rclone import Event, Outcome, Result, parse_line, summarize

if WINDOWS:
    from . import win32
else:
    import fcntl

KEEP_LOGS = 20


class PairBusy(Exception):
    """Another Spola process (GUI or CLI) is already running this pair."""


class BisyncRun:
    def __init__(self, argv: list[str], env: dict, lock_path: Path, log_dir: Path,
                 path1: str, path2: str,
                 on_event: Callable[[Event], None] | None = None,
                 on_exit: Callable[[Outcome], None] | None = None):
        self.argv, self.env = argv, env
        self.lock_path, self.log_dir = lock_path, log_dir
        self.path1, self.path2 = path1, path2
        self.on_event, self.on_exit = on_event, on_exit
        self.events: list[Event] = []
        self.outcome: Outcome | None = None
        self._proc: subprocess.Popen | None = None
        self._lock_fd: int | None = None
        self._interrupts = 0
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._take_lock()
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # unique even for two runs in the same second
            now = time.time()
            self._log_path = self.log_dir / (time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                                             + f".{int(now % 1 * 1e6):06d}-{os.getpid()}.jsonl")
            self._proc = subprocess.Popen(
                self.argv, env=self.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                # our own process group: a Ctrl+C in the terminal that started
                # Spola must not reach rclone behind our back
                **(win32.popen_options() if WINDOWS else {"start_new_session": True}))
            if WINDOWS:
                # how interrupt() reaches it: its console is ours, or its own
                self._proc.spola_shared_console = win32.has_console()
        except BaseException:
            self._release_lock()
            raise
        self._thread = threading.Thread(target=self._read, name="bisync-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """The graceful stop, once: asked again while bisync finishes and
        saves its state, it keeps waiting (cancel() again is "stop now",
        which leaves its lock and listings for a repair)."""
        if self._interrupts == 0:
            self.cancel()

    def cancel(self) -> None:
        """First call: graceful stop. Second call: stop now."""
        if self._proc and self._proc.poll() is None:
            self._interrupts += 1
            if WINDOWS:
                # the second call, or an event that cannot be sent: at once
                if self._interrupts > 1 or not win32.interrupt(self._proc):
                    self._proc.kill()
                return
            try:
                self._proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    def wait(self, timeout: float | None = None) -> Outcome | None:
        self._done.wait(timeout)
        return self.outcome

    @property
    def running(self) -> bool:
        return not self._done.is_set()

    @property
    def stopping(self) -> bool:
        """Asked to stop and not over yet: bisync finishes or cancels its
        transfers (up to 30 s) and saves its state."""
        return self._interrupts > 0 and self.running

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    # -- internals -----------------------------------------------------------

    def _take_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | (os.O_NOINHERIT if WINDOWS else os.O_CLOEXEC), 0o600)
        if WINDOWS:
            taken = win32.lock(fd)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                taken = True
            except BlockingIOError:
                taken = False
        if not taken:
            os.close(fd)
            raise PairBusy(str(self.lock_path))
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            if WINDOWS:
                win32.unlock(self._lock_fd)
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _read(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            with open(self._log_path, "w", encoding="utf-8") as log:
                for line in self._proc.stderr:
                    log.write(line)
                    ev = parse_line(line)
                    if ev is None:
                        continue
                    self.events.append(ev)
                    if self.on_event:
                        try:
                            self.on_event(ev)
                        except Exception:  # a UI bug must not stop the run
                            pass
            self._proc.stderr.close()
            code = self._proc.wait()
            self.outcome = summarize(code, self.events, self.path1, self.path2,
                                     cancelled=self._interrupts > 0 and code != 0)
            if self._interrupts > 0 and code != 0 and self.outcome.result is Result.CANCELLED:
                self.outcome.message = "stopped"
        finally:
            self._release_lock()
            self._prune_logs()
            self._done.set()
        if self.on_exit:
            self.on_exit(self.outcome)

    def _prune_logs(self) -> None:
        try:
            logs = sorted(self.log_dir.glob("*.jsonl"))
            for old in logs[:-KEEP_LOGS]:
                old.unlink()
        except OSError:
            pass
