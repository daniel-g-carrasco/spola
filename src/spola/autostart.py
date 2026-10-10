"""Start Spola at login, hidden in the tray.

In the Flatpak the Background portal writes and removes the autostart entry
itself. Outside, Spola writes ~/.config/autostart/<app-id>.desktop pointing
at its own launcher, and only ever removes a file it wrote.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_ID, APP_NAME
from . import portal
from .i18n import _
from .paths import in_flatpak

MARKER = "X-Spola-Autostart=true"


def autostart_file(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(environ.get("HOME", os.path.expanduser("~")), ".config")
    return Path(base) / "autostart" / f"{APP_ID}.desktop"


def launcher() -> str:
    """The command that started this Spola (a source checkout's bin/spola,
    or an installed `spola`)."""
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("spola") or "spola"


def desktop_entry(command: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Comment={_('Keep your folders in sync with Koofr')}\n"
        f"Exec={GLib_quote(command)} --background\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        f"{MARKER}\n"
    )


def GLib_quote(command: str) -> str:
    # Exec= uses shell-like quoting: a path with spaces goes in double quotes
    return f'"{command}"' if " " in command else command


def apply(enabled: bool, connection=None, environ=None) -> None:
    if in_flatpak():
        if connection is not None:
            portal.request_background(connection, _("Spola keeps your folders in sync with Koofr in the background."),
                                      enabled, ["spola", "--background"], lambda *_: None)
        return
    path = autostart_file(environ)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry(launcher()), encoding="utf-8")
    elif path.exists():
        try:
            ours = MARKER in path.read_text(encoding="utf-8")
        except OSError:
            ours = False
        if ours:
            path.unlink()
