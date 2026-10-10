"""Settings and folder pairs, stored as JSON in $XDG_CONFIG_HOME/spola.

The password is never here: it lives in the keyring (see secrets.py).
Writes are atomic (temporary file, fsync, rename) so a crash or a full disk
can never leave a half-written file behind.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_VERSION = 1
DEFAULT_MAX_DELETE_PERCENT = 10
DEFAULT_POLL_MINUTES = 10        # full runs when Koofr's activity feed is not available
DEFAULT_FULL_CHECK_MINUTES = 60  # full runs while the activity feed works


@dataclass
class Pair:
    id: str
    local: str            # absolute local folder
    remote: str           # path on Koofr, always starting with "/"
    paused: bool = False
    # False until the first alignment (a confirmed --resync) has run. Before
    # that, no normal bisync is ever started for the pair.
    initialized: bool = False
    # Identity of the local root recorded when the pair was added: if the
    # folder is replaced (unmounted disk, renamed and recreated) the device or
    # inode changes and the pair stops instead of seeing everything deleted.
    root_dev: int | None = None
    root_ino: int | None = None
    max_delete_percent: int = DEFAULT_MAX_DELETE_PERCENT

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(6)

    @property
    def name(self) -> str:
        return Path(self.local).name or self.local


@dataclass
class Config:
    user: str = ""                       # Koofr account e-mail
    mount_id: str = ""                   # Koofr primary mount (web links, activity feed)
    instance_id: str = ""                # this installation, in rclone's user agent
    pairs: list[Pair] = field(default_factory=list)
    poll_minutes: int = DEFAULT_POLL_MINUTES
    full_check_minutes: int = DEFAULT_FULL_CHECK_MINUTES
    pause_on_metered: bool = True
    pause_on_power_saver: bool = True
    backup_days: int = 30
    paused_all: bool = False
    nautilus_integration: bool = False
    autostart: bool = True               # start at login, in the background
    conflict_language: str = ""          # "" = desktop language; "en", "it"

    def pair(self, pair_id: str) -> Pair | None:
        return next((p for p in self.pairs if p.id == pair_id), None)

    def to_json(self) -> dict:
        data = asdict(self)
        data["version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        pair_known = {f.name for f in fields(Pair)}
        kwargs = {k: v for k, v in data.items() if k in known and k != "pairs"}
        cfg = cls(**kwargs)
        cfg.pairs = [Pair(**{k: v for k, v in p.items() if k in pair_known})
                     for p in data.get("pairs", [])]
        return cfg


def load(path: Path) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = Config.from_json(json.load(f))
    except FileNotFoundError:
        cfg = Config()
    if not cfg.instance_id:
        # saved at once: the window and the command line must share it
        cfg.instance_id = secrets.token_hex(4)
        try:
            save(cfg, path)
        except OSError:
            pass
    return cfg


def save(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg.to_json(), f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def normalize_remote(path: str) -> str:
    """'/data/work/' and 'data/work' both become '/data/work'; '' is '/'."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("'..' is not allowed in a Koofr path")
    return "/" + "/".join(parts)
