"""Spola: keep local folders in two-way sync with Koofr, using rclone bisync.

The package is split so that everything deciding what happens to the user's
files (preflight checks, rclone command lines, log parsing, outcome
classification) is plain Python without GTK, and is covered by the tests in
tests/. The GTK side (app, window, tray) only displays and triggers.
"""

import sys

__version__ = "0.3.19"

WINDOWS = sys.platform == "win32"

APP_ID = "io.github.daniel_g_carrasco.spola"
APP_NAME = "Spola"
TEXTDOMAIN = "spola"
WEBSITE = "https://spola.neistar.com"
KOOFR_WEB = "https://app.koofr.net"
KOOFR_APP_PASSWORDS_URL = "https://app.koofr.net/app/admin/preferences/password"
