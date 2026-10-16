"""Take over the folder pairs of Koofr Desktop.

Koofr Desktop keeps its pairs in its config.json (~/.koofr, on Windows
%APPDATA%/koofr). Each becomes a Spola pair with the same local folder and
Koofr path, paused if it was paused there, and still to be aligned: the
first alignment runs only after its preview, pair by pair. Koofr Desktop
must be stopped first (and not set to start at login), or the two engines
would sync the same folders. A pair on another mount than the account's own
storage (a shared folder, a connected service) is left out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from .config import Config, Pair
from .i18n import _
from .preflight import (Problem, check_local_root, check_overlap, claim_root, koofr_desktop_active,
                        koofr_desktop_config, koofr_desktop_syncs, root_identity, scan_names)
from .rclone import default_excluded


@dataclass
class Imported:
    pair: Pair
    warnings: list[Problem] = field(default_factory=list)   # names Koofr refuses: left out of runs


@dataclass
class Report:
    added: list[Imported] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)    # (local, why)


class KoofrDesktopActive(Exception):
    pass


@dataclass
class Candidate:
    """One of Koofr Desktop's pairs, as it would become Spola's."""
    local: str
    remote: str             # normalised; as Koofr Desktop has it when it cannot be
    paused: bool
    problem: str = ""       # why it cannot be taken over ("" when it can)


def problem_of(cfg: Config, local: str, remote: str, home: str | None = None) -> str:
    """Why this pair cannot join Spola's pairs as they are now ("" if it can)."""
    if any(os.path.realpath(p.local) == local for p in cfg.pairs):
        return _("already in Spola")
    problems = check_local_root(local, home) + check_overlap(local, remote, cfg)
    if remote == "/":
        problems.append(Problem("remote-root", _("Choose a folder on Koofr, not the whole Koofr storage."), "/"))
    return problems[0].message if problems else ""


def candidates(cfg: Config, config_path: Path | None = None, home: str | None = None) -> list[Candidate]:
    """Koofr Desktop's pairs, each with what keeps it out, if anything. Of
    two that overlap, the first in Koofr Desktop's order can be taken."""
    found: list[Candidate] = []
    scratch = Config(pairs=list(cfg.pairs), mount_id=cfg.mount_id)      # with the ones taken so far
    for s in koofr_desktop_syncs(config_path or koofr_desktop_config()):
        local = os.path.realpath(s["localPath"])
        paused = bool(s.get("paused"))
        if s.get("mountId") and s["mountId"] != cfg.mount_id:
            # a folder shared with this account, or a service connected to
            # Koofr: Spola syncs with the account's own storage only. Not
            # knowing which one is the account's own, nothing is taken.
            found.append(Candidate(local, s.get("remotePath") or "/", paused,
                                   _("not in your own Koofr storage (a shared folder or a connected service)")
                                   if cfg.mount_id else
                                   _("Spola could not ask Koofr which storage is your own: try again when "
                                     "Koofr can be reached")))
            continue
        try:
            remote = config_mod.normalize_remote(s.get("remotePath") or "/")
        except ValueError as e:
            found.append(Candidate(local, s.get("remotePath") or "/", paused, str(e)))
            continue
        candidate = Candidate(local, remote, paused, problem_of(scratch, local, remote, home))
        if not candidate.problem:
            scratch.pairs.append(Pair("candidate", local, remote))
        found.append(candidate)
    return found


def new_pair(candidate: Candidate) -> Pair:
    dev, ino = root_identity(candidate.local)
    pair = Pair(Pair.new_id(), candidate.local, candidate.remote, paused=candidate.paused, root_dev=dev, root_ino=ino)
    claim_root(pair)
    return pair


def import_koofr_desktop(cfg: Config, config_path: Path | None = None,
                         active: bool | None = None, home: str | None = None) -> Report:
    if koofr_desktop_active() if active is None else active:
        raise KoofrDesktopActive(_("Koofr Desktop is running or set to start at login: stop it first."))
    report = Report()
    for candidate in candidates(cfg, config_path, home):
        if candidate.problem:
            report.skipped.append((candidate.local, candidate.problem))
            continue
        pair = new_pair(candidate)
        cfg.pairs.append(pair)
        report.added.append(Imported(pair, scan_names(pair.local, pair.remote, skip=default_excluded)))
    return report
