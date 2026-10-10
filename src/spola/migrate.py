"""Take over the folder pairs of Koofr Desktop.

Koofr Desktop keeps its pairs in ~/.koofr/config.json. Each becomes a Spola
pair with the same local folder and Koofr path, paused if it was paused
there, and still to be aligned: the first alignment runs only after its
preview, pair by pair. Koofr Desktop must be stopped first (and not set to
start at login), or the two engines would sync the same folders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from .config import Config, Pair
from .i18n import _
from .preflight import (Problem, check_local_root, check_overlap, koofr_desktop_active, koofr_desktop_syncs,
                        root_identity, scan_names)
from .rclone import default_excluded

KOOFR_DESKTOP_CONFIG = Path.home() / ".koofr" / "config.json"


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


def import_koofr_desktop(cfg: Config, config_path: Path = KOOFR_DESKTOP_CONFIG,
                         active: bool | None = None, home: str | None = None) -> Report:
    if koofr_desktop_active() if active is None else active:
        raise KoofrDesktopActive(_("Koofr Desktop is running or set to start at login: stop it first."))
    report = Report()
    for s in koofr_desktop_syncs(config_path):
        local = os.path.realpath(s["localPath"])
        try:
            remote = config_mod.normalize_remote(s.get("remotePath") or "/")
        except ValueError as e:
            report.skipped.append((local, str(e)))
            continue
        if any(os.path.realpath(p.local) == local for p in cfg.pairs):
            report.skipped.append((local, _("already in Spola")))
            continue
        problems = check_local_root(local, home) + check_overlap(local, remote, cfg)
        if remote == "/":
            problems.append(Problem("remote-root", _("Choose a folder on Koofr, not the whole Koofr storage."), "/"))
        if problems:
            report.skipped.append((local, problems[0].message))
            continue
        dev, ino = root_identity(local)
        pair = Pair(Pair.new_id(), local, remote, paused=bool(s.get("paused")), root_dev=dev, root_ino=ino)
        cfg.pairs.append(pair)
        report.added.append(Imported(pair, scan_names(local, remote, skip=default_excluded)))
    return report
