"""Command line: the same rules as the window, for tests and for people who
like a terminal. Commands that change the configuration refuse to run while
the app is running (it owns the configuration then).

    spola login
    spola add ~/scratch/spola-test /spola-test
    spola status
    spola preview spola-test
    spola align spola-test          # first alignment: preview, then asks
    spola sync spola-test [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import APP_ID, KOOFR_APP_PASSWORDS_URL, __version__, config, jobs, koofr_api, listings, preflight
from .account import AccountError, Credentials, check
from .i18n import _, ngettext
from .paths import Paths
from .rclone import Mode, Outcome, Result, bisync_args, default_excluded, find_rclone, koofr_env
from .runner import BisyncRun, PairBusy

COMMANDS = ("login", "logout", "status", "add", "remove", "preview", "align", "repair", "sync")


def _app_running() -> bool:
    try:
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "NameHasOwner", GLib.Variant("(s)", (APP_ID,)), None,
                              Gio.DBusCallFlags.NONE, 2000, None)
        return bool(reply.unpack()[0])
    except Exception:
        return False


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _credentials(cfg: config.Config, rclone: str) -> Credentials | None:
    from . import secrets
    password = secrets.lookup(cfg.user) if cfg.user else None
    return Credentials.from_password(cfg.user, password, rclone) if password else None


def _find_pair(cfg: config.Config, key: str) -> config.Pair | None:
    for pair in cfg.pairs:
        if key in (pair.id, pair.name) or Path(key).expanduser().resolve() == Path(pair.local):
            return pair
    return None


def _print_plan(outcome: Outcome) -> None:
    plan = outcome.plan
    sections = [
        (plan.upload, _("Copied to Koofr")),
        (plan.download, _("Copied to this computer")),
        (plan.replace_local, _("Replaced here by the newer Koofr version (the old one goes to the backup)")),
        (plan.delete_local, _("Deleted here (moved to Spola's backup)")),
        (plan.delete_remote, _("Deleted on Koofr")),
        (plan.conflicts, _("Changed on both sides: both versions are kept")),
        (plan.refused, _("Left out: Koofr does not accept these names (rename them to sync them)")),
    ]
    if plan.empty and outcome.result is Result.OK:
        print(_("Nothing to do: the two folders already match."))
    for paths, title in sections:
        if paths:
            print(f"\n{title}: " + ngettext("{n} file", "{n} files", len(paths)).format(n=len(paths)))
            for p in paths[:50]:
                print(f"  {p}")
            if len(paths) > 50:
                print("  " + _("…and {n} more").format(n=len(paths) - 50))
    if outcome.result is not Result.OK and outcome.message:
        print("\n" + outcome.message)


def _run(cfg, pair, paths, rclone, creds, mode, dry_run=False, force=False, quiet=False) -> Outcome | None:
    try:
        job = jobs.prepare(cfg, pair, paths, mode, dry_run, force)
    except jobs.NotReady as e:
        _fail(e.problems[0].message)
        return None

    def on_event(ev):
        if not quiet and ev.stats and ev.stats.get("totalTransfers"):
            s = ev.stats
            print(f"\r  {s.get('transfers', 0)}/{s.get('totalTransfers', 0)} files, "
                  f"{s.get('bytes', 0) // 1048576}/{(s.get('totalBytes') or 0) // 1048576} MB", end="", flush=True)

    run = BisyncRun(bisync_args(rclone, job.request), koofr_env(creds.user, creds.obscured),
                    paths.lock_file(pair.id), paths.log_dir(pair.id), job.path1, job.path2, on_event=on_event)
    try:
        run.start()
    except PairBusy:
        _fail(_("This folder is being synced by another Spola process."))
        return None
    try:
        outcome = run.wait()
    except KeyboardInterrupt:
        print("\n" + _("Stopping (the current transfers finish first; press Ctrl+C again to stop at once)…"))
        run.cancel()
        try:
            outcome = run.wait()
        except KeyboardInterrupt:
            run.cancel()
            outcome = run.wait()
    if not quiet:
        print()
    if outcome is not None:
        outcome.plan.refused = [r.path for r in job.refused]
    return outcome


def _ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes", "s", "si", "sì")
    except EOFError:
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spola", description=_("Keep folders in sync with Koofr."))
    parser.add_argument("--version", action="version", version=f"Spola {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help=_("sign in with your Koofr e-mail and an app password"))
    sub.add_parser("logout", help=_("remove the app password from the keyring"))
    sub.add_parser("status", help=_("show the folder pairs"))
    p = sub.add_parser("add", help=_("add a folder pair"))
    p.add_argument("local")
    p.add_argument("remote")
    p = sub.add_parser("remove", help=_("forget a folder pair (no file is deleted)"))
    p.add_argument("pair")
    for name, text in (("preview", _("show what the next sync would do")),
                       ("align", _("first alignment of a new pair (preview, then asks)")),
                       ("repair", _("new alignment after a critical error (preview, then asks)")),
                       ("sync", _("sync a pair now"))):
        p = sub.add_parser(name, help=text)
        p.add_argument("pair")
        if name in ("sync", "preview"):
            p.add_argument("--force", action="store_true",
                           help=_("go past the safety checks (only after looking at the preview)"))
        if name == "sync":
            p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    paths = Paths.from_env()
    cfg = config.load(paths.config_file)
    rclone = find_rclone()
    if rclone is None:
        return _fail(_("rclone was not found."))
    changes_config = args.command in ("login", "logout", "add", "remove", "align", "repair")
    if changes_config and _app_running():
        return _fail(_("Spola is running: use its window, or quit it first."))

    if args.command == "login":
        from . import secrets
        print(_("Create an app password at {url}").format(url=KOOFR_APP_PASSWORDS_URL))
        user = input(_("Koofr e-mail: ")).strip()
        password = getpass.getpass(_("App password: "))
        try:
            creds = Credentials.from_password(user, password, rclone)
            quota = check(rclone, paths, creds)
        except AccountError as e:
            return _fail(str(e))
        secrets.store(user, password)
        cfg.user = user
        try:
            cfg.mount_id = koofr_api.primary_mount_id(user, password)
        except koofr_api.ApiError:
            pass
        config.save(cfg, paths.config_file)
        print(_("Signed in. Free space: {free} GB").format(free=(quota.free or 0) // 1024**3))
        return 0

    if args.command == "logout":
        from . import secrets
        if cfg.user:
            secrets.clear(cfg.user)
        print(_("Signed out."))
        return 0

    if args.command == "status":
        print(_("Account: {user}").format(user=cfg.user or "-"))
        for pair in cfg.pairs:
            state = (_("paused") if pair.paused else _("active")) if pair.initialized else _("first alignment to confirm")
            count = listings.count(paths.workdir(pair.id))
            print(f"\n[{pair.id}] {pair.name}: {state}")
            print(f"  {pair.local}  ⇄  Koofr {pair.remote}")
            if count is not None:
                print("  " + _("{n} entries in the last synced state").format(n=count))
        return 0

    if args.command == "add":
        local = str(Path(args.local).expanduser().resolve())
        remote = config.normalize_remote(args.remote)
        problems = (preflight.check_local_root(local) + preflight.check_overlap(local, remote, cfg)
                    + preflight.check_koofr_desktop(local, Path.home() / ".koofr" / "config.json"))
        if remote == "/":
            problems.append(preflight.Problem("remote-root", _("Choose a folder on Koofr, not the whole Koofr storage."), "/"))
        problems = problems or preflight.scan_names(local, remote, skip=default_excluded)
        if problems:
            for p in problems:
                print(f"- {p.message} {p.path}", file=sys.stderr)
            return 1
        dev, ino = preflight.root_identity(local)
        pair = config.Pair(config.Pair.new_id(), local, remote, root_dev=dev, root_ino=ino)
        cfg.pairs.append(pair)
        config.save(cfg, paths.config_file)
        print(_("Added [{id}]. Next: spola align {id}").format(id=pair.id))
        return 0

    pair = _find_pair(cfg, args.pair)
    if pair is None:
        return _fail(_("No such pair: {key}").format(key=args.pair))

    if args.command == "remove":
        cfg.pairs.remove(pair)
        config.save(cfg, paths.config_file)
        print(_("Removed. No file was deleted."))
        return 0

    creds = _credentials(cfg, rclone)
    if creds is None:
        return _fail(_("Not signed in: run 'spola login'."))

    if args.command == "preview":
        mode = Mode.SYNC if pair.initialized else Mode.RESYNC
        outcome = _run(cfg, pair, paths, rclone, creds, mode, dry_run=True, force=args.force, quiet=True)
        if outcome:
            _print_plan(outcome)
        return 0 if outcome and outcome.result is Result.OK else 1

    if args.command in ("align", "repair"):
        if args.command == "align" and pair.initialized:
            return _fail(_("This pair is already aligned."))
        outcome = _run(cfg, pair, paths, rclone, creds, Mode.RESYNC, dry_run=True, quiet=True)
        if outcome is None or outcome.result is not Result.OK:
            if outcome:
                _print_plan(outcome)
            return 1
        _print_plan(outcome)
        if not _ask("\n" + _("Go ahead?")):
            return 1
        outcome = _run(cfg, pair, paths, rclone, creds, Mode.RESYNC)
        if outcome and outcome.result is Result.OK:
            pair.initialized = True
            config.save(cfg, paths.config_file)
            # writes the .lst-old that --recover needs if a later run is interrupted
            _run(cfg, pair, paths, rclone, creds, Mode.SYNC, quiet=True)
            print(_("Aligned."))
            return 0
        if outcome:
            _print_plan(outcome)
        return 1

    if args.command == "sync":
        if args.force and not args.dry_run:
            preview = _run(cfg, pair, paths, rclone, creds, Mode.SYNC, dry_run=True, force=True, quiet=True)
            if preview is None:
                return 1
            _print_plan(preview)
            if not _ask("\n" + _("Go past the safety checks and do this?")):
                return 1
        outcome = _run(cfg, pair, paths, rclone, creds, Mode.SYNC, dry_run=args.dry_run, force=args.force)
        if outcome is None:
            return 1
        _print_plan(outcome)
        if outcome.result is Result.NEEDS_CONFIRMATION:
            print(_("Look at the preview (spola preview {id} --force) and confirm with: spola sync {id} --force")
                  .format(id=pair.id))
        elif outcome.result is Result.NEEDS_REPAIR:
            print(_("Repair with: spola repair {id}").format(id=pair.id))
        return 0 if outcome.result is Result.OK else 1
    return 1
