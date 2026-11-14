"""Spola: keep local folders in two-way sync with Koofr, using rclone bisync.

The package is in four layers, with the imports going one way only
(tests/test_strati.py): core/ works things out, data/ reads the formats
that are not Spola's own, app/ is the services without a window (the
engine, rclone, Koofr, the settings, Windows), gui/ is the windows and the
tray. Everything deciding what happens to the user's files (preflight
checks, rclone command lines, log parsing, outcome classification) is
plain Python without GTK, and is covered by the tests in tests/. The GTK
side (gui/) only displays and triggers.
"""

import sys
from pathlib import Path

__version__ = "0.3.47"

WINDOWS = sys.platform == "win32"
# the package's folder: a module one or two folders down finds the checkout
# (data/, extensions/) from here, not from its own place
PACKAGE_DIR = Path(__file__).resolve().parent

APP_ID = "io.github.daniel_g_carrasco.spola"
APP_NAME = "Spola"
TEXTDOMAIN = "spola"
WEBSITE = "https://spola.neistar.com"
KOOFR_WEB = "https://app.koofr.net"
KOOFR_APP_PASSWORDS_URL = "https://app.koofr.net/app/admin/preferences/password"
