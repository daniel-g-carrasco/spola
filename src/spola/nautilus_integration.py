"""Install the Files (Nautilus) extension and its emblems for this user.

nautilus-python loads extensions from ~/.local/share/nautilus-python/extensions
(and from the system directories); the emblem icons go to the user's hicolor
theme. Both live on the host, outside any Flatpak, which is why the app
copies them into the home folder instead of shipping them in its own tree.
Files must be restarted afterwards (nautilus -q) to load or unload them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

EXTENSION_NAME = "spola-nautilus.py"
EMBLEMS = ("synced", "syncing", "pending", "error", "conflict", "excluded")
MARKER = "Spola in Files (Nautilus)"


def source_dir() -> Path:
    """Where the app's copy of the extension and emblems lives: the source
    checkout, or /app/share/spola in the Flatpak."""
    for candidate in (Path(__file__).resolve().parent.parent.parent, Path("/app/share/spola")):
        if (candidate / "nautilus" / EXTENSION_NAME).exists():
            return candidate
    raise FileNotFoundError(EXTENSION_NAME)


def data_home(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_DATA_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(environ.get("HOME", os.path.expanduser("~")), ".local", "share")
    return Path(base)


def extension_path(environ=None) -> Path:
    return data_home(environ) / "nautilus-python" / "extensions" / EXTENSION_NAME


def emblem_paths(environ=None) -> list[Path]:
    folder = data_home(environ) / "icons" / "hicolor" / "scalable" / "emblems"
    return [folder / f"emblem-spola-{state}-symbolic.svg" for state in EMBLEMS]


def catalogue_paths(environ=None) -> list[Path]:
    """Spola's translations for the menu entries, per language."""
    return [data_home(environ) / "locale" / lang / "LC_MESSAGES" / "spola.mo" for lang in _languages()]


def _languages() -> list[str]:
    for base in (source_dir() / "locale", Path("/app/share/locale")):
        if base.is_dir():
            return sorted(p.name for p in base.iterdir() if (p / "LC_MESSAGES" / "spola.mo").exists())
    return []


def installed(environ=None) -> bool:
    return extension_path(environ).exists()


def install(environ=None) -> list[Path]:
    src = source_dir()
    written = []
    target = extension_path(environ)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / "nautilus" / EXTENSION_NAME, target)
    written.append(target)
    for path in emblem_paths(environ):
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "data" / "icons" / "hicolor" / "scalable" / "emblems" / path.name, path)
        written.append(path)
    for path in catalogue_paths(environ):
        lang = path.parent.parent.name
        for base in (src / "locale", Path("/app/share/locale")):
            mo = base / lang / "LC_MESSAGES" / "spola.mo"
            if mo.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(mo, path)
                written.append(path)
                break
    _refresh_icon_cache(environ)
    return written


def uninstall(environ=None) -> list[Path]:
    removed = []
    for path in [extension_path(environ), *emblem_paths(environ), *catalogue_paths(environ)]:
        if path.exists():
            if path.name == EXTENSION_NAME and MARKER not in path.read_text(encoding="utf-8", errors="replace"):
                continue      # not ours
            path.unlink()
            removed.append(path)
    _refresh_icon_cache(environ)
    return removed


def _refresh_icon_cache(environ=None) -> None:
    """Only if a cache exists there: without one GTK reads the folders."""
    theme = data_home(environ) / "icons" / "hicolor"
    if (theme / "icon-theme.cache").exists():
        for tool in ("gtk4-update-icon-cache", "gtk-update-icon-cache"):
            if shutil.which(tool):
                subprocess.run([tool, "-f", "-t", str(theme)], capture_output=True, timeout=60)
                break


def files_running() -> bool:
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    with open(f"/proc/{entry.name}/comm", encoding="utf-8", errors="replace") as f:
                        if f.read().strip() == "nautilus":
                            return True
                except OSError:
                    continue
    except OSError:
        pass
    return False
