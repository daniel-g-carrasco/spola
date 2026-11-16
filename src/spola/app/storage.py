"""Where Spola keeps its own files: the folders of app/paths.py (a copy of
the model's), and the files of each pair inside them.

Inside the Flatpak the XDG variables already point to ~/.var/app/<app-id>/,
outside they are the usual ~/.config, ~/.local/state and ~/.local/share. On
Windows, %APPDATA%\\Spola for the settings, %LOCALAPPDATA%\\Spola for the rest.
Nothing here lives in the cache folder: bisync's listings are the memory of
what was in sync, and a cache cleaner must not be able to throw them away.
The folders come from app/services.py (app_paths); a test makes its own
with Paths(config, state, data).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import paths


class Paths(paths.Paths):
    """config: settings and folder pairs; state: bisync listings, filters,
    run logs, locks; data: local backups of replaced and deleted files."""

    @property
    def status_file(self) -> Path:
        # status.py: what the running app is doing, written again every minute
        # (not on Windows), for the Noctalia plugin and the command line
        return self.state / "status.json"

    @property
    def rclone_config(self) -> Path:
        # Deliberately a file that never exists: rclone then keeps its config
        # in memory (the remote comes from environment variables) and never
        # reads the user's own ~/.config/rclone/rclone.conf, which is visible
        # because the app has access to the home folder.
        return self.state / "no-rclone.conf"

    def workdir(self, pair_id: str) -> Path:
        # One workdir per pair, so the listings of a pair are simply the only
        # *.lst files in it, whatever name rclone derives for the session.
        return self.state / "bisync" / pair_id

    def filters_file(self, pair_id: str) -> Path:
        return self.state / "filters" / f"{pair_id}.txt"

    def lock_file(self, pair_id: str) -> Path:
        return self.state / "locks" / f"{pair_id}.lock"

    def log_dir(self, pair_id: str) -> Path:
        return self.state / "logs" / pair_id

    def exclude_file(self, pair_id: str) -> Path:
        return self.state / "exclude" / f"{pair_id}.txt"

    def backup_dir(self, pair_id: str) -> Path:
        return self.data / "backup" / pair_id

    def forget(self, pair_id: str) -> None:
        """A removed pair's state: listings, filters, exclusions, lock and
        logs. Never its backups: what they hold may be the only copy left.
        Only for a pair id (12 hex digits in the app): never a path."""
        if not re.fullmatch(r"[0-9A-Za-z_-]+", pair_id):
            raise ValueError(f"not a pair id: {pair_id!r}")
        for folder in (self.workdir(pair_id), self.log_dir(pair_id)):
            shutil.rmtree(folder, ignore_errors=True)
        filters = self.filters_file(pair_id)
        for file in (filters, filters.with_name(filters.name + ".md5"), self.exclude_file(pair_id),
                     self.lock_file(pair_id)):
            try:
                file.unlink(missing_ok=True)
            except OSError:         # still open (a CLI run): it stays, harmless
                pass
