"""Spola: keep local folders in two-way sync with Koofr, using rclone bisync.

The package is in four layers, with the imports going one way only
(tests/test_strati.py): core/ works things out, data/ reads the formats
that are not Spola's own, app/ is the services without a window (the
engine, rclone, Koofr, the settings, Windows), gui/ is the windows and the
tray. Everything deciding what happens to the user's files (preflight
checks, rclone command lines, log parsing, outcome classification) is
plain Python without GTK, and is covered by the tests in tests/. The GTK
side (gui/) only displays and triggers.

The constants below are the one place the app's identity lives, with the
names every app of mine has (the template's): the modules shared with the
other apps get them as arguments, from app/services.py.
"""

import sys
from pathlib import Path

__version__ = "0.3.48"

WINDOWS = sys.platform == "win32"
# the package's folder: a module one or two folders down finds the checkout
# (data/, extensions/) from here, not from its own place
PACKAGE_DIR = Path(__file__).resolve().parent

APP_ID = "io.github.daniel_g_carrasco.spola"
APP_NAME = "Spola"
SLUG = "spola"                      # the repository, the binary on Linux, the folders on Linux, a release's files
EXE_NAME = "Spola"                  # the Windows executable and its folders (%APPDATA%\Spola)
TEXTDOMAIN = SLUG
ENV_PREFIX = "SPOLA"                # the developer aids: SPOLA_DEV_REMOTE and the like
GITHUB_REPO = "daniel-g-carrasco/spola"
WEBSITE = "https://spola.neistar.com"
# The releases' signing key (minisign, key id 440F2327E9BC1227); its secret
# half is the CI's MINISIGN_SECRET_KEY (a test checks that the CI verifies
# its signatures with this very key)
PUBLIC_KEY = "RWQnErzpJyMPROWGTtWUJRvxMHh8hdPaJU+NhzmlE7SNKlnpn8tGNYH3"
KOOFR_WEB = "https://app.koofr.net"
KOOFR_APP_PASSWORDS_URL = "https://app.koofr.net/app/admin/preferences/password"
