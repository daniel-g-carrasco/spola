"""Spola in the file managers of this user: sync emblems and a context menu.

  Files (Nautilus), Nemo, Caja   one Python extension, extensions/spola-files.py,
                                 copied into each one's extension folder with the
                                 file manager's name filled in (they share the
                                 extension API: Nemo and Caja are Nautilus forks);
                                 needs nautilus-python, nemo-python, python3-caja
  Dolphin                        a service menu (the context menu); its overlay
                                 icons need a compiled plugin, extensions/dolphin/,
                                 which an app cannot install for the user
  Thunar                         custom actions added to its uca.xml (Thunar has no
                                 emblem API for extensions)

Everything goes into the host's home, also from the Flatpak, and is marked as
Spola's, so uninstall removes only what Spola wrote. A file manager is set up
when its folders show it has been used (Files always, as before): a new one
is picked up the next time Spola starts. They must be restarted to see the
extension (nautilus -q, nemo -q, caja -q).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import APP_ID
from .i18n import translation_for

EXTENSION_SOURCE = "spola-files.py"
EMBLEMS = ("synced", "syncing", "pending", "error", "conflict", "excluded")
MARKER = "Spola in Files (Nautilus)"          # in the extension's text, also in the copies
BUS = APP_ID
OBJECT_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
INTERFACE = f"{APP_ID}.Sync1"

# name: (python bindings folder, file name of the copy, FILE_MANAGER value)
PYTHON_TARGETS = {
    "nautilus": ("nautilus-python", "spola-nautilus.py", "Nautilus"),
    "nemo": ("nemo-python", "spola-nemo.py", "Nemo"),
    "caja": ("caja-python", "spola-caja.py", "Caja"),
}
SERVICE_MENU = "io.github.daniel_g_carrasco.spola.desktop"
UCA_PREFIX = "spola-"                 # unique-id of Spola's Thunar actions
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

def N_(text: str) -> str:
    """Marks a string for translation; it is translated into every language
    when the menus are written (they carry all of them)."""
    return text


# the entries of the menu-only file managers: (id, D-Bus method, English label).
# These menus cannot ask Spola anything before they show: the previous
# versions are always there, and Spola's window says so when there is none
# (ShowVersions on a folder opens what was deleted in it).
ACTIONS = (
    ("sync-now", "SyncNow", N_("Sync Now with Koofr")),
    ("open-on-koofr", "OpenOnKoofr", N_("Open on Koofr")),
    ("share-link", "ShowShareLink", N_("Koofr Share Link…")),
    ("versions", "ShowVersions", N_("Previous Versions…")),
    ("deleted", "ShowDeleted", N_("Recently Deleted Files…")),
)
# Thunar can tell files from folders: what each entry applies to
FILE_KINDS = ("audio-files", "image-files", "other-files", "text-files", "video-files")
UCA_KINDS = {"versions": FILE_KINDS, "deleted": ("directories",)}


def source_dir() -> Path:
    """Where the app's copy of the extensions and emblems lives: the source
    checkout, or /app/share/spola in the Flatpak."""
    for candidate in (Path(__file__).resolve().parent.parent.parent, Path("/app/share/spola")):
        if (candidate / "extensions" / EXTENSION_SOURCE).exists():
            return candidate
    raise FileNotFoundError(EXTENSION_SOURCE)


def _host_dir(environ, var: str, fallback: str) -> Path:
    """An XDG folder of the host. Inside the Flatpak the XDG variables are the
    sandbox's own (~/.var/app/<app-id>/...): the host's are in HOST_XDG_*
    when the host sets them, else the default under the home folder."""
    environ = os.environ if environ is None else environ
    base = environ.get(("HOST_" + var) if environ.get("FLATPAK_ID") else var)
    if not base or not os.path.isabs(base):
        base = os.path.join(environ.get("HOME", os.path.expanduser("~")), fallback)
    return Path(base)


def data_home(environ=None) -> Path:
    return _host_dir(environ, "XDG_DATA_HOME", ".local/share")


def config_home(environ=None) -> Path:
    return _host_dir(environ, "XDG_CONFIG_HOME", ".config")


# -- which file managers ---------------------------------------------------------------

def detected(environ=None) -> list[str]:
    """The file managers this user has run (they leave folders behind on the
    first start); Files is always included."""
    data, config = data_home(environ), config_home(environ)
    found = ["nautilus"]
    if (config / "nemo").is_dir() or (data / "nemo").is_dir():
        found.append("nemo")
    if (config / "caja").is_dir():
        found.append("caja")
    if (config / "dolphinrc").exists() or (data / "dolphin").is_dir():
        found.append("dolphin")
    if _uca_file(environ).exists():
        found.append("thunar")
    return found


# -- paths -----------------------------------------------------------------------------

def extension_path(name: str = "nautilus", environ=None) -> Path:
    folder, file_name, _fm = PYTHON_TARGETS[name]
    return data_home(environ) / folder / "extensions" / file_name


def emblem_paths(environ=None) -> list[Path]:
    folder = data_home(environ) / "icons" / "hicolor" / "scalable" / "emblems"
    return [folder / f"emblem-spola-{state}-symbolic.svg" for state in EMBLEMS]


def service_menu_path(environ=None) -> Path:
    return data_home(environ) / "kio" / "servicemenus" / SERVICE_MENU


def _uca_file(environ=None) -> Path:
    return config_home(environ) / "Thunar" / "uca.xml"


def catalogue_paths(environ=None) -> list[Path]:
    """Spola's translations for the menu entries, per language."""
    return [data_home(environ) / "locale" / lang / "LC_MESSAGES" / "spola.mo" for lang in _languages()]


def _languages() -> list[str]:
    for base in (source_dir() / "locale", Path("/app/share/locale")):
        if base.is_dir():
            return sorted(p.name for p in base.iterdir() if (p / "LC_MESSAGES" / "spola.mo").exists())
    return []


def installed(environ=None) -> bool:
    return any(extension_path(name, environ).exists() for name in PYTHON_TARGETS) \
        or service_menu_path(environ).exists() or bool(_uca_ours(environ))


# -- the pieces --------------------------------------------------------------------------

def extension_text(file_manager: str) -> str:
    """The extension with FILE_MANAGER set for one file manager."""
    text = (source_dir() / "extensions" / EXTENSION_SOURCE).read_text(encoding="utf-8")
    line = 'FILE_MANAGER = "Nautilus"'
    if text.count(line) != 1:
        raise ValueError("FILE_MANAGER line not found in the extension")
    return text.replace(line, f'FILE_MANAGER = "{file_manager}"')


def _command(method: str, placeholder: str) -> str:
    return f"gdbus call --session --dest {BUS} --object-path {OBJECT_PATH} --method {INTERFACE}.{method} {placeholder}"


def _translations(label: str) -> dict[str, str]:
    out = {}
    for lang in _languages():
        text = translation_for(lang).gettext(label)
        if text != label:
            out[lang] = text
    return out


def service_menu_text() -> str:
    """Dolphin's context menu (KIO service menu): a "Koofr" submenu for every
    file and folder; Spola answers with a notification for those outside its
    folders, since a service menu cannot tell them apart."""
    lines = ["[Desktop Entry]", "Type=Service", "MimeType=all/all;",
             "Actions=" + ";".join(a for a, _m, _l in ACTIONS) + ";",
             "X-KDE-Submenu=Koofr", f"X-Spola-Marker={MARKER}", ""]
    for action, method, label in ACTIONS:
        lines += [f"[Desktop Action {action}]", f"Name={label}"]
        lines += [f"Name[{lang}]={text}" for lang, text in _translations(label).items()]
        lines += [f"Icon={APP_ID}-symbolic", f"Exec={_command(method, '%f')}", ""]
    return "\n".join(lines)


def _uca_ours(environ=None) -> list[ET.Element]:
    path = _uca_file(environ)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    return [a for a in root.findall("action") if (a.findtext("unique-id") or "").startswith(UCA_PREFIX)]


def _uca_action(action: str, method: str, label: str) -> ET.Element:
    el = ET.Element("action")
    ET.SubElement(el, "icon").text = f"{APP_ID}-symbolic"
    ET.SubElement(el, "name").text = label
    for lang, text in _translations(label).items():
        ET.SubElement(el, "name", {XML_LANG: lang}).text = text
    ET.SubElement(el, "submenu").text = "Koofr"
    ET.SubElement(el, "unique-id").text = UCA_PREFIX + action
    ET.SubElement(el, "command").text = _command(method, "%f")
    ET.SubElement(el, "description").text = label
    ET.SubElement(el, "range")
    ET.SubElement(el, "patterns").text = "*"
    for kind in UCA_KINDS.get(action, ("directories", *FILE_KINDS)):
        ET.SubElement(el, kind)
    return el


def _write_uca(environ, add: bool) -> bool:
    """Thunar's custom actions: Spola's replaced (add) or removed, the user's
    own kept as they are. Only an existing uca.xml is touched: Thunar writes
    its default actions there on its first start."""
    path = _uca_file(environ)
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return False
    root = tree.getroot()
    for action in [a for a in root.findall("action") if (a.findtext("unique-id") or "").startswith(UCA_PREFIX)]:
        root.remove(action)
    if add:
        for action, method, label in ACTIONS:
            root.append(_uca_action(action, method, label))
    ET.indent(tree, space="\t")
    tmp = path.with_suffix(".xml.spola-tmp")
    tree.write(tmp, encoding="UTF-8", xml_declaration=True)
    os.replace(tmp, path)
    return True


# -- install / uninstall ------------------------------------------------------------

def install(environ=None) -> list[Path]:
    src = source_dir()
    written = []
    found = detected(environ)
    for name, (_folder, _file, file_manager) in PYTHON_TARGETS.items():
        if name not in found:
            continue
        target = extension_path(name, environ)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(extension_text(file_manager), encoding="utf-8")
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
    if "dolphin" in found:
        menu = service_menu_path(environ)
        menu.parent.mkdir(parents=True, exist_ok=True)
        menu.write_text(service_menu_text(), encoding="utf-8")
        menu.chmod(0o755)            # Plasma 6 runs only executable service menus from the home folder
        written.append(menu)
    if "thunar" in found and _write_uca(environ, add=True):
        written.append(_uca_file(environ))
    _refresh_icon_cache(environ)
    return written


def uninstall(environ=None) -> list[Path]:
    removed = []
    paths = [extension_path(name, environ) for name in PYTHON_TARGETS] + [service_menu_path(environ)]
    for path in paths:
        if path.exists():
            if MARKER not in path.read_text(encoding="utf-8", errors="replace"):
                continue      # not ours
            path.unlink()
            removed.append(path)
    for path in [*emblem_paths(environ), *catalogue_paths(environ)]:
        if path.exists():
            path.unlink()
            removed.append(path)
    if _uca_ours(environ) and _write_uca(environ, add=False):
        removed.append(_uca_file(environ))
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
