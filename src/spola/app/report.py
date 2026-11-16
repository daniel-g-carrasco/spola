"""What a report of a problem needs, in one text: Spola's versions and
paths, the toolkit's (given by gui/: this layer does not load it), the conditions it runs in, every synced folder with its state and
last outcome, the room its logs, listings and backups take. The About
window shows it under Troubleshooting, with a Save button. Nothing in it is
a password; the account's e-mail is shortened."""

from __future__ import annotations

import platform
import subprocess
import sys
import time

from spola import WINDOWS, __version__
from spola.data import listings
from . import errorlog
from .diagnostics import human, tree_size
from .paths import in_flatpak
from .storage import Paths
from .rclone import no_window


def masked(email: str) -> str:
    """"d***@example.com": enough to tell two accounts apart."""
    if "@" not in email:
        return "***" if email else ""
    name, domain = email.split("@", 1)
    return f"{name[:1]}***@{domain}"


def rclone_version(rclone: str | None) -> str:
    if not rclone:
        return "not found"
    try:
        out = subprocess.run([rclone, "version"], capture_output=True, text=True, timeout=10, **no_window())
        return (out.stdout.splitlines() or ["?"])[0].strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"


def summary(engine, toolkit: str = "GTK not loaded") -> str:
    """The quick part, read on the main loop: versions, settings, folders.
    `toolkit` is the gui's line about its own versions (gui/ui.py)."""
    cfg, paths = engine.cfg, engine.paths
    where = "Flatpak" if in_flatpak() else ("Windows" if WINDOWS else "host")
    lines = [
        f"Spola {__version__} ({where}), Python {platform.python_version()}, {platform.platform()}",
        toolkit,
        f"rclone: {engine.rclone or 'not found'}",
        f"settings: {paths.config_file}",
        f"state: {paths.state}",
        f"backups: {paths.data / 'backup'}",
        f"errors: {errorlog.log_file(paths.state)}" + ("" if errorlog.log_file(paths.state).is_file() else " (none)"),
        f"account: {masked(cfg.user) or 'none'}, signed in: {'yes' if engine.creds is not None else 'no'}, "
        f"mount: {'known' if cfg.mount_id else 'unknown'}",
    ]
    space = engine.space
    if space is not None:
        lines.append(f"space on Koofr: {human(space.used)} of {human(space.total)}, {human(space.free)} free")
    network = getattr(engine, "network", None)
    if network is not None:
        available = engine.network_available() if hasattr(engine, "network_available") \
            else network.get_network_available()
        metered = engine.network_metered() if hasattr(engine, "network_metered") else network.get_network_metered()
        lines.append(f"network: available {available} (GLib {network.get_network_available()}), metered {metered}")
    power = getattr(engine, "power", None)
    if power is not None:
        lines.append(f"power saver: {power.get_power_saver_enabled()}")
    blocked = engine.blocked_reason() if hasattr(engine, "blocked_reason") else ""
    lines.append(f"all paused: {cfg.paused_all}, blocked: {blocked or 'no'}")
    lines.append(f"preferences: poll {cfg.poll_minutes} min, full check {cfg.full_check_minutes} min, "
                 f"backups {cfg.backup_days} days, limits {cfg.bwlimit or 'none'}, keep awake {cfg.keep_awake}, "
                 f"pause on metered {cfg.pause_on_metered}, on power saver {cfg.pause_on_power_saver}, "
                 f"conflicts {cfg.conflict_rule!r}, autostart {cfg.autostart}, instance {cfg.instance_id}")
    lines.append(f"folders: {len(cfg.pairs)}")
    for pair in cfg.pairs:
        rt = engine.runtime.get(pair.id)
        lines.append(f"  [{pair.id}] {pair.local} <-> {pair.remote}")
        lines.append(f"    initialized {pair.initialized}, paused {pair.paused}, max delete {pair.max_delete_percent}%, "
                     f"conflicts {pair.conflict_rule or 'default'!s}, root {pair.root_dev}:{pair.root_ino}")
        if rt is None:
            continue
        status = getattr(rt.status, "name", str(rt.status))
        lines.append(f"    status: {status} {rt.message!r}" + (f", safety {rt.safety}" if rt.safety else "")
                     + (f", gone {rt.gone}" if rt.gone else "") + (f", retries {rt.retries}" if rt.retries else "")
                     + (f", could not start: {rt.last_failure!r}" if rt.last_failure else ""))
        out = rt.last_outcome
        if out is not None:
            result = getattr(out.result, "name", str(out.result))
            at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rt.last_sync)) if getattr(rt, "last_sync", 0) \
                else "?"
            lines.append(f"    last run: {result}, exit {out.exit_code}, {out.message!r}, last success {at}")
        if rt.conflicts:
            lines.append(f"    conflicts: {len(rt.conflicts)}")
        if rt.refused:
            lines.append(f"    files left out: {len(rt.refused)}")
    return "\n".join(lines)


def storage(paths: Paths, rclone: str | None, pair_ids=()) -> str:
    """The slow part, for a thread: rclone's version, the entries of each
    folder's listings (a read of both, tens of MB for a big folder) and the
    room taken."""
    lines = [f"rclone version: {rclone_version(rclone)}"]
    for pair_id in pair_ids:
        try:
            counts = listings.counts(paths.workdir(pair_id))
        except OSError as e:
            lines.append(f"[{pair_id}] listings: {e}")
            continue
        lines.append(f"[{pair_id}] listings: {counts[0]} entries here, {counts[1]} on Koofr" if counts
                     else f"[{pair_id}] listings: none")
    for label, folder in (("logs", paths.state / "logs"), ("listings", paths.state / "bisync"),
                          ("backups", paths.data / "backup")):
        if folder.is_dir():
            files, size = tree_size(folder)
            lines.append(f"{label}: {files} files, {human(size)}")
        else:
            lines.append(f"{label}: none")
    lines.append(f"python: {sys.executable}")
    return "\n".join(lines)
