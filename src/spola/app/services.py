"""Spola's own instances of the services that know no app.

paths, log, errorlog, updates, minisign, win32, wintoast, diagnostics (and
i18n at the root, atomic in data) are the shared modules: copies, byte for
byte, of the model's comune/ (app-template), listed with their
fingerprints in impronte/comune.json and held to them by
tests/test_impronte.py. They are never edited here: a change goes to the
model and comes back with its tools/impronte.py --apply. They take a name,
an id, a version as arguments, so that the same files serve every app of
mine, GTK or Qt. This is where they meet the names in the package's
__init__ (tests/test_services.py)."""

from __future__ import annotations

from spola import EXE_NAME, GITHUB_REPO, PUBLIC_KEY, SLUG, __version__

from .storage import Paths


def app_paths(environ=None) -> Paths:
    """Where Spola keeps its files (app/storage.py: the shared folders, and
    the files of each pair in them)."""
    return Paths.from_env(SLUG, EXE_NAME, environ)


def project():
    """Whose releases an update comes from (updates.py is loaded only when
    asked: on Linux the Flatpak brings the updates)."""
    from .updates import Project
    return Project(repo=GITHUB_REPO, slug=SLUG, exe_name=EXE_NAME, version=__version__, public_key=PUBLIC_KEY)
