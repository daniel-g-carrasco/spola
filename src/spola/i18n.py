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


def translation_for(language: str) -> gettext.NullTranslations:
    """The catalogue of one language, whatever the desktop language is
    (English has no catalogue: the source strings are returned)."""
    for directory in _locale_dirs():
        if gettext.find(TEXTDOMAIN, str(directory), languages=[language]):
            return gettext.translation(TEXTDOMAIN, str(directory), languages=[language])
    return gettext.NullTranslations()


def every_translation(message: str) -> set[str]:
    """The message in English and in every language Spola has a catalogue for:
    names written to disk in one language must be recognised in all of them."""
    out = {message}
    for directory in _locale_dirs():
        if not directory.is_dir():
            continue
        for lang_dir in directory.iterdir():
            mo = lang_dir / "LC_MESSAGES" / f"{TEXTDOMAIN}.mo"
            if mo.exists():
                try:
                    with open(mo, "rb") as f:
                        out.add(gettext.GNUTranslations(f).gettext(message))
                except OSError:
                    pass
    return out


def _(message: str) -> str:
    return _translation.gettext(message)


def pgettext(context: str, message: str) -> str:
    """For a word that translates differently in another place ("Syncing"
    as a state, "Syncing" as the title of a group of settings)."""
    return _translation.pgettext(context, message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
