"""Where Spola keeps its own files.

Inside the Flatpak the XDG variables already point to ~/.var/app/<app-id>/,
outside they are the usual ~/.config, ~/.local/state and ~/.local/share. On
Windows, %APPDATA%\\Spola for the settings, %LOCALAPPDATA%\\Spola for the rest.
Nothing here lives in ~/.cache: bisync's listings are the memory of what was
in sync, and a cache cleaner must not be able to throw them away.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


def _xdg(var: str, fallback: str, environ) -> Path:
    value = environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path(environ.get("HOME", os.path.expanduser("~"))) / fallback


@dataclass(frozen=True)
class Paths:
    config: Path   # settings and folder pairs
    state: Path    # bisync listings, filters, run logs, locks
    data: Path     # local backups of replaced and deleted files

    @classmethod
    def from_env(cls, environ=None) -> "Paths":
        environ = os.environ if environ is None else environ
        if os.name == "nt" and not environ.get("XDG_CONFIG_HOME"):
            # Windows: the settings roam with the profile, the listings and
            # backups belong to this computer (the XDG variables still win,
            # for the tests)
            roaming = Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
            local = Path(environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return cls(config=roaming / "Spola", state=local / "Spola" / "state", data=local / "Spola" / "data")
        return cls(
            config=_xdg("XDG_CONFIG_HOME", ".config", environ) / "spola",
            state=_xdg("XDG_STATE_HOME", ".local/state", environ) / "spola",
            data=_xdg("XDG_DATA_HOME", ".local/share", environ) / "spola",
        )

    @property
    def config_file(self) -> Path:
        return self.config / "config.json"

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


def nearest_folder(path: str) -> str:
    """The closest folder above `path` that exists, for a file that is gone."""
    folder = os.path.dirname(path)
    while not os.path.isdir(folder) and folder != os.path.dirname(folder):
        folder = os.path.dirname(folder)
    return folder


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")
