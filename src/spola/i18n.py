"""GNU gettext set-up. Catalogues live in po/ and are compiled by
tools/compile_po.py (to locale/ in a checkout, to /app/share/locale in the
Flatpak). Source strings are English; Italian is the first translation."""

from __future__ import annotations

import gettext
import os
from pathlib import Path

from . import TEXTDOMAIN

_translation: gettext.NullTranslations = gettext.NullTranslations()


def _locale_dirs():
    root = Path(__file__).resolve().parent.parent.parent
    yield root / "locale"                     # source checkout
    for prefix in ("/app", "/usr/local", "/usr"):
        yield Path(prefix) / "share" / "locale"


def setup() -> None:
    """Load the catalogue for the desktop language, if we have one."""
    global _translation
    languages = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            languages = [part.split(".")[0] for part in value.split(":") if part]
            break
    for directory in _locale_dirs():
        if gettext.find(TEXTDOMAIN, str(directory), languages=languages or None):
            _translation = gettext.translation(TEXTDOMAIN, str(directory), languages=languages or None)
            return


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
