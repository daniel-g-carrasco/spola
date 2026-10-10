"""The scheduler: decides when each pair runs, one run at a time, and keeps
the status that the window, the tray and (later) Nautilus show.

Rules, from docs/progetto.md (5.3, 5.4):
  * runs are queued and executed one at a time for the whole account;
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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from gi.repository import Gio, GLib, GObject

from . import config as config_mod
from . import conflicts as conflicts_mod
from . import jobs, koofr_api, listings
from .account import Credentials
from .config import Config, Pair
from .i18n import _, every_translation, ngettext
from .paths import Paths
from .rclone import Event, Kind, Mode, Outcome, Result, bisync_args, default_excluded, find_rclone, koofr_env
from .runner import BisyncRun, PairBusy
from .watcher import TreeWatcher

DEBOUNCE_S = 10
MAX_WAIT_S = 120
RETRY_DELAYS_MIN = (1, 5, 15, 30, 60)
RESUME_GAP_S = 90       # wall clock ahead of the monotonic clock: the machine slept
ACTIVITY_INTERVAL_S = 60


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


@dataclass
class Task:
    pair_id: str
    mode: Mode = Mode.SYNC
    dry_run: bool = False
    force: bool = False
    reason: str = ""
    callback: Callable[[Outcome | None, str], None] | None = None   # (outcome, error)


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
    before_run: tuple = (None, "")          # (status, message) a preview must restore
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
        # Koofr's activity feed as a doorbell: None = not tried yet
        self.feed_ok: bool | None = None
        self.feed_enabled = self.dev_remote is None
        self._activity_since = int(time.time() * 1000)
        self._activity_seen: set[str] = set()
        self._activity_busy = False
        self.runtime: dict[str, PairRuntime] = {p.id: PairRuntime() for p in cfg.pairs}
        self.queue: list[Task] = []
        self.active: Task | None = None
        self._poll_source = 0
        self._shutdown_cb: Callable[[], None] | None = None
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
        self.queue.clear()
        for rt in self.runtime.values():
            if rt.watcher:
                rt.watcher.stop()
        current = self._current_run()
        if current is None:
            done()
            return
        self._shutdown_cb = done
        current.cancel()
        # bisync needs up to 30 s for transfers plus 60 s to save its state;
        # after that a second SIGINT stops it at once (--recover handles it).
        GLib.timeout_add_seconds(95, self._force_stop)

    def _force_stop(self) -> bool:
        current = self._current_run()
        if current is not None:
            current.cancel()
        return False

    def _current_run(self) -> BisyncRun | None:
        if self.active is None:
            return None
        rt = self.runtime.get(self.active.pair_id)
        return rt.run if rt else None

    def save_config(self) -> None:
        config_mod.save(self.cfg, self.paths.config_file)

    # -- pairs -----------------------------------------------------------------

    def _setup_pair(self, pair: Pair) -> None:
        rt = self.runtime.setdefault(pair.id, PairRuntime())
        rt.status = self._idle_status(pair, rt)
        self._update_conflicts(pair, rt)
        self._start_watcher(pair, rt)

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
        rt = self.runtime.pop(pair_id, None)
        self.queue = [t for t in self.queue if t.pair_id != pair_id]
        if rt:
            if rt.watcher:
                rt.watcher.stop()
            if rt.run and rt.run.running:
                rt.run.cancel()
            for source in (rt.retry_source, rt.debounce_source):
                if source:
                    GLib.source_remove(source)
        self.cfg.pairs.remove(pair)
        self.save_config()
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
        else:
            self._start_watcher(pair, rt)
            self.request_sync(pair_id, "resume")
        self._set_status(pair, rt, self._idle_status(pair, rt) if not rt.run else rt.status)

    def set_paused_all(self, paused: bool) -> None:
        self.cfg.paused_all = paused
        self.save_config()
        if paused:
            self.queue = [t for t in self.queue if t.dry_run]
            current = self._current_run()
            if current and self.active and not self.active.dry_run:
                current.cancel()
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
        for pair in self.cfg.pairs:
            rt = self.runtime[pair.id]
            if not rt.run:
                self._set_status(pair, rt, self._idle_status(pair, rt))
        self.emit("status-changed")

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
        if not pair.initialized:
            return Status.NEEDS_SETUP
        if pair.paused or self.blocked_reason():
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
        self.emit("pair-changed", pair.id)

    def overall(self) -> Status:
        statuses = [rt.status for pid, rt in self.runtime.items() if self.cfg.pair(pid)]
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
        if self.blocked_reason() and reason != "manual":
            if rt.run is None:
                self._set_status(pair, rt, Status.PAUSED)
            return
        if rt.debounce_source:
            GLib.source_remove(rt.debounce_source)
            rt.debounce_source = 0
        self._enqueue(Task(pair_id, Mode.SYNC, reason=reason))

    def preview(self, pair_id: str, mode: Mode, force: bool,
                callback: Callable[[Outcome | None, str], None]) -> None:
        """Dry run, ahead of the queue, for a confirmation dialog."""
        self._enqueue(Task(pair_id, mode, dry_run=True, force=force, reason="preview", callback=callback),
                      front=True)

    def confirm(self, pair_id: str, mode: Mode, force: bool,
                callback: Callable[[Outcome | None, str], None] | None = None) -> None:
        """The real run the user confirmed after a preview."""
        rt = self.runtime[pair_id]
        if rt.status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
            rt.status = Status.PENDING
        self._enqueue(Task(pair_id, mode, force=force, reason="confirmed", callback=callback), front=True)

    def stop(self, pair_id: str) -> None:
        rt = self.runtime.get(pair_id)
        if rt and rt.run and rt.run.running:
            rt.run.cancel()

    def set_filters(self, pair_id: str, text: str) -> bool:
        """Store the pair's exclusions. If they changed and the pair is
        aligned, it needs a new alignment (bisync refuses to run otherwise):
        the pair is marked for repair, to be confirmed from a preview."""
        pair, rt = self.cfg.pair(pair_id), self.runtime.get(pair_id)
        if pair is None or rt is None or not jobs.write_filters(self.paths, pair, text):
            return False
        if pair.initialized:
            self.queue = [t for t in self.queue if t.pair_id != pair_id or t.dry_run]
            if rt.run is None:
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
        if rt.run is not None and not (rt.task and rt.task.dry_run):
            raise conflicts_mod.ResolveError(_("Wait for the current sync to finish."))
        conflicts_mod.resolve(pair.local, conflict, choice)
        gone = {c.path for c in conflict.copies}
        rt.conflicts = [p for p in rt.conflicts if p not in gone]
        if rt.run is None:
            self._set_status(pair, rt, self._idle_status(pair, rt))
        self.request_sync(pair_id, "conflict-resolved")

    def _enqueue(self, task: Task, front: bool = False) -> None:
        duplicate = any(t.pair_id == task.pair_id and t.mode == task.mode and not t.dry_run and not task.dry_run
                        and t.force == task.force for t in self.queue)
        running_same = (self.active is not None and self.active.pair_id == task.pair_id
                        and not self.active.dry_run and not task.dry_run and task.reason not in ("confirmed",))
        if duplicate:
            return
        if running_same:
            # it will run again afterwards if the changes need it
            self.runtime[task.pair_id].changed_during_run.add("")
            return
        if front:
            self.queue.insert(0, task)
        else:
            self.queue.append(task)
        GLib.idle_add(self._next)

    # -- running -----------------------------------------------------------------

    def _next(self) -> bool:
        if self.active is not None or not self.queue or self._shutdown_cb:
            return False
        task = self.queue.pop(0)
        pair = self.cfg.pair(task.pair_id)
        if pair is None:
            GLib.idle_add(self._next)
            return False
        rt = self.runtime[pair.id]

        def fail(message: str, status: Status = Status.ERROR) -> bool:
            if task.callback:
                task.callback(None, message)
            if not task.dry_run:
                self._set_status(pair, rt, status, message)
            GLib.idle_add(self._next)
            return False

        if self.creds is None:
            return fail(_("Sign in to Koofr first."))
        if self.rclone is None:
            return fail(_("rclone was not found."))
        if task.reason not in ("preview", "confirmed", "manual") and (pair.paused or self.blocked_reason()):
            self._set_status(pair, rt, Status.PAUSED)
            GLib.idle_add(self._next)
            return False
        try:
            override = os.path.join(self.dev_remote, pair.remote.lstrip("/")) if self.dev_remote else None
            if override:
                os.makedirs(override, exist_ok=True)
            job = jobs.prepare(self.cfg, pair, self.paths, task.mode, task.dry_run, task.force,
                               path2_override=override)
            argv = bisync_args(self.rclone, job.request)
        except jobs.NotReady as e:
            return fail(e.problems[0].message)
        except OSError as e:
            return fail(str(e))
        run = BisyncRun(argv, koofr_env(self.creds.user, self.creds.obscured),
                        self.paths.lock_file(pair.id), self.paths.log_dir(pair.id),
                        job.path1, job.path2,
                        on_event=lambda ev, pid=pair.id: GLib.idle_add(self._on_event, pid, ev),
                        on_exit=lambda out, t=task: GLib.idle_add(self._on_exit, t, out))
        try:
            run.start()
        except PairBusy:
            return fail(_("This folder is being synced by another Spola process."), Status.PENDING)
        except OSError as e:
            return fail(str(e))
        self.active = task
        rt.run, rt.task = run, task
        rt.pending_refused = job.refused
        rt.stats, rt.transferring = None, []
        rt.changed_during_run.clear()
        rt.before_run = (rt.status, rt.message)
        if not task.dry_run:
            self._inhibit_cookie = self.inhibit(_("Syncing with Koofr"))
        message = _("Preparing the preview…") if task.dry_run else _("Syncing…")
        self._set_status(pair, rt, Status.SYNCING, message)
        return False

    def _on_event(self, pair_id: str, ev: Event) -> bool:
        rt = self.runtime.get(pair_id)
        pair = self.cfg.pair(pair_id)
        if rt is None or pair is None:
            return False
        if ev.kind is Kind.STATS and ev.stats:
            rt.stats = ev.stats
            rt.transferring = list(ev.stats.get("transferring") or [])
            self.emit("pair-changed", pair_id)
        return False

    def _on_exit(self, task: Task, outcome: Outcome) -> bool:
        self.active = None
        if self._inhibit_cookie:
            self.uninhibit(self._inhibit_cookie)
            self._inhibit_cookie = 0
        pair = self.cfg.pair(task.pair_id)
        rt = self.runtime.get(task.pair_id)
        if pair is not None and rt is not None:
            rt.run, rt.task = None, None
            rt.transferring = []
            self._handle_outcome(pair, rt, task, outcome)
            if not task.dry_run:
                self.emit("run-finished", pair.id, task.reason)
        if task.callback:
            task.callback(outcome, "")
        if self._shutdown_cb:
            cb, self._shutdown_cb = self._shutdown_cb, None
            cb()
            return False
        GLib.idle_add(self._next)
        return False

    def _handle_outcome(self, pair: Pair, rt: PairRuntime, task: Task, outcome: Outcome) -> None:
        outcome.plan.refused = [r.path for r in rt.pending_refused]
        if task.dry_run:
            # a preview decides nothing: a pair waiting for the user keeps waiting
            status, message = rt.before_run
            if status in (Status.NEEDS_REPAIR, Status.NEEDS_CONFIRMATION):
                self._set_status(pair, rt, status, message)
            else:
                self._set_status(pair, rt, self._idle_status(pair, rt))
            return
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
            self._update_conflicts(pair, rt)
            if outcome.plan.conflicts:
                self.emit("attention", pair.id, "conflicts",
                          ngettext("{n} file was changed both here and on Koofr: both versions are kept.",
                                   "{n} files were changed both here and on Koofr: both versions are kept.",
                                   len(outcome.plan.conflicts)).format(n=len(outcome.plan.conflicts)))
            self._set_status(pair, rt, self._idle_status(pair, rt))
            self._follow_up(pair, rt, outcome)
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

    def _update_conflicts(self, pair: Pair, rt: PairRuntime) -> None:
        entries = listings.load(self.paths.workdir(pair.id), 1) if pair.initialized else None
        rt.conflicts = [e.path for e in entries or [] if not e.is_dir and self._conflict_name.search(e.path)]

    def _follow_up(self, pair: Pair, rt: PairRuntime, outcome: Outcome) -> None:
        """Changes seen while rclone ran: ignore the ones rclone made itself."""
        touched = set(outcome.plan.download) | set(outcome.plan.delete_local) | set(outcome.plan.replace_local)
        for name in outcome.plan.conflicts:
            touched.add(name)
        parents = {os.path.dirname(p) for p in touched}
        foreign = {rel for rel in rt.changed_during_run
                   if rel not in touched and rel not in parents and not self._conflict_name.search(rel)}
        rt.changed_during_run.clear()
        if foreign:
            self._schedule_debounced(pair, rt)

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
        if rt.run is not None:
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
        return True

    def _on_conditions(self, *_args) -> None:
        if not self.blocked_reason():
            self.sync_all("conditions")
        for pair in self.cfg.pairs:
            rt = self.runtime[pair.id]
            if rt.run is None:
                self._set_status(pair, rt, self._idle_status(pair, rt))
        self.emit("status-changed")
