# SPDX-License-Identifier: GPL-3.0-or-later
"""Files written whole or not at all, with the previous version kept.

A temporary file in the same folder, flushed to the disk, then renamed over
the target: a crash, a full disk or a pulled cable leave either the old file
or the new one, never half of one. The file as it was before is kept next
to it as <name>.bak, and reading falls back to it when the file is damaged,
saying so. On Windows the rename is tried again for a moment when it is
refused (replace_patiently).

For a file of the app's own that is updated in place (settings, a project,
a cache). A file made for the user that must never take the place of one
already there goes through files.py.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

REPLACE_WAITS = (0.05, 0.1, 0.2, 0.4)      # seconds between tries: under a second in all


def replace_patiently(tmp: str | os.PathLike, path: str | os.PathLike) -> None:
    """os.replace, tried again for a moment when Windows refuses it: an
    antivirus or the search indexer holds a file that was just written open
    for an instant, and the rename then fails with PermissionError although
    nothing is wrong (seen on 14/11/2026). Any other error, and a refusal
    that lasts, is raised."""
    for wait in REPLACE_WAITS:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(wait)
    os.replace(tmp, path)


def write_json(path: Path, data, keep_backup: bool = True, indent: int = 1, backup_optional: bool = False) -> None:
    """`data` as JSON in `path`, whole or not at all; the file as it was
    stays as <name>.bak (keep_backup). `backup_optional`: a backup that
    cannot be made does not stop the write (the settings: saving matters
    more than the copy of the save before)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        if keep_backup and path.exists():
            backup = path.with_name(path.name + ".bak")
            try:
                # a backup that is the file itself (a hard link left by a save that stopped
                # half way) holds what it must already: Windows refuses to copy a file on itself
                if not (backup.exists() and os.path.samefile(path, backup)):
                    shutil.copy2(path, backup)
            except shutil.SameFileError:
                pass
            except OSError:
                if not backup_optional:
                    raise
        replace_patiently(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_bytes(path: Path, data: bytes) -> None:
    """A file written whole or not at all (a temporary file in the folder,
    then renamed over the target). An old file of the same name is
    replaced, so the caller asks first."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        replace_patiently(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: Path) -> tuple[object, str | None]:
    """The data and, when the file could not be read and the backup was used instead, why."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            raise
        data = json.loads(backup.read_text(encoding="utf-8"))
        return data, f"{path.name} could not be read ({type(error).__name__}: {error}); the previous save was opened"
