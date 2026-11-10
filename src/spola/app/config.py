"""Settings and folder pairs, stored as JSON in $XDG_CONFIG_HOME/spola.

The password is never here: it lives in the keyring (see secrets.py).
Writes are atomic (temporary file, fsync, rename) so a crash or a full disk
can never leave a half-written file behind, and the file as it was before
stays next to it (config.json.bak): a damaged file, or one that is gone, is
replaced by that previous save rather than by nothing, and what happened is
told (Config.notices). The file carries a format version: an older format
is migrated on reading; a newer one (a Spola downgraded) is read for what
this version knows and written back whole, the unknown settings kept.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_VERSION = 1
# from one format version to the next, in order: {from_version: step}
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}
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
    # what to do with a file changed on both sides: "" follows the general
    # setting, else "ask", "newer", "both", "local", "koofr" (conflicts.py)
    conflict_rule: str = ""
    # settings of a newer Spola this one does not know, written back as they were
    extra: dict = field(default_factory=dict, compare=False, repr=False)

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
    keep_awake: bool = True         # no sleep while a sync runs (logind, or the desktop's inhibit)
    tray_icon: bool = True          # the notification area icon (off when a shell widget shows the status)
    backup_days: int = 30
    paused_all: bool = False
    nautilus_integration: bool = False
    autostart: bool = True               # start at login, in the background
    conflict_language: str = ""          # "" = desktop language; "en", "it"
    conflict_rule: str = "ask"           # for new conflicts: "ask", "newer", "both"
    upload_limit_kb: int = 0             # KiB/s to Koofr, 0 = no limit
    download_limit_kb: int = 0           # KiB/s from Koofr, 0 = no limit
    check_updates: bool = True           # Windows: a newer release, once a day at start
    last_update_check: str = ""          # ISO date of the last check made on its own
    explorer_status: bool = True         # Windows: the sync status in File Explorer (cloudfiles.py)
    explorer_menu: bool = True           # Windows: "Open on Koofr", "Copy Koofr Share Link" (explorer.py)
    # settings of a newer Spola this one does not know, written back as they were
    extra: dict = field(default_factory=dict, compare=False, repr=False)
    # what load() had to do to read the file (Notice); never saved
    notices: list = field(default_factory=list, compare=False, repr=False)

    @property
    def bwlimit(self) -> str | None:
        """rclone's --bwlimit value, "upload:download", or None."""
        if not self.upload_limit_kb and not self.download_limit_kb:
            return None
        up = f"{int(self.upload_limit_kb)}k" if self.upload_limit_kb else "off"
        down = f"{int(self.download_limit_kb)}k" if self.download_limit_kb else "off"
        return f"{up}:{down}"

    def pair(self, pair_id: str) -> Pair | None:
        return next((p for p in self.pairs if p.id == pair_id), None)

    def to_json(self) -> dict:
        data = asdict(self)
        extra, pairs = data.pop("extra"), data.pop("pairs")
        del data["notices"]
        out = {**extra, **data}
        out["pairs"] = [{**p.pop("extra"), **p} for p in pairs]
        out["version"] = CONFIG_VERSION
        return out

    @classmethod
    def from_json(cls, data) -> "Config":
        """ValueError or TypeError when `data` is not a configuration."""
        if not isinstance(data, dict) or not isinstance(data.get("pairs", []), list):
            raise ValueError("not a Spola configuration")
        own = {"extra", "notices", "version", "pairs"}
        known = {f.name for f in fields(cls)} - own
        pair_known = {f.name for f in fields(Pair)} - own
        cfg = cls(**_typed(cls, {k: v for k, v in data.items() if k in known}))
        cfg.extra = {k: v for k, v in data.items() if k not in known and k not in own}
        for p in data.get("pairs", []):
            if not isinstance(p, dict):
                raise ValueError("not a Spola configuration")
            pair = Pair(**_typed(Pair, {k: v for k, v in p.items() if k in pair_known}))
            pair.extra = {k: v for k, v in p.items() if k not in pair_known}
            cfg.pairs.append(pair)
        return cfg


def _typed(cls, kwargs: dict) -> dict:
    """ValueError when a value has not the type of the field's default (a
    number for a switch, a word for a number): such a file is damaged, and
    the previous save is a better start than a crash later on."""
    for f in fields(cls):
        if f.name not in kwargs or f.default is None or f.default.__class__ not in (bool, int, str):
            continue            # id, local, remote (no default), root_dev/root_ino (None or int)
        value = kwargs[f.name]
        if f.default.__class__ is bool and value.__class__ is not bool:
            raise ValueError(f"{f.name}: {value!r} is not a switch")
        if f.default.__class__ is int and (value.__class__ is not int):
            raise ValueError(f"{f.name}: {value!r} is not a number")
        if f.default.__class__ is str and value.__class__ is not str:
            raise ValueError(f"{f.name}: {value!r} is not text")
    for name in ("id", "local", "remote"):
        if name in kwargs and kwargs[name].__class__ is not str:
            raise ValueError(f"{name}: {kwargs[name]!r} is not text")
    return kwargs


@dataclass(frozen=True)
class Notice:
    """What load() had to do: "recovered" (the file was damaged: the previous
    save was used; `detail` is where the damaged file was put), "fresh" (damaged
    and nothing to recover: Spola starts anew; `detail` as above), "missing"
    (the file was gone: the previous save was used), "newer" (saved by a
    newer Spola; `detail` is its format version)."""
    kind: str
    detail: str = ""
    reason: str = ""


class Damaged(Exception):
    """The file is there and is not a configuration."""


def backup_file(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def migrate(data: dict) -> dict:
    """An older format brought to this one, one version at a time."""
    version = data.get("version", 1)
    if version.__class__ is not int:
        raise ValueError(f"format version {version!r}")
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"no migration from format version {version}")
        data = step(data)
        version += 1
        data["version"] = version
    return data


def _read(path: Path) -> tuple[Config, int]:
    """The configuration in `path` and its format version; FileNotFoundError,
    or Damaged with the reason."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as e:          # bad JSON and bad UTF-8 are ValueErrors
        raise Damaged(f"{type(e).__name__}: {e}") from e
    try:
        if not isinstance(data, dict):
            raise ValueError("not a Spola configuration")
        version = data.get("version", 1)
        return Config.from_json(migrate(data)), version if version.__class__ is int else 1
    except (TypeError, ValueError) as e:
        raise Damaged(f"{type(e).__name__}: {e}") from e


def _set_aside(path: Path) -> Path:
    """The damaged file, kept next to the settings under another name."""
    aside = path.with_name(path.name + time.strftime(".broken-%Y%m%d-%H%M%S"))
    try:
        os.replace(path, aside)
    except OSError:
        pass
    return aside


def load(path: Path) -> Config:
    """The settings, whatever the file's state (see the module's docstring);
    cfg.notices says what had to be done."""
    notices: list[Notice] = []
    version = CONFIG_VERSION
    recovered = False
    try:
        cfg, version = _read(path)
    except FileNotFoundError:
        try:
            cfg, version = _read(backup_file(path))
            notices.append(Notice("missing"))
            recovered = True
        except (FileNotFoundError, Damaged):
            cfg = Config()
    except Damaged as e:
        aside = _set_aside(path)
        try:
            cfg, version = _read(backup_file(path))
            notices.append(Notice("recovered", str(aside), str(e)))
            recovered = True
        except (FileNotFoundError, Damaged):
            cfg = Config()
            notices.append(Notice("fresh", str(aside), str(e)))
    if version > CONFIG_VERSION:
        notices.append(Notice("newer", str(version)))
    if not cfg.instance_id:
        # saved at once: the window and the command line must share it
        cfg.instance_id = secrets.token_hex(4)
    if not path.exists() or recovered:
        try:
            save(cfg, path)
        except OSError:
            pass
    cfg.notices = notices
    return cfg


def save(cfg: Config, path: Path) -> None:
    """Atomic: the new file is complete before it takes the old one's place,
    and the old one stays as the backup (a hard link where the disk allows,
    a copy elsewhere: never a moment without a settings file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg.to_json(), f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        _keep_previous(path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _keep_previous(path: Path) -> None:
    bak = backup_file(path)
    try:
        if not path.exists():
            return
        bak.unlink(missing_ok=True)
        try:
            os.link(path, bak)
        except OSError:
            shutil.copyfile(path, bak)
    except OSError:
        pass                    # a backup that cannot be made must not stop the save


def normalize_remote(path: str) -> str:
    """'/data/work/' and 'data/work' both become '/data/work'; '' is '/'."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError("'..' is not allowed in a Koofr path")
    return "/" + "/".join(parts)
