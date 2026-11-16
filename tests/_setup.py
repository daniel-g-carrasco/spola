"""Shared test helpers: put src/ on sys.path, find fixtures and rclone.
Every test module starts with `import _setup`. The tests copied from the
model's comune/ (their files are in impronte/comune.json) ask it for
PACKAGE and shared(name), so that they never name the package."""

import importlib
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

PACKAGE = "spola"


def shared(name: str):
    """The shared module of that name ("i18n", "app.updates"), as the
    common tests ask for it: spola.<name>."""
    return importlib.import_module(f"{PACKAGE}.{name}")


# No test writes in the user's own folders (tests/_userfolders.py, a copy of
# the model's). They are watched as the environment names them now, before
# anything changes it: a run that writes next to the installed Spola's own
# files (its settings, its listings, its backups) fails at its exit. Then
# the tests get folders of their own: the four XDG variables (config,
# state, data, cache) point into one temporary folder, removed at the exit
# (app/paths.py lets them win on Windows too). The toolkit reads them as
# well, so it shows its own colours and not the user's. Done here, before
# GTK starts. The guard looks at folders, not at Windows' registry: the
# tests that write there use keys of their own (test_autostart.py,
# test_explorer.py, test_cloudfiles.py, test_preflight.py).
import _userfolders  # noqa: E402
from spola import EXE_NAME, SLUG, TEXTDOMAIN, i18n  # noqa: E402

_userfolders.watch(SLUG, EXE_NAME)
_userfolders.isolate()
# Spola's catalogues in this checkout, for the tests that read a language
# (translation_for); the interface itself in the source strings
i18n.setup(TEXTDOMAIN, ROOT, "en")
# the failures said as the app says them (__main__ names the log at start)
from spola.app import log  # noqa: E402

log.setup(SLUG)


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


WINDOWS = sys.platform == "win32"


def case_sensitive(folder) -> bool:
    """Whether names that differ only in case are two files there (not on
    Windows' NTFS, as not on Koofr)."""
    probe = Path(folder) / ".case-probe-a"
    probe.write_text("a")
    try:
        return not (Path(folder) / ".CASE-PROBE-A").exists()
    finally:
        probe.unlink()


def rclone_binary() -> str | None:
    return os.environ.get("SPOLA_RCLONE") or shutil.which("rclone")
