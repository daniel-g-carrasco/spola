"""The scheduler: decides when each pair runs, one run at a time, and keeps
the status that the window, the tray and (later) Nautilus show.

Rules, from docs/progetto.md (5.3, 5.4, appendix P):
  * runs are queued and executed one at a time for the whole account, and
    next to them one preview (a dry run, which only reads), never two runs
    on the same pair;
  * a preview is kept: its dialog can be closed and opened again at once,
    and a pair that waits for the user gets its preview prepared ahead;
  * a pair that was never aligned never runs on its own: the first alignment
    (and any repair) starts only after the user confirmed a dry-run preview;
  * local changes start a run 10 s after the last change, or at most 2 min
    after the first one; Koofr is checked every `poll_minutes`;
  * no new runs on a metered network or in power-saver mode (if enabled),
    nor while offline;
  * "too many deletes", "all files changed" and critical errors stop the
    pair until the user decides; transient errors retry with growing delays.
"""

from __future__ import annotations

import enum
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import Gio, GLib, GObject

from . import config as config_mod
from . import conflicts as conflicts_mod
from . import jobs, koofr_api, listings
from .recent import Recent
from . import activity as activity_mod
from . import account as account_mod
from .account import Credentials
from .config import Config, Pair
from .i18n import _, every_translation, ngettext
from .paths import Paths
from .rclone import (Event, Kind, Mode, Outcome, Result, bisync_args, default_excluded, describe, find_rclone,
                     fold_case_renames, koofr_env, relative_path)
from .states import PathStates, ancestors_and_self
from .runner import BisyncRun, PairBusy
from .watcher import TreeWatcher

DEBOUNCE_S = 10
MAX_WAIT_S = 120
RETRY_DELAYS_MIN = (1, 5, 15, 30, 60)
RESUME_GAP_S = 90       # wall clock ahead of the monotonic clock: the machine slept
ACTIVITY_INTERVAL_S = 60
PREVIEW_TTL_S = 30 * 60         # a ready preview opens at once for this long (with its age shown)
PREVIEW_AHEAD_DELAY_S = 60      # quiet time before preparing the preview of a pair that waits for the user
EVENT_SLICE_S = 0.008           # the main loop handles rclone's lines in slices this long, then draws
LOW_SPACE_FRACTION = 0.10       # below this share free on Koofr, the window says so
SPACE_REFRESH_S = 3600          # the space on Koofr is read again at most this often on its own


def conflict_name_pattern() -> re.Pattern:
    """Conflict copies, as named in any of Spola's languages (the desktop
    language may have changed since they were made) and as named by the first
    versions (name.conflict1.ext)."""
    words = every_translation("conflict-local") | every_translation("conflict-koofr")
    alternatives = "|".join(re.escape(w) for w in sorted(words))
    return re.compile(rf"\.(?:conflict\d+|(?:{alternatives})-\d{{4}}-\d{{2}}-\d{{2}}-\d+)(?:\.[^/.]*)?$")


def slept(wall_elapsed: float, monotonic_elapsed: float) -> bool:
    """The monotonic clock stops while the machine is suspended, the wall
    clock does not: a gap between the two means we just woke up. Works in the
    sandbox and under any desktop, unlike logind's PrepareForSleep."""
    return wall_elapsed - monotonic_elapsed > RESUME_GAP_S


class Status(enum.IntEnum):
    """Most serious first: the overall status is the minimum over pairs."""
    NEEDS_REPAIR = 0
    NEEDS_CONFIRMATION = 1
    ERROR = 2
    CONFLICTS = 3
    SKIPPED = 4          # synced, but some files are left out (names Koofr refuses)
    SYNCING = 5
    PENDING = 6
    NEEDS_SETUP = 7
    PAUSED = 8
    OK = 9


def quick_ignore(rel: str) -> bool:
    """Changes that never deserve a run: what the default filters leave out
    (temporary files, trash folders, node_modules...)."""
    return default_excluded(rel)


def space_of(rclone: str, paths: Paths, creds: Credentials | None,
             dev_remote: str | None) -> account_mod.Quota | None:
    """The space on Koofr (the disk of the developer's local "Koofr"); None
    if it cannot be known now. One `rclone about`: cheap, read-only."""
    try:
        if dev_remote:
            usage = shutil.disk_usage(dev_remote)
            return account_mod.Quota(usage.total, usage.used, usage.free)
        if creds is None:
            return None
        quota = account_mod.check(rclone, paths, creds)
        return quota if quota.total else None
    except Exception:  # only a summary or a warning: nothing waits for it
        return None


def free_space(rclone: str, paths: Paths, creds: Credentials | None, dev_remote: str | None) -> int | None:
    """Free space on Koofr, for a preview's summary."""
    space = space_of(rclone, paths, creds, dev_remote)
    return space.free if space is not None else None


@dataclass
class Preview:
    """A dry run of a pair, kept: the dialog that asked for it can be closed
    while it runs and opened again at once when it is done. `reason` is
    "preview" when the user asked for it, "ahead" when Spola prepared it for
    a pair waiting for the user."""
    mode: Mode
    force: bool
    reason: str
    task: Task | None = None
    outcome: Outcome | None = None
    error: str = ""
    stats: dict | None = None
    free_bytes: int | None = None       # free space on Koofr, when known
    finished: float = 0.0               # time.time()
    stale: bool = False                 # something changed since it started
    seen: bool = False                  # its answer reached a dialog
    listeners: list = field(default_factory=list)   # callback(outcome, error), called once

    @property
    def running(self) -> bool:
        return self.outcome is None and not self.error

    def fresh(self, now: float | None = None) -> bool:
        return (self.outcome is not None and not self.stale
                and (time.time() if now is None else now) - self.finished < PREVIEW_TTL_S)


@dataclass
class Task:
    pair_id: str
    mode: Mode = Mode.SYNC
    dry_run: bool = False
    force: bool = False
    reason: str = ""
    callback: Callable[[Outcome | None, str], None] | None = None   # (outcome, error)
    cancelled: bool = False         # Stop while the run was still being prepared
    case_renames: list = field(default_factory=list)    # preflight.CaseRename, done on Koofr first


@dataclass
class PairRuntime:
    status: Status = Status.NEEDS_SETUP
    message: str = ""
    last_sync: float | None = None
    stats: dict | None = None
    transferring: list[dict] = field(default_factory=list)
    run: BisyncRun | None = None
    task: Task | None = None
    last_outcome: Outcome | None = None
    conflicts: list[str] = field(default_factory=list)
    refused: list = field(default_factory=list)     # preflight.Refused of the last run
    retries: int = 0
    retry_source: int = 0
    debounce_source: int = 0
    first_change: float = 0.0
    changed_during_run: set[str] = field(default_factory=set)
    pending_refused: list = field(default_factory=list)
    before_run: tuple = (None, "")          # (status, message) before a run that may not start
    preview: Preview | None = None
    ahead_source: int = 0                   # timer before preparing the preview ahead
    states: PathStates = field(default_factory=lambda: PathStates(excluded=default_excluded))
    run_roots: tuple = ("", "")             # (path1, path2) of the run in progress
    activity: activity_mod.RunActivity = field(default_factory=activity_mod.RunActivity)
    watcher: TreeWatcher | None = None


class Engine(GObject.Object):
    __gsignals__ = {
        "pair-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "pairs-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # account, global pause or network conditions changed: the overall
        # status may change even when there is no pair to report it
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # (pair id, reason of the run): a real run (not a preview) ended;
        # its outcome is in the pair's runtime
        "run-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # (pair id, list of relative paths) whose file manager state changed
        "paths-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        # (pair id): a run ended, every state below the root may have changed
        "root-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # (pair id, kind, message): something the user should know about
        "attention": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
    }

    def __init__(self, paths: Paths, cfg: Config):
        super().__init__()
        self.paths = paths
        self.cfg = cfg
        self.creds: Credentials | None = None
        self.rclone = find_rclone()
        # Developer aid: the "Koofr" side becomes a local folder, so every flow
        # of the app can be tried without an account. Never set in the Flatpak.
        self.dev_remote = os.environ.get("SPOLA_DEV_REMOTE") or None
        self._conflict_name = conflict_name_pattern()
        self.trash = conflicts_mod.move_to_trash      # where a version not kept goes (tests replace it)
        # Koofr's activity feed as a doorbell: None = not tried yet
        self.feed_ok: bool | None = None
        self.feed_enabled = self.dev_remote is None
        self._activity_since = int(time.time() * 1000)
        self._activity_seen: set[str] = set()
        self._activity_busy = False
        self.runtime: dict[str, PairRuntime] = {p.id: PairRuntime() for p in cfg.pairs}
        self.recent = Recent(paths.state / "recent.json")
        self.queue: list[Task] = []
        self.active: Task | None = None         # the real run, being prepared or running
        self.previewing: Task | None = None     # the preview next to it
        # rclone's lines, from the reader threads to the main loop: handed
        # over in slices, so that a run with 150,000 files never freezes the window
        self._events: deque = deque()
        self._events_lock = threading.Lock()
        self._events_scheduled = False
        # the space on Koofr, read now and then (see refresh_space)
        self.space: account_mod.Quota | None = None
        self._space_read = 0.0
        self._space_busy = False
        self._poll_source = 0
        self._shutdown_cb: Callable[[], None] | None = None
        self._closing = False
        self.inhibit: Callable[[str], int] = lambda reason: 0
        self.uninhibit: Callable[[int], None] = lambda cookie: None
        self._inhibit_cookie = 0
        self.network = Gio.NetworkMonitor.get_default()
        self.power = Gio.PowerProfileMonitor.dup_default()
        self.network.connect("notify::network-available", self._on_conditions)
        self.network.connect("notify::network-metered", self._on_conditions)
        if self.power is not None:
            self.power.connect("notify::power-saver-enabled", self._on_conditions)

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        for pair in self.cfg.pairs:
            self._setup_pair(pair)
        self._reschedule_poll()
        GLib.timeout_add_seconds(3, self._initial_runs)
        self._clock = (time.time(), time.monotonic())
        GLib.timeout_add_seconds(60, self._check_clock)
        GLib.timeout_add_seconds(ACTIVITY_INTERVAL_S, self._poll_activity)
        GLib.timeout_add_seconds(60, self._prune_backups)
        GLib.timeout_add_seconds(24 * 3600, self._prune_backups)

    def _check_clock(self) -> bool:
        wall, mono = time.time(), time.monotonic()
        if slept(wall - self._clock[0], mono - self._clock[1]):
            self.sync_all("resume")
            self._on_conditions()
        self._clock = (wall, mono)
        return True

    def _initial_runs(self) -> bool:
        for pair in self.cfg.pairs:
            rt = self.runtime.get(pair.id)
            # signing in has already started (or queued) a run for it
            if rt and (rt.run or rt.last_sync or any(t.pair_id == pair.id for t in self.queue)):
                continue
            self.request_sync(pair.id, "startup")
        return False

    def _prune_backups(self) -> bool:
        for pair in self.cfg.pairs:
            try:
                jobs.prune_backups(self.paths, pair.id, self.cfg.backup_days)
            except OSError:
                pass
        return True

    def shutdown(self, done: Callable[[], None]) -> None:
        """Stop gracefully: no new runs, SIGINT to the running one, then done()."""
        self._closing = True            # a run still being prepared will not start
        self.queue.clear()
        for rt in self.runtime.values():
            if rt.watcher:
                rt.watcher.stop()
        runs = self._current_runs()
        if not runs:
            done()
            return
        self._shutdown_cb = done
        for run in runs:
            run.cancel()
        # bisync needs up to 30 s for transfers plus 60 s to save its state;
        # after that a second SIGINT stops it at once (--recover handles it).
        GLib.timeout_add_seconds(95, self._force_stop)

    def _force_stop(self) -> bool:
        for run in self._current_runs():
            run.cancel()
        return False

    def _current_runs(self) -> list[BisyncRun]:
        """The rclone processes running: the real run, the preview."""
        runs = []
        for task in (self.active, self.previewing):
            rt = self.runtime.get(task.pair_id) if task else None
            if rt and rt.run and rt.run.running:
                runs.append(rt.run)
        return runs

    def save_config(self) -> None:
        config_mod.save(self.cfg, self.paths.config_file)

    # -- pairs -----------------------------------------------------------------

    def _setup_pair(self, pair: Pair) -> None:
        rt = self.runtime.setdefault(pair.id, PairRuntime())
        rt.status = self._idle_status(pair, rt)
        self._update_conflicts(pair, rt)
        self._start_watcher(pair, rt)
        self._want_preview_ahead(pair, rt)

    def add_pair(self, local: str, remote: str) -> Pair:
        from .preflight import root_identity
        dev, ino = root_identity(local)
        pair = Pair(Pair.new_id(), os.path.realpath(local), config_mod.normalize_remote(remote),
                    root_dev=dev, root_ino=ino)
        self.cfg.pairs.append(pair)
        self.save_config()
        self._setup_pair(pair)
        self.emit("pairs-changed")
        return pair

    def remove_pair(self, pair_id: str) -> None:
        """Forget the pair. Files on both sides stay as they are."""
        pair = self.cfg.pair(pair_id)
        if pair is None:
            return
        self.cancel_preview(pair_id)
        rt = self.runtime.pop(pair_id, None)
        self.queue = [t for t in self.queue if t.pair_id != pair_id]
        if rt:
            if rt.watcher:
                rt.watcher.stop()
            if rt.run and rt.run.running:
                rt.run.cancel()
            for source in (rt.retry_source, rt.debounce_source, rt.ahead_source):
                if source:
                    GLib.source_remove(source)
        self.cfg.pairs.remove(pair)
        self.save_config()
        self.recent.forget_pair(pair_id)
        self.emit("pairs-changed")

    def set_paused(self, pair_id: str, paused: bool) -> None:
        pair = self.cfg.pair(pair_id)
        if pair is None or pair.paused == paused:
            return
        pair.paused = paused
        self.save_config()
        rt = self.runtime[pair_id]
        if paused:
            self.queue = [t for t in self.queue if t.pair_id != pair_id or t.dry_run]
            if rt.watcher:
                rt.watcher.stop()
                rt.watcher = None
            if rt.run and rt.run.running and not (rt.task and rt.task.dry_run):
                rt.run.cancel()
            if rt.preview is not None and rt.preview.running and rt.preview.reason == "ahead":
                self.cancel_preview(pair_id)
            if rt.ahead_source:
                GLib.source_remove(rt.ahead_source)
                rt.ahead_source = 0
        else:
            self._start_watcher(pair, rt)
            self.request_sync(pair_id, "resume")
        self._set_status(pair, rt, self._idle_status(pair, rt) if not rt.run else rt.status)

    def set_conflict_rule(self, pair_id: str, rule: str) -> None:
        """The folder's own rule for new conflicts ("" = the general one)."""
        pair = self.cfg.pair(pair_id)
        if pair is not None and pair.conflict_rule != rule:
            pair.conflict_rule = rule
            self.save_config()

    def set_paused_all(self, paused: bool) -> None:
        self.cfg.paused_all = paused
        self.save_config()
        if paused:
            self.queue = [t for t in self.queue if t.dry_run]
            rt = self.runtime.get(self.active.pair_id) if self.active else None
            if rt and rt.run and rt.run.running:
                rt.run.cancel()
            self._cancel_previews_ahead()
        else:
            self.sync_all("resume")
        for pair in self.cfg.pairs:
            rt = self.runtime[pair.id]
            if not rt.run:
                self._set_status(pair, rt, self._idle_status(pair, rt))
        self.emit("status-changed")

    def set_credentials(self, creds: Credentials | None) -> None:
        self.creds = creds
        if creds:
            self.sync_all("login")
            self.refresh_space(min_age=0)
        else:
            self.space = None
        for pair in self.cfg.pairs:
            rt = self.runtime[pair.id]
            if not rt.run:
                self._set_status(pair, rt, self._idle_status(pair, rt))
        self.emit("status-changed")

    # -- space on Koofr ------------------------------------------------------------

    def refresh_space(self, min_age: float = SPACE_REFRESH_S) -> None:
        """Read the space on Koofr again, in a thread, unless it was read less
        than `min_age` seconds ago: at sign-in, after a run that uploaded or
        deleted something, and once an hour."""
        if (self._space_busy or self.creds is None or self.rclone is None
                or (self._space_read and time.monotonic() - self._space_read < min_age)):
            return
        self._space_busy = True
        rclone, paths, creds, dev = self.rclone, self.paths, self.creds, self.dev_remote

        def work():
            space = space_of(rclone, paths, creds, dev)
            GLib.idle_add(lambda: self._space_read_done(space) and False)

        threading.Thread(target=work, name="koofr-space", daemon=True).start()

    def _space_read_done(self, space: account_mod.Quota | None) -> None:
        self._space_busy = False
        self._space_read = time.monotonic()
        if space is not None and space != self.space:
            self.space = space
            self.emit("status-changed")

    @property
    def low_space(self) -> bool:
        """Under LOW_SPACE_FRACTION of Koofr free: the window says so."""
        space = self.space
        return bool(space and space.total and space.free is not None
                    and space.free < space.total * LOW_SPACE_FRACTION)

    # -- status ----------------------------------------------------------------

    def blocked_reason(self) -> str:
        """Why no automatic run may start now, or ''."""
        if self.creds is None:
            return _("Not signed in to Koofr")
        if self.rclone is None:
            return _("rclone is missing")
        if self.cfg.paused_all:
            return _("Paused")
        if not self.network.get_network_available():
            return _("Offline")
        if self.cfg.pause_on_metered and self.network.get_network_metered():
            return _("Paused on a metered connection")
        if self.cfg.pause_on_power_saver and self.power is not None and self.power.get_power_saver_enabled():
            return _("Paused in power saver mode")
        return ""

    def _idle_status(self, pair: Pair, rt: PairRuntime) -> Status:
        if rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION) and rt.run is None:
            return rt.status
        if pair.paused or self.cfg.paused_all:
            # the user's own pause comes first: a pair put on hold before its
            # first alignment waits quietly, and asks for it once resumed
            return Status.PAUSED
        if self.confirmed_waiting(pair.id):
            return Status.PENDING           # what the user confirmed waits its turn
        if not pair.initialized:
            return Status.NEEDS_SETUP
        if self.blocked_reason():
            return Status.PAUSED
        if rt.status is Status.ERROR and rt.retry_source:
            return Status.ERROR
        if rt.debounce_source:
            return Status.PENDING
        if rt.conflicts:
            return Status.CONFLICTS
        if rt.refused:
            return Status.SKIPPED
        return Status.OK

    def _set_status(self, pair: Pair, rt: PairRuntime, status: Status, message: str | None = None) -> None:
        rt.status = status
        if message is not None:
            rt.message = message
        elif status is Status.PAUSED:
            rt.message = _("Paused") if pair.paused else self.blocked_reason()
        elif status is Status.NEEDS_SETUP:
            rt.message = _("First alignment to confirm")
        elif status in (Status.OK, Status.CONFLICTS, Status.SKIPPED, Status.PENDING):
            rt.message = ""
        if (status in (Status.NEEDS_SETUP, Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION)
                and not rt.ahead_source and not (rt.preview and (rt.preview.running or rt.preview.fresh()))):
            self._want_preview_ahead(pair, rt)
        self.emit("pair-changed", pair.id)

    def overall(self) -> Status:
        """The most serious status. A pair the user put on hold does not make
        the tray say "Paused" while the others sync: it counts only when
        every pair is paused."""
        pairs = [p for p in self.cfg.pairs if p.id in self.runtime]
        statuses = [self.runtime[p.id].status for p in pairs if not p.paused] \
            or [self.runtime[p.id].status for p in pairs]
        return min(statuses) if statuses else Status.OK

    # -- requests ----------------------------------------------------------------

    def sync_all(self, reason: str) -> None:
        for pair in self.cfg.pairs:
            self.request_sync(pair.id, reason)

    def request_sync(self, pair_id: str, reason: str) -> None:
        """An automatic or "sync now" run, if the pair may run."""
        pair = self.cfg.pair(pair_id)
        if pair is None or not pair.initialized or pair.paused:
            return
        rt = self.runtime[pair_id]
        if rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
            return      # waiting for the user
        if self.confirmed_waiting(pair_id):
            return      # the run the user confirmed does it, and goes even on a metered network
        if self.blocked_reason() and reason != "manual":
            if rt.run is None:
                self._set_status(pair, rt, Status.PAUSED)
            return
        if rt.debounce_source:
            GLib.source_remove(rt.debounce_source)
            rt.debounce_source = 0
        self._enqueue(Task(pair_id, Mode.SYNC, reason=reason))

    def preview(self, pair_id: str, mode: Mode, force: bool,
                callback: Callable[[Outcome | None, str], None] | None = None,
                reason: str = "preview", refresh: bool = False) -> Preview | None:
        """A dry run for a confirmation dialog, next to the real runs: the one
        running or ready for the same question is reused (unless `refresh`).
        `callback(outcome, error)` is called once, when it is done."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            if callback:
                GLib.idle_add(lambda: callback(None, _("This folder is no longer synced.")) and False)
            return None
        kept = rt.preview
        if kept is not None and (kept.mode, kept.force) == (mode, force) and not refresh:
            if kept.running:
                if callback:
                    kept.listeners.append(callback)
                if reason == "preview":
                    kept.reason = "preview"         # the user is waiting for it now
                return kept
            if kept.fresh():
                if callback:
                    kept.seen = True
                    GLib.idle_add(lambda: callback(kept.outcome, kept.error) and False)
                return kept
        if kept is not None and kept.running:
            self.cancel_preview(pair_id)            # another question: that answer is not wanted now
        preview = rt.preview = Preview(mode, force, reason, listeners=[callback] if callback else [])
        task = preview.task = Task(pair_id, mode, dry_run=True, force=force, reason=reason)
        task.callback = lambda outcome, error: self._preview_finished(pair_id, task, outcome, error)
        if rt.ahead_source:
            GLib.source_remove(rt.ahead_source)
            rt.ahead_source = 0
        self._enqueue(task, front=True)
        self.emit("pair-changed", pair_id)
        return preview

    def cancel_preview(self, pair_id: str) -> None:
        """Stop the pair's preview, queued, being prepared or running."""
        rt = self.runtime.get(pair_id)
        preview = rt.preview if rt else None
        if preview is None or not preview.running:
            return
        task, rt.preview = preview.task, None
        if any(t is task for t in self.queue):
            self.queue = [t for t in self.queue if t is not task]
        elif rt.task is task:
            if rt.run is not None and rt.run.running:
                rt.run.cancel()
            else:
                task.cancelled = True           # being prepared: it will not start
        listeners, preview.listeners = preview.listeners, []
        for callback in listeners:
            callback(None, _("Stopped."))
        self.emit("pair-changed", pair_id)

    def forget_listener(self, pair_id: str, callback) -> None:
        """A dialog closed while its preview runs: the preview goes on, and
        tells the user when it is ready."""
        rt = self.runtime.get(pair_id)
        if rt and rt.preview and callback in rt.preview.listeners:
            rt.preview.listeners.remove(callback)
            self.emit("pair-changed", pair_id)      # the window says it goes on

    def invalidate_preview(self, pair_id: str) -> None:
        """Something changed: a kept preview no longer tells the truth."""
        rt = self.runtime.get(pair_id)
        if rt is None or rt.preview is None:
            return
        if rt.preview.running:
            rt.preview.stale = True
        else:
            rt.preview = None
            self.emit("pair-changed", pair_id)
        pair = self.cfg.pair(pair_id)
        if pair is not None:
            self._want_preview_ahead(pair, rt)

    def review_args(self, pair_id: str) -> tuple[Mode, bool] | None:
        """The preview a pair waiting for the user needs: its first alignment,
        a repair, or the confirmation of a run stopped by a safety check."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return None
        if not pair.initialized or rt.status is Status.NEEDS_REPAIR:
            return Mode.RESYNC, False
        if rt.status is Status.NEEDS_CONFIRMATION:
            return Mode.SYNC, True
        return None

    def _preview_finished(self, pair_id: str, task: Task, outcome: Outcome | None, error: str) -> None:
        rt = self.runtime.get(pair_id)
        preview = rt.preview if rt else None
        if preview is None or preview.task is not task:
            return                  # cancelled, or replaced by another question
        if outcome is not None and outcome.result is Result.CANCELLED:
            outcome, error = None, error or _("Stopped.")
        if outcome is None and not error:
            error = _("The preview failed")
        preview.outcome, preview.error, preview.finished = outcome, error, time.time()
        listeners, preview.listeners = preview.listeners, []
        preview.seen = bool(listeners)
        for callback in listeners:
            callback(outcome, error)
        if outcome is None:
            rt.preview = None
        elif (not listeners and preview.reason == "preview"
              and self.review_args(pair_id) == (preview.mode, preview.force)):
            # the review the user asked for, its dialog closed while it ran: say it is ready
            plan = outcome.plan
            self.emit("attention", pair_id, "preview-ready",
                      describe(plan) if not plan.empty else _("Nothing to do: the two folders already match."))
        self.emit("pair-changed", pair_id)

    def _want_preview_ahead(self, pair: Pair, rt: PairRuntime) -> None:
        """A pair waiting for the user: its preview, prepared once things are
        quiet, opens at once when the user looks."""
        if self.review_args(pair.id) is None:
            return
        if rt.ahead_source:
            GLib.source_remove(rt.ahead_source)
        rt.ahead_source = GLib.timeout_add_seconds(max(1, int(PREVIEW_AHEAD_DELAY_S)), self._preview_ahead, pair.id)

    def _preview_ahead(self, pair_id: str) -> bool:
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return False
        rt.ahead_source = 0
        args = self.review_args(pair_id)
        if (args is None or pair.paused or self.cfg.paused_all or self.creds is None or self.rclone is None
                or self.blocked_reason()):
            return False
        kept = rt.preview
        if kept is not None and (kept.mode, kept.force) == args and (kept.running or kept.fresh()):
            return False
        self.preview(pair_id, *args, reason="ahead")
        return False

    def _cancel_previews_ahead(self) -> None:
        """Conditions changed (a metered network, a pause): what Spola
        prepares on its own stops; what the user asked for goes on."""
        for pair_id, rt in self.runtime.items():
            if rt.preview is not None and rt.preview.running and rt.preview.reason == "ahead":
                self.cancel_preview(pair_id)

    def confirm(self, pair_id: str, mode: Mode, force: bool,
                callback: Callable[[Outcome | None, str], None] | None = None) -> None:
        """The real run the user confirmed after a preview. It goes before
        every automatic run, after the ones confirmed earlier, and never stops
        the sync in progress: until its turn the pair says it is confirmed
        and waiting, not "waiting for your confirmation" any more."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            if callback:
                GLib.idle_add(lambda: callback(None, _("This folder is no longer synced.")) and False)
            return
        if rt.ahead_source:
            GLib.source_remove(rt.ahead_source)
            rt.ahead_source = 0
        self.cancel_preview(pair_id)    # one still running: its answer is not wanted now
        rt.preview = None               # what it said is about to happen
        self._enqueue(Task(pair_id, mode, force=force, reason="confirmed", callback=callback), front=True)
        if rt.task is None or rt.task.dry_run:
            self._set_status(pair, rt, Status.PENDING)

    def confirmed_waiting(self, pair_id: str) -> bool:
        """A run the user confirmed, queued behind the sync in progress."""
        return any(t.pair_id == pair_id and t.reason == "confirmed" and not t.dry_run for t in self.queue)

    def stop(self, pair_id: str) -> None:
        rt = self.runtime.get(pair_id)
        if rt and rt.run and rt.run.running:
            rt.run.cancel()
        elif rt and rt.task is not None and rt.run is None:
            rt.task.cancelled = True        # still preparing: it will not start

    def set_filters(self, pair_id: str, text: str) -> bool:
        """Store the pair's exclusions. If they changed and the pair is
        aligned, it needs a new alignment (bisync refuses to run otherwise):
        the pair is marked for repair, to be confirmed from a preview."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None or not jobs.write_filters(self.paths, pair, text):
            return False
        self.invalidate_preview(pair_id)
        if pair.initialized:
            self.queue = [t for t in self.queue if t.pair_id != pair_id or t.dry_run]
            if rt.run is None or (rt.task and rt.task.dry_run):
                self._set_status(pair, rt, Status.NEEDS_REPAIR, _("The exclusions changed"))
            else:
                rt.status = Status.NEEDS_REPAIR
                rt.message = _("The exclusions changed")
        return True

    def conditions_changed(self) -> None:
        """Preferences about metered networks or power saving changed."""
        self._on_conditions()

    def conflicts(self, pair_id: str) -> list[conflicts_mod.Conflict]:
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return []
        return conflicts_mod.group(rt.conflicts, pair.local)

    def resolve_conflict(self, pair_id: str, conflict: conflicts_mod.Conflict, choice: str) -> None:
        """Local file operations only; the next run carries them to Koofr.
        Refused while a run of the pair is in progress."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return
        self._check_not_syncing(rt)
        conflicts_mod.resolve(pair.local, conflict, choice, trash=self.trash)
        self._forget_conflicts(pair, rt, {c.path for c in conflict.copies})
        self.request_sync(pair_id, "conflict-resolved")

    def resolve_conflicts(self, pair_id: str, decisions: list[conflicts_mod.Decision]) -> tuple[int, list[str]]:
        """Many at once (Resolve All): the decided ones, then one sync for
        them all. Returns how many were resolved, and why the others were not."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return 0, []
        self._check_not_syncing(rt)
        done, errors = self._resolve_decided(pair, rt, decisions)
        if done:
            self.request_sync(pair_id, "conflict-resolved")
        return done, errors

    def conflict_rule(self, pair: Pair) -> str:
        """The folder's own rule for new conflicts, or the general one."""
        rule = pair.conflict_rule or self.cfg.conflict_rule
        return rule if rule in conflicts_mod.RULES else conflicts_mod.ASK

    def rename_refused(self, pair_id: str, renames: list[tuple[str, str]]) -> tuple[int, list[str]]:
        """Give the files Koofr refuses a name it accepts (local renames, as
        planned by preflight.rename_plan): the next run syncs them. Returns how
        many were renamed, and why the others were not."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return 0, []
        self._check_not_syncing(rt)
        done: set[str] = set()
        errors: list[str] = []
        for old, new in renames:
            src, dst = os.path.join(pair.local, old), os.path.join(pair.local, new)
            if not os.path.lexists(src):
                errors.append(_("“{name}” is no longer there.").format(name=old))
            elif os.path.lexists(dst):
                errors.append(_("A file named “{name}” exists now: rename it first.").format(name=new))
            else:
                try:
                    os.rename(src, dst)
                    done.add(old)
                except OSError as e:
                    errors.append(f"{old}: {e.strerror or e}")
        if done:
            self.invalidate_preview(pair_id)
            rt.refused = [r for r in rt.refused if r.path not in done]
            rt.pending_refused = [r for r in rt.pending_refused if r.path not in done]
            rt.states.refused = {r.path for r in rt.refused}
            self.emit("root-changed", pair_id)
            if rt.run is None:
                self._set_status(pair, rt, self._idle_status(pair, rt))
            if pair.initialized:
                self.request_sync(pair_id, "renamed")
        return len(done), errors

    def _check_not_syncing(self, rt: PairRuntime) -> None:
        if rt.run is not None and not (rt.task and rt.task.dry_run):
            raise conflicts_mod.ResolveError(_("Wait for the current sync to finish."))

    def _resolve_decided(self, pair: Pair, rt: PairRuntime,
                         decisions: list[conflicts_mod.Decision]) -> tuple[int, list[str]]:
        done, errors, gone = 0, [], set()
        for decision in decisions:
            if decision.choice is None:
                continue
            try:
                conflicts_mod.resolve(pair.local, decision.conflict, decision.choice, trash=self.trash)
            except (conflicts_mod.ResolveError, OSError) as e:
                errors.append(f"{decision.conflict.original}: {e}")
                continue
            done += 1
            gone |= {c.path for c in decision.conflict.copies}
        if gone:
            self._forget_conflicts(pair, rt, gone)
        return done, errors

    def _forget_conflicts(self, pair: Pair, rt: PairRuntime, gone: set[str]) -> None:
        self.invalidate_preview(pair.id)
        rt.conflicts = [p for p in rt.conflicts if p not in gone]
        rt.states.conflicts = set(rt.conflicts)
        self.emit("root-changed", pair.id)
        if rt.run is None:
            self._set_status(pair, rt, self._idle_status(pair, rt))

    def _new_conflicts(self, pair: Pair, rt: PairRuntime, originals: list[str]) -> None:
        """Conflicts a run just found: the folder's rule, if any, resolves
        what it can (the latest conflict of each file only), the user hears
        about what is left."""
        new, done = set(originals), 0
        rule = self.conflict_rule(pair)
        if rule != conflicts_mod.ASK:
            latest = {c.original: c for c in self.conflicts(pair.id) if c.original in new}
            done, _errors = self._resolve_decided(pair, rt, [conflicts_mod.decide(c, rule) for c in latest.values()])
            if done:
                self.emit("attention", pair.id, "resolved",
                          ngettext("{n} conflict resolved: both versions are kept, renamed.",
                                   "{n} conflicts resolved: both versions are kept, renamed.", done).format(n=done)
                          if rule == conflicts_mod.BOTH else
                          ngettext("{n} conflict resolved by the folder's rule: the version not kept is in the "
                                   "wastebasket.",
                                   "{n} conflicts resolved by the folder's rule: the versions not kept are in the "
                                   "wastebasket.", done).format(n=done))
                self.request_sync(pair.id, "conflict-resolved")
        left = len(new) - done
        if left > 0:
            self.emit("attention", pair.id, "conflicts",
                      ngettext("{n} file was changed both here and on Koofr: both versions are kept.",
                               "{n} files were changed both here and on Koofr: both versions are kept.",
                               left).format(n=left))

    def _enqueue(self, task: Task, front: bool = False) -> None:
        # a run already queued does the same (a confirmed one, without the
        # safety check, covers an automatic one)
        duplicate = any(t.pair_id == task.pair_id and t.mode == task.mode and not t.dry_run and not task.dry_run
                        and (t.force == task.force or t.force) for t in self.queue)
        running_same = (self.active is not None and self.active.pair_id == task.pair_id
                        and not self.active.dry_run and not task.dry_run and task.reason not in ("confirmed",))
        if duplicate:
            return
        if running_same:
            # it will run again afterwards if the changes need it
            self.runtime[task.pair_id].changed_during_run.add("")
            return
        if front and task.reason == "confirmed" and not task.dry_run:
            # confirmations in the order the user gave them
            at = max((i + 1 for i, t in enumerate(self.queue) if t.reason == "confirmed" and not t.dry_run),
                     default=0)
            self.queue.insert(at, task)
        elif front:
            self.queue.insert(0, task)
        else:
            self.queue.append(task)
        GLib.idle_add(self._next)

    # -- running -----------------------------------------------------------------

    def _next(self) -> bool:
        """Fill the free lanes: the real run, the preview. A pair already busy
        in the other lane waits."""
        if self._shutdown_cb or self._closing:
            return False
        for dry in (False, True):
            if (self.previewing if dry else self.active) is not None:
                continue
            task = next((t for t in self.queue if t.dry_run == dry and self._pair_free(t.pair_id)), None)
            if task is not None:
                self.queue = [t for t in self.queue if t is not task]
                self._prepare(task)
        return False

    def _pair_free(self, pair_id: str) -> bool:
        rt = self.runtime.get(pair_id)
        return rt is None or rt.task is None

    def _set_lane(self, task: Task, value: Task | None) -> None:
        if task.dry_run:
            self.previewing = value
        else:
            self.active = value

    def _prepare(self, task: Task) -> None:
        pair = self.cfg.pair(task.pair_id)
        if pair is None:
            GLib.idle_add(self._next)
            return
        rt = self.runtime[pair.id]

        def fail(message: str, status: Status = Status.ERROR) -> None:
            if task.callback:
                task.callback(None, message)
            if not task.dry_run:
                self._set_status(pair, rt, status, message)
            GLib.idle_add(self._next)

        if self.creds is None:
            return fail(_("Sign in to Koofr first."))
        if self.rclone is None:
            return fail(_("rclone was not found."))
        if task.reason not in ("preview", "confirmed", "manual") and (pair.paused or self.blocked_reason()):
            if task.dry_run:
                return fail(_("Stopped."))
            self._set_status(pair, rt, Status.PAUSED)
            GLib.idle_add(self._next)
            return
        # Preparing a run walks the whole folder (the names Koofr refuses) and
        # can take seconds on a large one: off the main loop, so the window, a
        # preview dialog and the tray never freeze. The lane is taken meanwhile.
        # A preview leaves the pair's status alone: it decides nothing.
        self._set_lane(task, task)
        rt.task = task
        rt.before_run = (rt.status, rt.message)
        if not task.dry_run:
            self._set_status(pair, rt, Status.SYNCING, _("Preparing…"))
        else:
            self.emit("pair-changed", pair.id)
        override = os.path.join(self.dev_remote, pair.remote.lstrip("/")) if self.dev_remote else None
        cfg, paths, rclone, creds = self.cfg, self.paths, self.rclone, self.creds

        def work():
            free = None
            try:
                if override:
                    os.makedirs(override, exist_ok=True)
                job = jobs.prepare(cfg, pair, paths, task.mode, task.dry_run, task.force, path2_override=override)
                result = (job, bisync_args(rclone, job.request), "")
                if task.mode is Mode.SYNC:
                    # a rename by case alone: on Koofr first (a preview only says it)
                    task.case_renames = jobs.case_renames(paths, pair)
                    if task.case_renames and not task.dry_run:
                        problem = jobs.rename_on_koofr(rclone, koofr_env(creds.user, creds.obscured), cfg, pair,
                                                       paths, task.case_renames, override)
                        if problem:
                            result = (None, None, problem)
                if task.dry_run:
                    free = free_space(rclone, paths, creds, override)
            except jobs.NotReady as e:
                result = (None, None, e.problems[0].message)
            except OSError as e:
                result = (None, None, str(e))
            GLib.idle_add(self._start_prepared, task, *result, free)

        threading.Thread(target=work, name=f"prepare-{pair.id}", daemon=True).start()

    def _start_prepared(self, task: Task, job, argv, error: str, free: int | None = None) -> bool:
        """The run prepared in a thread: start it, unless things changed
        meanwhile (shutdown, pair removed or paused, Stop pressed)."""
        self._set_lane(task, None)
        if self._closing:
            return False
        pair = self.cfg.pair(task.pair_id)
        rt = self.runtime.get(task.pair_id)
        if pair is None or rt is None:
            GLib.idle_add(self._next)
            return False
        rt.task = None

        def restore() -> None:
            if task.dry_run:
                self.emit("pair-changed", pair.id)
                return
            status, message = rt.before_run
            if status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
                self._set_status(pair, rt, status, message)
            else:
                self._set_status(pair, rt, self._idle_status(pair, rt))

        def fail(message: str, status: Status = Status.ERROR) -> bool:
            if task.callback:
                task.callback(None, message)
            if task.dry_run:
                restore()
            else:
                if rt.before_run != (status, message):
                    # a run that cannot start says so once, not at every poll
                    self.emit("attention", pair.id, "error", message)
                self._set_status(pair, rt, status, message)
            GLib.idle_add(self._next)
            return False

        if task.cancelled or (task.reason not in ("confirmed", "manual", "preview")
                              and (pair.paused or self.blocked_reason())):
            if task.callback:
                task.callback(None, _("Stopped."))
            restore()
            GLib.idle_add(self._next)
            return False
        if error:
            return fail(error)
        if self.creds is None:
            return fail(_("Sign in to Koofr first."))
        dry = task.dry_run
        run = BisyncRun(argv, koofr_env(self.creds.user, self.creds.obscured),
                        self.paths.lock_file(pair.id), self.paths.log_dir(pair.id),
                        job.path1, job.path2,
                        on_event=lambda ev, pid=pair.id: self._post_event(pid, ev, dry),
                        on_exit=lambda out, t=task: self._post_exit(t, out))
        try:
            run.start()
        except PairBusy:
            return fail(_("This folder is being synced by another Spola process."), Status.PENDING)
        except OSError as e:
            return fail(str(e))
        self._set_lane(task, task)
        rt.run, rt.task = run, task
        rt.pending_refused = job.refused
        rt.run_roots = (job.path1, job.path2)
        if task.dry_run:
            if rt.preview is not None and rt.preview.task is task:
                rt.preview.free_bytes = free
            self.emit("pair-changed", pair.id)
            return False
        rt.stats, rt.transferring = None, []
        rt.changed_during_run.clear()
        rt.activity = activity_mod.RunActivity()
        self._inhibit_cookie = self.inhibit(_("Syncing with Koofr"))
        self._set_status(pair, rt, Status.SYNCING, _("Syncing…"))
        return False

    def _post_event(self, pair_id: str, ev: Event, dry: bool) -> None:
        """In the reader thread: queue a line for the main loop. A preview
        needs only its progress; its plan comes with the outcome."""
        if dry and ev.kind is not Kind.STATS:
            return
        with self._events_lock:
            self._events.append((pair_id, ev))
            if self._events_scheduled:
                return
            self._events_scheduled = True
        GLib.idle_add(self._drain_events)

    def _post_exit(self, task: Task, outcome: Outcome) -> None:
        """In the reader thread: the end of the run, after its last lines."""
        with self._events_lock:
            self._events.append((None, (task, outcome)))
            if self._events_scheduled:
                return
            self._events_scheduled = True
        GLib.idle_add(self._drain_events)

    def _drain_events(self) -> bool:
        """In the main loop: the queued lines (and ends of runs), in order, a
        slice at a time."""
        end = time.monotonic() + EVENT_SLICE_S
        while time.monotonic() < end:
            with self._events_lock:
                if not self._events:
                    self._events_scheduled = False
                    return False
                pair_id, item = self._events.popleft()
            if pair_id is None:
                self._on_exit(*item)
            else:
                self._on_event(pair_id, item)
        return True

    def _on_event(self, pair_id: str, ev: Event) -> bool:
        rt = self.runtime.get(pair_id)
        pair = self.cfg.pair(pair_id)
        if rt is None or pair is None:
            return False
        real = not (rt.task and rt.task.dry_run)
        roots = list(rt.run_roots)
        if not real:
            if ev.kind is Kind.STATS and ev.stats and rt.preview is not None and rt.preview.task is rt.task:
                rt.preview.stats = ev.stats
                self.emit("pair-changed", pair_id)
            return False
        self._track(rt.activity, ev, lambda p: relative_path(p, roots))
        if ev.kind is Kind.STATS and ev.stats:
            rt.stats = ev.stats
            rt.transferring = list(ev.stats.get("transferring") or [])
            self.emit("pair-changed", pair_id)
        elif ev.kind is Kind.QUEUE and ev.path:
            rel = relative_path(ev.path, list(rt.run_roots))
            rt.states.syncing.add(rel)
            self.emit("paths-changed", pair_id, ancestors_and_self(rel))
        elif ev.kind is Kind.ERROR and ev.path:
            rel = relative_path(ev.path, list(rt.run_roots))
            rt.states.errors.add(rel)
            self.emit("paths-changed", pair_id, ancestors_and_self(rel))
        return False

    @staticmethod
    def _track(act: activity_mod.RunActivity, ev: Event, rel) -> None:
        """The file-by-file activity of a real run, for the window."""
        if ev.kind is Kind.QUEUE and ev.path:
            kind = activity_mod.kind_of_queue(ev.side, ev.action)
            if kind:
                act.queued(rel(ev.path), kind)
        elif ev.kind is Kind.PHASE:
            act.phase(ev.side)
        elif ev.kind is Kind.MOVED and ev.path:
            act.move_seen(rel(ev.path), rel(ev.target))
        elif ev.kind is Kind.RENAMED and ev.path:
            act.renamed(rel(ev.path))
        elif ev.kind is Kind.CONFLICT and ev.path:
            act.warning(rel(ev.path))
        elif ev.kind is Kind.CONFLICT_RENAME:
            act.copies_renamed()
        elif ev.kind is Kind.STATS and ev.stats:
            act.transferring(list(ev.stats.get("transferring") or []), rel)
        elif ev.kind is Kind.COPIED and ev.path:
            act.copied(rel(ev.path), to_koofr=ev.side == "Path2")
        elif ev.kind is Kind.DELETED and ev.path:
            act.deleted(rel(ev.path), here=(ev.side == "Path1") if ev.side else None)
        elif ev.kind is Kind.BACKUP and ev.path:
            act.backed_up(rel(ev.path))
        elif ev.kind is Kind.ERROR and ev.path:
            act.failed(rel(ev.path))

    def _on_exit(self, task: Task, outcome: Outcome) -> bool:
        self._set_lane(task, None)
        if not task.dry_run and self._inhibit_cookie:
            self.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        pair = self.cfg.pair(task.pair_id)
        rt = self.runtime.get(task.pair_id)
        fold_case_renames(outcome.plan, task.case_renames)
        if pair is not None and rt is not None:
            rt.run, rt.task = None, None
            if not task.dry_run:
                rt.transferring = []
                rt.activity.run_ended(outcome.result is Result.OK)
                self.invalidate_preview(pair.id)        # the folders are not what it saw
            self._handle_outcome(pair, rt, task, outcome)
            if not task.dry_run:
                self.emit("run-finished", pair.id, task.reason)
        if task.callback:
            task.callback(outcome, "")
        if self._shutdown_cb:
            if self.active is None and self.previewing is None:
                cb, self._shutdown_cb = self._shutdown_cb, None
                cb()
            return False
        GLib.idle_add(self._next)
        return False

    def _handle_outcome(self, pair: Pair, rt: PairRuntime, task: Task, outcome: Outcome) -> None:
        outcome.plan.refused = [r.path for r in rt.pending_refused]
        if rt.states.syncing or outcome.plan.error_paths:
            rt.states.syncing = set()
            rt.states.errors = {p for p in outcome.plan.error_paths}
            if outcome.result is not Result.OK:
                self.emit("root-changed", pair.id)
        if task.dry_run:
            return          # a preview decides nothing (its dialog has the outcome)
        rt.last_outcome = outcome
        result = outcome.result
        if result is Result.OK:
            new = {r.path for r in rt.pending_refused} - {r.path for r in rt.refused}
            rt.refused = list(rt.pending_refused)
            if new:
                self.emit("attention", pair.id, "refused",
                          ngettext("{n} file is not synced: Koofr does not accept its name.",
                                   "{n} files are not synced: Koofr does not accept their names.",
                                   len(rt.refused)).format(n=len(rt.refused)))
            rt.retries = 0
            rt.last_sync = time.time()
            if task.mode is Mode.RESYNC:
                if not pair.initialized:
                    pair.initialized = True
                    self.save_config()
                    self._start_watcher(pair, rt)
                # A resync leaves no .lst-old behind, so an interruption of the
                # next run could not be recovered (--recover needs it). A run
                # right away, with nothing to do, writes it.
                self._enqueue(Task(pair.id, Mode.SYNC, reason="seal"), front=True)
            rt.status = Status.OK
            plan = outcome.plan
            if plan.upload or plan.delete_remote:
                self.refresh_space(min_age=60)          # what Koofr holds just changed
            self.recent.record(pair.id, pair.local, plan.upload + [new for _old, new in plan.moved_remote],
                               plan.download + plan.replace_local + [new for _old, new in plan.moved_local])
            listed = self._update_conflicts(pair, rt)
            foreign = self._follow_up(pair, rt, outcome)
            touched = set(plan.upload) | set(plan.download) | set(plan.delete_local) \
                | set(plan.replace_local) | set(plan.conflicts) \
                | {p for move in plan.moved_remote + plan.moved_local for p in move}
            rt.states.run_finished(listed, touched, plan.error_paths, rt.conflicts, foreign)
            self.emit("root-changed", pair.id)
            if plan.conflicts:
                self._new_conflicts(pair, rt, plan.conflicts)
            self._set_status(pair, rt, self._idle_status(pair, rt))
        elif result is Result.NEEDS_CONFIRMATION:
            self._set_status(pair, rt, Status.NEEDS_CONFIRMATION, outcome.message)
            self.emit("attention", pair.id, "confirm", outcome.message)
        elif result is Result.NEEDS_REPAIR:
            self._set_status(pair, rt, Status.NEEDS_REPAIR, outcome.message)
            self.emit("attention", pair.id, "repair", outcome.message)
        elif result is Result.CANCELLED:
            self._set_status(pair, rt, self._idle_status(pair, rt))
        else:
            delay = RETRY_DELAYS_MIN[min(rt.retries, len(RETRY_DELAYS_MIN) - 1)]
            rt.retries += 1
            if rt.retry_source:
                GLib.source_remove(rt.retry_source)
            rt.retry_source = GLib.timeout_add_seconds(delay * 60, self._retry, pair.id)
            message = outcome.message or _("The sync stopped with an error.")
            if outcome.network:
                message = _("Koofr cannot be reached right now: trying again in {n} min.").format(n=delay)
            self._set_status(pair, rt, Status.ERROR, message)
            if rt.retries == 3:
                self.emit("attention", pair.id, "error", message)

    def _retry(self, pair_id: str) -> bool:
        rt = self.runtime.get(pair_id)
        if rt:
            rt.retry_source = 0
        self.request_sync(pair_id, "retry")
        return False

    def _update_conflicts(self, pair: Pair, rt: PairRuntime) -> list[str]:
        """Reload the last listing: the conflicts, and the synced paths for
        the file manager. Returns the listed paths."""
        entries = listings.load(self.paths.workdir(pair.id), 1) if pair.initialized else None
        paths = [e.path for e in entries or []]
        rt.conflicts = [e.path for e in entries or [] if not e.is_dir and self._conflict_name.search(e.path)]
        rt.states.load_listing(paths)
        rt.states.conflicts = set(rt.conflicts)
        rt.states.refused = {r.path for r in rt.refused}
        return paths

    def _follow_up(self, pair: Pair, rt: PairRuntime, outcome: Outcome) -> set[str]:
        """Changes seen while rclone ran: ignore the ones rclone made itself.
        Returns the paths that changed on their own (still pending)."""
        touched = set(outcome.plan.download) | set(outcome.plan.delete_local) | set(outcome.plan.replace_local)
        for name in outcome.plan.conflicts:
            touched.add(name)
        for old, new in outcome.plan.moved_local:       # rclone moved these here itself
            touched |= {old, new}
        parents = {os.path.dirname(p) for p in touched}
        foreign = {rel for rel in rt.changed_during_run
                   if rel not in touched and rel not in parents and not self._conflict_name.search(rel)}
        rt.changed_during_run.clear()
        if foreign:
            self._schedule_debounced(pair, rt)
        return foreign

    # -- local changes, periodic checks, conditions --------------------------------

    def _start_watcher(self, pair: Pair, rt: PairRuntime) -> None:
        if rt.watcher or not pair.initialized or pair.paused or not os.path.isdir(pair.local):
            return
        rt.watcher = TreeWatcher(pair.local, lambda rel, pid=pair.id: self._on_local_change(pid, rel),
                                 ignore=quick_ignore)
        rt.watcher.start()

    def _on_local_change(self, pair_id: str, rel: str) -> None:
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None:
            return
        if pair.initialized:
            self.emit("paths-changed", pair_id, rt.states.mark_pending(rel))
        if rt.preview is not None:
            self.invalidate_preview(pair_id)
        if rt.run is not None and not (rt.task and rt.task.dry_run):
            rt.changed_during_run.add(rel)
            return
        self._schedule_debounced(pair, rt)

    def _schedule_debounced(self, pair: Pair, rt: PairRuntime) -> None:
        now = time.monotonic()
        if not rt.debounce_source:
            rt.first_change = now
        else:
            GLib.source_remove(rt.debounce_source)
        delay = DEBOUNCE_S if now - rt.first_change < MAX_WAIT_S - DEBOUNCE_S else 1
        rt.debounce_source = GLib.timeout_add_seconds(delay, self._debounce_fired, pair.id)
        if rt.status in (Status.OK, Status.CONFLICTS, Status.SKIPPED):
            self._set_status(pair, rt, Status.PENDING)

    def _debounce_fired(self, pair_id: str) -> bool:
        rt = self.runtime.get(pair_id)
        if rt:
            rt.debounce_source = 0
        self.request_sync(pair_id, "local-change")
        return False

    def _reschedule_poll(self) -> None:
        """Full runs every hour while the activity feed tells us what changed
        on Koofr, every `poll_minutes` when it does not."""
        if self._poll_source:
            GLib.source_remove(self._poll_source)
        minutes = self.cfg.full_check_minutes if self.feed_ok else self.cfg.poll_minutes
        self._poll_source = GLib.timeout_add_seconds(max(1, int(minutes)) * 60, self._poll)

    # -- Koofr's activity feed ------------------------------------------------------

    def _poll_activity(self) -> bool:
        creds = self.creds
        if (self._activity_busy or creds is None or not creds.password or not self.feed_enabled
                or self.blocked_reason() or not any(p.initialized and not p.paused for p in self.cfg.pairs)):
            return True
        self._activity_busy = True
        since, mount_id = self._activity_since, self.cfg.mount_id

        def work():
            try:
                mount = mount_id or koofr_api.primary_mount_id(creds.user, creds.password)
                result = (mount, koofr_api.activity(creds.user, creds.password, mount, since)), None
            except koofr_api.ApiError as e:
                result = None, e
            GLib.idle_add(self._activity_done, *result)

        threading.Thread(target=work, name="koofr-activity", daemon=True).start()
        return True

    def _activity_done(self, result, error) -> bool:
        self._activity_busy = False
        if error is not None or result is None:
            self._set_feed(False)
            return False
        mount_id, activities = result
        if mount_id != self.cfg.mount_id:
            self.cfg.mount_id = mount_id
            self.save_config()
        new = [a for a in activities if a.id not in self._activity_seen]
        if activities:
            # the feed may include the boundary instant: remember what was seen there
            self._activity_since = max(a.timestamp for a in activities)
            self._activity_seen = {a.id for a in activities if a.timestamp == self._activity_since}
        touched, _full = koofr_api.touched_pairs(new, self.cfg.pairs, mount_id, self.cfg.instance_id)
        for pair_id in sorted(touched):
            self.invalidate_preview(pair_id)
            self.request_sync(pair_id, "remote-change")
        self._set_feed(True)
        return False

    def _set_feed(self, ok: bool) -> None:
        if ok != self.feed_ok:
            self.feed_ok = ok
            self._reschedule_poll()
            self.emit("status-changed")

    def _poll(self) -> bool:
        self.sync_all("poll")
        self.refresh_space()
        return True

    def _on_conditions(self, *_args) -> None:
        if not self.blocked_reason():
            self.sync_all("conditions")
        else:
            # a metered connection or power saver mode, just now: an automatic
            # run stops (bisync saves its state and goes on later); one the user
            # asked for, or a preview, goes on
            task = self.active
            if task is not None and task.reason not in ("manual", "confirmed"):
                self.stop(task.pair_id)
            self._cancel_previews_ahead()
        for pair in self.cfg.pairs:
            rt = self.runtime[pair.id]
            if rt.run is None:
                self._set_status(pair, rt, self._idle_status(pair, rt))
        self.emit("status-changed")
