"""Verifying that both sides hold the same files, on demand: `rclone check`
between the synced folder and its Koofr folder, with the pair's filters
and exclusions, by size and checksum (Koofr keeps an MD5 for every file;
this computer reads its files to compute theirs, so a large folder takes
a while). Nothing is changed on either side. The report ("--combined") is
a file in the pair's workdir, one line per file: "= path" the same on
both sides, "+ path" only here, "- path" only on Koofr, "* path"
different, "! path" could not be compared. The engine runs it as a
preview of its own kind (Mode.CHECK): never alongside a sync of the same
pair, cancellable, its outcome kept a while for the dialog."""

from __future__ import annotations

from pathlib import Path

from .i18n import _
from .rclone import NETWORK_HINTS, BisyncRequest, Event, Kind, Outcome, Plan, Result

REPORT = "verify.txt"


def check_args(rclone: str, req: BisyncRequest, report: Path) -> list[str]:
    args = [
        rclone, "check", req.path1.rstrip("/") + "/", req.path2,
        "--config", str(req.rclone_config),
        "--filter-from", str(req.filters_file),
        "--combined", str(report),
        "--checkers", "16",
        "--skip-links",
        "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE",
        "--color", "NEVER",
    ]
    if req.user_agent:
        args += ["--user-agent", req.user_agent]
    if req.exclude_from:
        args += ["--exclude-from", str(req.exclude_from)]
    return args


def summarize_check(exit_code: int, events: list[Event], report: Path, cancelled: bool = False) -> Outcome:
    """The report as an Outcome: only here in `plan.upload`, only on Koofr
    in `plan.download`, different in `plan.conflicts`, not comparable in
    `plan.error_paths`, the identical ones counted in `identical`."""
    plan = Plan()
    identical = 0
    try:
        text = report.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        text = ""
    for line in text.splitlines():
        if len(line) < 3 or line[1] != " ":
            continue
        mark, path = line[0], line[2:]
        if mark == "=":
            identical += 1
        elif mark == "+":
            plan.upload.append(path)
        elif mark == "-":
            plan.download.append(path)
        elif mark == "*":
            plan.conflicts.append(path)
        elif mark == "!":
            plan.error_paths.append(path)
    stats = next((ev.stats for ev in reversed(events) if ev.kind is Kind.STATS), None)
    errors = [ev.message for ev in events if ev.level == "error" and ev.message]
    plan.errors = errors
    network = any(hint in message.lower() for message in errors for hint in NETWORK_HINTS)
    differences = len(plan.upload) + len(plan.download) + len(plan.conflicts) + len(plan.error_paths)
    if cancelled:
        result, message = Result.CANCELLED, "stopped"
    elif exit_code == 0 or (exit_code == 1 and (differences or text)):
        # 1 is rclone's "differences found"; with a report, that is the answer
        result, message = Result.OK, ""
    else:
        result = Result.FAILED
        message = _("Koofr could not be reached.") if network else (errors[0] if errors else
                                                                   _("The verification stopped with an error."))
    for name in ("upload", "download", "conflicts", "error_paths"):
        setattr(plan, name, sorted(set(getattr(plan, name)), key=lambda s: (s.count("/"), s.casefold())))
    return Outcome(result, exit_code, message, plan, stats, network, identical=identical)
