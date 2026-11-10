"""The files the last runs carried across, for the tray menu, as Koofr
Desktop does ("Recently synchronized files", five of them). Kept in the state
folder so the list survives a restart; deleted files are not listed (there is
nothing to open).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

LIMIT = 5
CANDIDATES = 200        # files of one run looked at for the most recent ones


@dataclass(frozen=True)
class RecentFile:
    pair_id: str
    path: str           # relative to the pair's root
    direction: str      # "up" (to Koofr) or "down" (to this computer)
    when: float


class Recent:
    def __init__(self, file: Path, limit: int = LIMIT):
        self.file = file
        self.limit = limit
        self.items: list[RecentFile] = []
        try:
            with open(file, encoding="utf-8") as f:
                self.items = [RecentFile(**d) for d in json.load(f)][:limit]
        except (OSError, ValueError, TypeError):
            self.items = []

    def record(self, pair_id: str, root: str, up: list[str], down: list[str]) -> bool:
        """Add the files of one run; the newest (by modification time) first.
        Returns whether the list changed."""
        candidates = [(p, "up") for p in up[:CANDIDATES]] + [(p, "down") for p in down[:CANDIDATES]]
        if not candidates:
            return False
        found = []
        for path, direction in candidates:
            try:
                mtime = os.stat(os.path.join(root, path)).st_mtime
            except OSError:
                continue
            found.append((mtime, path, direction))
        found.sort(reverse=True)
        now = time.time()
        new = [RecentFile(pair_id, path, direction, now) for _m, path, direction in found[:self.limit]]
        keys = {(f.pair_id, f.path) for f in new}
        self.items = (new + [f for f in self.items if (f.pair_id, f.path) not in keys])[:self.limit]
        self._save()
        return bool(new)

    def forget_pair(self, pair_id: str) -> None:
        before = len(self.items)
        self.items = [f for f in self.items if f.pair_id != pair_id]
        if len(self.items) != before:
            self._save()

    def _save(self) -> None:
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(f) for f in self.items]), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass


def transferring(entries: list[dict], limit: int = LIMIT) -> list[tuple[str, str]]:
    """(relative path, direction) of the files rclone is copying right now,
    from the "transferring" list of its stats."""
    out = []
    for entry in entries[:limit]:
        name = str(entry.get("name") or "")
        if not name:
            continue
        direction = "up" if str(entry.get("srcFs") or "").startswith("local") else "down"
        out.append((name, direction))
    return out
