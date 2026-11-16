# SPDX-License-Identifier: GPL-3.0-or-later
"""What a report of a problem needs, in one text: the app's version and
paths, the toolkit's versions (the gui says them: this layer does not load
the toolkit), the settings, the room its files take. The
About window shows it under Troubleshooting, with a Save button. Nothing
in it is a secret: what a user pastes in an issue must be safe to paste.

The module knows no app and no toolkit: name, version and the toolkit's
line are given by the caller; `cfg` is read for theme, language,
check_updates and notices only."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from .errorlog import log_file
from .paths import Paths, in_flatpak


def human(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def tree_size(path: Path) -> tuple[int, int]:
    """(files, bytes) under `path`, links not followed."""
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += st.st_size
    return files, size


def summary(name: str, version: str, cfg, paths: Paths, toolkit: str) -> str:
    """The quick part, read on the main loop: versions, settings, paths.
    `name` and `version` are the app's, `toolkit` is the gui's line about
    its own versions (gui/ui.py)."""
    where = "Flatpak" if in_flatpak() else ("Windows" if sys.platform == "win32" else "host")
    if not in_flatpak():        # the Flatpak runs the package with the runtime's Python: neither
        where += ", frozen" if getattr(sys, "frozen", False) else ", from the sources"
    lines = [
        f"{name} {version} ({where}), Python {platform.python_version()}, {platform.platform()}",
        toolkit,
        f"settings: {paths.config_file}",
        f"state: {paths.state}",
        f"data: {paths.data}",
        f"errors: {log_file(paths.state)}" + ("" if log_file(paths.state).is_file() else " (none)"),
        f"preferences: theme {cfg.theme!r}, language {cfg.language!r}, check updates {cfg.check_updates}",
    ]
    if cfg.notices:
        lines.append("settings file: " + "; ".join(f"{n.kind} {n.detail} {n.reason}".strip() for n in cfg.notices))
    return "\n".join(lines)


def storage(paths: Paths) -> str:
    """The slow part, for a thread: the room taken by the app's folders."""
    lines = []
    for label, folder in (("state", paths.state), ("data", paths.data)):
        if folder.is_dir():
            files, size = tree_size(folder)
            lines.append(f"{label}: {files} files, {human(size)}")
        else:
            lines.append(f"{label}: none")
    lines.append(f"python: {sys.executable}")
    return "\n".join(lines)
