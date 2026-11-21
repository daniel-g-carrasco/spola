#!/usr/bin/env python3
"""Sync Now is never left without an answer: what the run did, that there
was nothing to do, or why no run follows; in the window when it is in
front (in the dialog open over it, when there is one), in a notification
when it is not. Sync All Now is one answer, not one a folder. The names
Koofr refuses are held against the disk when Review is opened, and their
dialog has its own Sync Now. The window and the application are too large
to build here (they need the engine): their methods are run on stand-ins
that hold only what the methods touch."""

import locale
import types
import unittest

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
# GTK sets the user's locale: the other tests read numbers as in C ("2000")
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")

from spola.app import preflight  # noqa: E402
from spola.app.engine import Status  # noqa: E402
from spola.app.rclone import Outcome, Plan, Result  # noqa: E402
from spola.gui import application, window as window_module  # noqa: E402
from spola.gui.ui import SyncAnswers, run_summary, status_detail  # noqa: E402
from spola.gui.window import LeftOutDialog, MainWindow  # noqa: E402


class Overlay:
    """Where the toasts go, kept to be read."""

    def __init__(self):
        self.toasts = []

    def add_toast(self, toast) -> None:
        if toast not in self.toasts:        # (shown again, as Adw.ToastOverlay does: not a second one)
            self.toasts.append(toast)

    def titles(self) -> list[str]:
        return [toast.get_title() for toast in self.toasts]


def runtime(status=Status.OK, refused=(), plan=None, result=Result.OK):
    return types.SimpleNamespace(status=status, message="", refused=list(refused), conflicts=[], preview=None, gone=False,
                                 last_outcome=Outcome(result, 0, plan=plan or Plan()))


def fake_engine(pairs: dict, whys: dict | None = None):
    """`pairs`: id -> (name, runtime); `whys`: id -> what request_sync answers."""
    engine = types.SimpleNamespace(asked=[], rechecked=[], left=0)
    engine.cfg = types.SimpleNamespace(
        pairs=[types.SimpleNamespace(id=pid, name=name, local="/nowhere", remote="/r") for pid, (name, _rt) in pairs.items()])
    engine.cfg.pair = lambda pid: next((pair for pair in engine.cfg.pairs if pair.id == pid), None)
    engine.runtime = {pid: rt for pid, (_name, rt) in pairs.items()}

    def request_sync(pid, reason):
        engine.asked.append((pid, reason))
        return (whys or {}).get(pid, "")

    def recheck_refused(pid):
        engine.rechecked.append(pid)
        return engine.left
    engine.request_sync, engine.recheck_refused = request_sync, recheck_refused
    engine.sync_now = lambda pid: request_sync(pid, "manual")
    engine.connect, engine.disconnect = (lambda *args: 1), (lambda handler: None)
    engine.kept_listings = lambda pid: []
    return engine


class FakeWindow:
    """What MainWindow's own methods for the answers touch."""

    def __init__(self, engine, active: bool = True):
        self.engine, self.active = engine, active
        self.toasts = Overlay()
        self._answers, self._idle_toast, self._idle_answers = None, None, SyncAnswers()
        self.shown, self.opened = [], []

    def is_active(self) -> bool:
        return self.active

    def show_pair(self, pair_id: str) -> None:
        self.shown.append(pair_id)

    for _name in ("sync_now", "answers_in", "_answer", "_on_run_finished", "toast", "review"):
        locals()[_name] = getattr(MainWindow, _name)
    del _name


class StatusTest(unittest.TestCase):
    def test_a_folder_waiting_for_its_turn_says_so(self):
        """One folder syncs at a time: "Changes waiting to be synced" alone,
        for as long as another one uploads, looked like a sync that never starts."""
        engine = fake_engine({"p1": ("audio", runtime(Status.PENDING))})
        engine.confirmed_waiting = lambda pid: False
        engine.waits_behind = lambda pid: ""
        self.assertEqual(status_detail(engine, "p1", short=False), "")
        engine.waits_behind = lambda pid: "albums"
        self.assertEqual(status_detail(engine, "p1", short=False), "It starts as soon as the sync in progress is done.")
        self.assertEqual(status_detail(engine, "p1", short=True), "starts after the sync in progress")


class SummaryTest(unittest.TestCase):
    def test_what_a_run_did_and_what_it_left_out(self):
        self.assertEqual(run_summary(runtime()), "")
        self.assertEqual(run_summary(runtime(plan=Plan(upload=["a", "b"]))), "2 files to Koofr")
        refused = [preflight.Refused("x/08 - \U0001F534.flac", False, "emoji")]
        self.assertEqual(run_summary(runtime(refused=refused)), "nothing new to sync, 1 file left out")
        self.assertEqual(run_summary(runtime(refused=refused, plan=Plan(upload=["a"]))),
                         "1 file to Koofr, 1 file left out")

    def test_several_folders_are_one_answer(self):
        answers = SyncAnswers()
        answers.add("audio", "")
        self.assertEqual(answers.text(), "“audio” was already up to date")
        answers.add("albums", "")
        answers.add("audio", "")                                # the same folder twice is one
        self.assertEqual(answers.text(), "2 folders were already up to date")
        answers.add("work", "3 files to Koofr")
        self.assertEqual(answers.text(), "“work”: 3 files to Koofr\n2 folders were already up to date")
        answers.clear()
        self.assertEqual(answers.text(), "")


class WindowTest(unittest.TestCase):
    def test_a_run_with_nothing_to_do_says_so(self):
        window = FakeWindow(fake_engine({"p1": ("audio", runtime())}))
        window.sync_now("p1")
        self.assertEqual(window.engine.asked, [("p1", "manual")])
        self.assertEqual(window.toasts.titles(), [])            # the run answers when it ends
        window._on_run_finished(window.engine, "p1", "manual")
        self.assertEqual(window.toasts.titles(), ["“audio” was already up to date"])

    def test_a_run_that_cannot_start_says_why_at_once(self):
        window = FakeWindow(fake_engine({"p1": ("audio", runtime())}, {"p1": "“audio” is paused: turn on Keep in Sync first."}))
        window.sync_now("p1")
        self.assertEqual(window.toasts.titles(), ["“audio” is paused: turn on Keep in Sync first."])

    def test_files_left_out_are_not_up_to_date(self):
        window = FakeWindow(fake_engine({"p1": ("audio", runtime(Status.SKIPPED, refused=[object()]))}))
        window._on_run_finished(window.engine, "p1", "manual")
        self.assertEqual(window.toasts.titles(), ["“audio”: nothing new to sync, 1 file left out"])

    def test_sync_all_is_one_toast(self):
        window = FakeWindow(fake_engine({pid: (pid, runtime()) for pid in ("a", "b", "c")}
                                        | {"d": ("work", runtime(plan=Plan(download=["x"])))}))
        for pid in ("a", "b", "c", "d"):
            window._on_run_finished(window.engine, pid, "manual")
        self.assertEqual(window.toasts.titles(), ["3 folders were already up to date", "“work”: 1 file from Koofr"])
        window.toasts.toasts[0].emit("dismissed")               # gone: the next Sync Now starts its own count
        window._on_run_finished(window.engine, "a", "manual")
        self.assertEqual(window.toasts.titles()[-1], "“a” was already up to date")

    def test_only_what_was_asked_and_went_well_and_with_the_window_in_front(self):
        window = FakeWindow(fake_engine({"p1": ("audio", runtime()), "p2": ("bad", runtime(result=Result.FAILED))}))
        window._on_run_finished(window.engine, "p1", "poll")    # nobody asked
        window._on_run_finished(window.engine, "p2", "manual")  # the status says what went wrong
        window.active = False
        window._on_run_finished(window.engine, "p1", "manual")  # behind: the application notifies
        self.assertEqual(window.toasts.titles(), [])

    def test_with_a_dialog_open_the_answer_is_in_the_dialog(self):
        window, inside = FakeWindow(fake_engine({"p1": ("audio", runtime())})), Overlay()
        window.answers_in(inside)
        window._on_run_finished(window.engine, "p1", "manual")
        self.assertEqual((window.toasts.titles(), inside.titles()), ([], ["“audio” was already up to date"]))
        window.answers_in(None)
        window._on_run_finished(window.engine, "p1", "manual")
        self.assertEqual(window.toasts.titles(), ["“audio” was already up to date"])


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.dialogs = []
        self._dialog = window_module.LeftOutDialog
        window_module.LeftOutDialog = lambda window, pair_id: types.SimpleNamespace(
            present=lambda parent: self.dialogs.append(pair_id))

    def tearDown(self):
        window_module.LeftOutDialog = self._dialog

    def test_review_looks_at_the_disk_first(self):
        rt = runtime(Status.SKIPPED, refused=[object()])
        window = FakeWindow(fake_engine({"p1": ("audio", rt)}))
        window.review("p1")
        self.assertEqual((window.engine.rechecked, self.dialogs), (["p1"], ["p1"]))

    def test_nothing_left_to_review_is_said_not_an_empty_dialog(self):
        rt = runtime(Status.SKIPPED, refused=[object()])
        window = FakeWindow(fake_engine({"p1": ("audio", rt)}))

        def recheck(pid):                                       # the file was renamed since the last run
            rt.refused.clear()
            rt.status = Status.PENDING
            return 1
        window.engine.recheck_refused = recheck
        window.review("p1")
        self.assertEqual(self.dialogs, [])
        self.assertEqual(window.toasts.titles(), ["“audio”: no file is left out any more"])


class LeftOutDialogTest(unittest.TestCase):
    def test_its_own_sync_now(self):
        engine = fake_engine({"p1": ("audio", runtime(Status.SKIPPED))})
        window = FakeWindow(engine)
        dialog = LeftOutDialog(window, "p1")
        self.assertIs(window._answers, dialog.toasts)           # the answers show over the window, in the dialog
        self.assertEqual(dialog.sync.get_title(), "Sync Now")
        dialog.sync.emit("activated")
        self.assertEqual((engine.rechecked, engine.asked), (["p1"], [("p1", "manual")]))
        dialog._closed()
        self.assertIsNone(window._answers)


def app_with(window, engine, whys: str = ""):
    app = types.SimpleNamespace(window=window, engine=engine, told=[], _sync_answers=SyncAnswers(), _answered_at=0.0)
    app._tell = app.told.append
    engine.sync_all = lambda reason: whys
    for name in ("_on_run_finished", "_sync_all"):
        setattr(app, name, types.MethodType(getattr(application.SpolaApp, name), app))
    return app


class ApplicationTest(unittest.TestCase):
    def test_from_the_tray_the_answer_is_a_notification(self):
        engine = fake_engine({"a": ("audio", runtime()), "b": ("albums", runtime()),
                              "c": ("work", runtime(plan=Plan(upload=["x"])))})
        app = app_with(None, engine)                            # in the tray: no window
        app._on_run_finished(engine, "a", "manual")
        self.assertEqual(app.told, ["“audio” was already up to date"])
        app._on_run_finished(engine, "b", "manual")
        app._on_run_finished(engine, "c", "manual")             # the same notification, which grows
        self.assertEqual(app.told[-1], "“work”: 1 file to Koofr\n2 folders were already up to date")
        app._on_run_finished(engine, "a", "poll")
        self.assertEqual(len(app.told), 3)

    def test_a_later_request_is_another_answer(self):
        engine = fake_engine({"a": ("audio", runtime()), "b": ("albums", runtime())})
        app = app_with(FakeWindow(engine, active=False), engine)
        app._on_run_finished(engine, "a", "manual")
        app._answered_at -= application.SYNC_ANSWER_S + 1
        app._on_run_finished(engine, "b", "manual")
        self.assertEqual(app.told, ["“audio” was already up to date", "“albums” was already up to date"])

    def test_with_the_window_in_front_the_window_answers(self):
        engine = fake_engine({"a": ("audio", runtime())})
        app = app_with(FakeWindow(engine, active=True), engine)
        app._on_run_finished(engine, "a", "manual")
        self.assertEqual(app.told, [])

    def test_sync_all_that_starts_nothing_says_why(self):
        engine = fake_engine({"a": ("audio", runtime())})
        window = FakeWindow(engine, active=True)
        app = app_with(window, engine, "No folder can sync now: each one says why on its page.")
        app._sync_all()
        self.assertEqual(window.toasts.titles(), ["No folder can sync now: each one says why on its page."])
        window.active = False
        app._sync_all()
        self.assertEqual(app.told, ["No folder can sync now: each one says why on its page."])
        engine.sync_all = lambda reason: ""                     # runs follow: they answer when they end
        app._sync_all()
        self.assertEqual(len(app.told), 1)


if __name__ == "__main__":
    unittest.main()
