"""What a report of a problem needs, in one text: Spola's versions and
paths, the conditions it runs in, every synced folder with its state and
last outcome, the room its logs, listings and backups take. The About
window shows it under Troubleshooting, with a Save button. Nothing in it is
a password; the account's e-mail is shortened."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from . import WINDOWS, __version__, listings
from .paths import Paths, in_flatpak
from .rclone import no_window


def masked(email: str) -> str:
    """"d***@example.com": enough to tell two accounts apart."""
    if "@" not in email:
        return "***" if email else ""
    name, domain = email.split("@", 1)
    return f"{name[:1]}***@{domain}"


def human(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1000
    return f"{size} B"


def rclone_version(rclone: str | None) -> str:
    if not rclone:
        return "not found"
    try:
        out = subprocess.run([rclone, "version"], capture_output=True, text=True, timeout=10, **no_window())
        return (out.stdout.splitlines() or ["?"])[0].strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {e}"


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


def toolkit_versions() -> str:
    try:
        from gi.repository import Adw, GLib, Gtk
        return (f"GLib {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}.{GLib.MICRO_VERSION}, "
                f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
                f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}")
    except Exception:   # the command line, or a GTK too old to say
        return "GTK not loaded"


def summary(engine) -> str:
    """The quick part, read on the main loop: versions, settings, folders."""
    cfg, paths = engine.cfg, engine.paths
    where = "Flatpak" if in_flatpak() else ("Windows" if WINDOWS else "host")
    lines = [
        f"Spola {__version__} ({where}), Python {platform.python_version()}, {platform.platform()}",
        toolkit_versions(),
        f"rclone: {engine.rclone or 'not found'}",
        f"settings: {paths.config_file}",
        f"state: {paths.state}",
        f"backups: {paths.data / 'backup'}",
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
