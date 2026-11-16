# SPDX-License-Identifier: GPL-3.0-or-later
"""GNU gettext set-up. Catalogues live in po/ and are compiled by
tools/compile_po.py (to locale/ in a checkout, to /app/share/locale in the
Flatpak, to share/locale in the Windows build). Source strings are English;
Italian is the first translation.

The module knows no app: setup() is told the catalogue's name and where a
source checkout is. It is the one module every layer may import.
"""

from __future__ import annotations

import gettext
import os
import sys
from pathlib import Path

_translation: gettext.NullTranslations = gettext.NullTranslations()
_domain = ""                        # the catalogue's name, given to setup()
_checkout: Path | None = None       # the root of a source checkout (its locale/), given to setup()


def _locale_dirs():
    if _checkout is not None:
        yield _checkout / "locale"            # source checkout
    if getattr(sys, "frozen", False):          # the Windows build
        yield Path(getattr(sys, "_MEIPASS", "")) / "share" / "locale"
    for prefix in ("/app", "/usr/local", "/usr"):
        yield Path(prefix) / "share" / "locale"


def _system_languages() -> list[str]:
    """Windows sets none of the variables: the language of its interface
    ("it_IT"), as GTK reads it for its own strings."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        import locale
        name = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except (AttributeError, OSError):
        return []
    return [name] if name else []


def setup(domain: str, checkout: Path | None = None, language: str = "") -> None:
    """At start: the catalogue is named `domain`; `checkout` is the root of
    a run from the sources (its locale/ is looked at first). Then the
    language, as use() takes it."""
    global _domain, _checkout
    _domain, _checkout = domain, checkout
    use(language)


def use(language: str = "") -> None:
    """Load the catalogue for the desktop language, or for `language` (a
    preference: "it", "en"), if we have one."""
    global _translation
    languages = [language] if language else []
    if not languages:
        for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
            value = os.environ.get(var)
            if value:
                languages = [part.split(".")[0] for part in value.split(":") if part]
                break
        else:
            languages = _system_languages()
    _translation = gettext.NullTranslations()
    for directory in _locale_dirs():
        if gettext.find(_domain, str(directory), languages=languages or None):
            _translation = gettext.translation(_domain, str(directory), languages=languages or None)
            return


def translation_for(language: str) -> gettext.NullTranslations:
    """The catalogue of one language, whatever the desktop language is
    (English has no catalogue: the source strings are returned)."""
    for directory in _locale_dirs():
        if gettext.find(_domain, str(directory), languages=[language]):
            return gettext.translation(_domain, str(directory), languages=[language])
    return gettext.NullTranslations()


def _(message: str) -> str:
    return _translation.gettext(message)


def N_(message: str) -> str:
    """A string marked for the catalogue and translated later, where it is
    shown (a table of names written before the language is known)."""
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return _translation.pgettext(context, message)


def language() -> str:
    """The language of the interface: that of the catalogue loaded
    ("it"), English without one."""
    return (_translation.info().get("language") or "en").split("_")[0]


def share_with_children(variable: str) -> None:
    """This process's language given to the processes it starts, through
    the environment they inherit, in `variable` (the app's own name, as
    "<PREFIX>_WINDOW_LANGUAGE"): their messages speak it too."""
    os.environ[variable] = language()


def inherited(variable: str, default: str = "") -> str:
    """In a child process: the language its parent shared in `variable`
    (share_with_children), else `default` (a child started on its own, by
    a test or a tool; "" is the desktop's language)."""
    return os.environ.get(variable) or default


def setup_as_child(domain: str, variable: str, checkout: Path | None = None, default: str = "") -> None:
    """setup() in a child process, in the language its parent shared."""
    setup(domain, checkout, inherited(variable, default))


def number(value: float, decimals: int | None = 2, sign: bool = False, grouping: bool = False) -> str:
    """A measure as the language of the interface writes it: 3,58 in
    Italian, 3.58 in English; with grouping 12.345,6 and 12,345.6; with
    sign +3,58. `decimals` None: as many as the value has, up to six
    figures (Python's "g": 450, 13,5, 0,045), for a value read from a file
    and said back as it was written."""
    shape = "g" if decimals is None else f".{decimals}f"
    text = f"{value:{'+' if sign else ''}{',' if grouping else ''}{shape}}"
    return text.translate(str.maketrans(",.", ".,")) if language() == "it" else text


def count(n: int) -> str:
    """A number as the locale writes it (1.200 in Italian, 1,200 in
    English), for the texts that show counts."""
    try:
        return f"{n:n}"
    except ValueError:
        return str(n)
