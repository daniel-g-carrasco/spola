"""The layers, held by reading the imports (the AST: nothing is imported,
no toolkit is needed). The same file in every app of mine.

    core/   works things out     may import: core, i18n
    data/   formats and tables   may import: data, i18n
    app/    services, no window  may import: app, core, data, the package's names, i18n
    gui/    the windows          may import: everything

The arrows go one way: gui uses app, app uses core and data, core and data
know nobody. The toolkit is imported in gui only, also inside a function;
where an app says so (WINDOWLESS), its services in app/ may import the
parts of the toolkit's library that show nothing: a main loop, a bus, a
keyring. Never core, data or a shared module, and never a window's part.
core and data do not touch the network. A layer's __init__.py holds a
docstring and nothing else (no re-exports: they hide who uses whom). An
import between folders is absolute and names the layer (`from <package>.app
import config`); a relative one stays in its folder (`from . import ui`).
The package's __init__ (the app's names) and i18n import no layer. The
entry point and the command line (__main__.py, cli.py) are a front end as
the window is: they may use every layer, but the gui inside a function
only, so that by themselves they never load the toolkit.

A broken rule fails with file, line and rule. DEBTS lists the imports
against the rules that are there today, one per line with the reason: a
new one is not accepted, and one that is gone must leave the list, so the
list can only get shorter.
"""

import ast
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ("core", "data", "app", "gui")
MAY = {"core": {"core"}, "data": {"data"}, "app": {"app", "core", "data"}, "gui": set(LAYERS)}
TOOLKIT = ("gi",)                               # the Qt apps: ("PySide6", "shiboken6")
# The parts of the toolkit's library that show nothing, which app/ may
# import (and the bare package with them). Spola's services live on them
# (Daniel, 10/11/2026): the engine on GLib's main loop and GObject's signals;
# the file managers' service, the Background portal and logind over Gio's
# D-Bus; Gio's file monitors; libsecret for the app password. Rewriting the
# engine that decides about the user's files to be rid of them would buy
# nothing. Never Gtk, Adw or Gdk outside gui/, never any of it in core/.
WINDOWLESS: tuple[str, ...] = ("gi.repository.GLib", "gi.repository.Gio", "gi.repository.GObject",
                               "gi.repository.Secret")
NETWORK = ("urllib.request", "http.client", "http.server", "socket", "ssl", "ftplib", "smtplib", "requests")
ROOT_FOR_ALL = ("i18n",)                        # the one module of the root every layer may import
# The modules every app of mine has, the same, that know no app (the
# template's list). None here yet: Spola's services were moved as they were,
# and still read the app's names from the package.
SHARED: set[str] = set()
# "file -> what it imports": "why it is still there, and when it goes".
# Three are left, one call each, where what shows nothing is still not
# allowed: core/, the command line, and the versions of GTK itself.
DEBTS: dict[str, str] = {
    "core/progress.py -> gi": "GLib.format_size for the sizes: a function of core's own, with the same words",
    "app/diagnostics.py -> gi": "the versions of GLib, GTK and libadwaita, inside a function: gui/ passes them "
                                "in, as the template does (toolkit_versions)",
    "cli.py -> gi": "one D-Bus call, inside a function, to know whether the app is running: a service in app/",
}
# gui modules that do not import the toolkit: plain logic in the wrong layer.
# Frozen: none may be added (a new one belongs in core, data or app).
GUI_WITHOUT_TOOLKIT: set[str] = set()
# gui modules that are values and nothing else (names set to numbers and
# strings): the style's, written by tools/make_style.py. None here yet.
GUI_VALUES: set[str] = set()


def package_dir(root: Path = ROOT) -> Path:
    """The one package under src/."""
    found = [p for p in sorted((root / "src").iterdir()) if (p / "__init__.py").is_file()]
    assert len(found) == 1, found
    # MSYS2's Python mixes "/" and "\\" in what iterdir() gives, and then
    # relative_to() does not find a file under its own folder
    return Path(found[0].as_posix())


def layer_of(parts: tuple[str, ...]) -> str | None:
    return parts[0] if parts and parts[0] in LAYERS else None


def imports(path: Path, src: Path):
    """(line, dotted name, relative level, inside a function) of every
    import of the file; the name is absolute, starting at the package."""
    package = (src.name, *path.relative_to(src).parts[:-1])
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    nested = {id(node) for scope in ast.walk(tree) if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
              for node in ast.walk(scope)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, 0, id(node) in nested
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
            module = ".".join(x for x in (base, node.module) if x)
            for alias in node.names:
                yield node.lineno, f"{module}.{alias.name}", node.level, id(node) in nested


def starts(name: str, prefixes) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def broken(src: Path, shared=frozenset(), windowless=()) -> list[tuple[str, str]]:
    """(key for DEBTS, message with file, line and rule) of every import
    against the rules, and of every layer __init__ that holds code."""
    out = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src)
        mine = layer_of(rel.parts)
        here = rel.parts[:-1]
        if mine and rel.name == "__init__.py":
            body = ast.parse(path.read_text(encoding="utf-8")).body
            if any(not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)) for n in body):
                out.append((f"{rel.as_posix()} -> code", f"{rel.as_posix()}:1: a layer's __init__ holds a docstring only"))
        for line, name, level, in_function in imports(path, src):
            parts = tuple(name.split("."))
            inside = parts[0] == src.name
            theirs = layer_of(parts[1:]) if inside else None
            rule = None
            module = "/".join(parts[1:3 if theirs else 2]) + ".py"
            if starts(name, TOOLKIT) and mine != "gui":
                shows_nothing = windowless and (name in TOOLKIT or starts(name, windowless))
                if not (mine == "app" and shows_nothing and rel.as_posix() not in shared):
                    rule = "the toolkit is imported in gui only"
                    if windowless and mine == "app":
                        rule += " (app may import what shows nothing: " + ", ".join(windowless) + ")"
            elif inside and rel.as_posix() in shared and module not in shared:
                rule = "a shared module knows no app: its names are arguments, and it imports shared modules only"
            elif mine in ("core", "data") and starts(name, NETWORK):
                rule = f"no network in {mine}"
            elif inside and mine is None and rel.name in ("__init__.py", "i18n.py"):
                if theirs is not None or rel.name == "__init__.py":
                    rule = f"{rel.name} imports no layer" if theirs else "the package's __init__ imports none of its modules"
            elif inside and mine is None and theirs == "gui" and not in_function:       # __main__.py, cli.py
                rule = "the gui is imported inside a function (the command line must not load the toolkit)"
            elif inside and mine is not None and theirs is not None and theirs not in MAY[mine]:
                rule = f"{mine} may not import {theirs}"
            elif inside and mine in ("core", "data") and theirs is None and parts[1:2] and parts[1] not in ROOT_FOR_ALL:
                rule = f"{mine} takes the app's names as arguments (only i18n comes from the root)"
            if rule is None and inside and level:
                target = parts[1:len(here) + 1]
                deeper = len(parts) > len(here) + 2 and (src.joinpath(*parts[1:len(here) + 2])).is_dir()
                if level > 1 or target != here or deeper:
                    rule = "an import between folders is absolute: it names the layer"
            if rule:
                what = "/".join(parts[1:3]) if inside else parts[0]
                out.append((f"{rel.as_posix()} -> {what}", f"{rel.as_posix()}:{line}: {name}: {rule}"))
    return out


def gui_without_toolkit(src: Path) -> set[str]:
    out = set()
    for path in sorted((src / "gui").rglob("*.py")):
        if path.name != "__init__.py" and not any(starts(name, TOOLKIT) for _l, name, _r, _f in imports(path, src)):
            out.add(path.relative_to(src).as_posix())
    return out


def only_values(path: Path) -> bool:
    """A docstring, then names set to numbers and strings: nothing else."""
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return all(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) for node in body)


class Layers(unittest.TestCase):
    def test_the_layers_are_there(self):
        src = package_dir()
        for name in LAYERS:
            self.assertTrue((src / name / "__init__.py").is_file(), f"{name}/ with its __init__.py (one line saying "
                                                                    f"what goes there, when it is empty)")

    def test_every_import_goes_one_way(self):
        src = package_dir()
        self.assertEqual(sorted(name for name in SHARED if not (src / name).is_file()), [], "SHARED names files that are gone")
        found = broken(src, SHARED, WINDOWLESS)
        new = [message for key, message in found if key not in DEBTS]
        self.assertEqual(new, [], "imports against the layers:\n" + "\n".join(new))
        paid = sorted(set(DEBTS) - {key for key, _message in found})
        self.assertEqual(paid, [], "debts paid: take these lines out of DEBTS")

    def test_the_gui_holds_no_plain_logic(self):
        src = package_dir()
        values = {name for name in GUI_VALUES if (src / name).is_file()}
        self.assertEqual(sorted(name for name in values if not only_values(src / name)), [],
                         "GUI_VALUES is for names set to numbers and strings: this one does more")
        found = gui_without_toolkit(src) - values
        self.assertEqual(sorted(found - GUI_WITHOUT_TOOLKIT), [],
                         "gui modules without the toolkit: plain logic belongs in core, data or app")
        self.assertEqual(sorted(GUI_WITHOUT_TOOLKIT - found), [], "moved or gone: take these out of GUI_WITHOUT_TOOLKIT")


class TheCheckItself(unittest.TestCase):
    """The rules tried on a package written for the purpose: a check that
    lets everything through would otherwise look like a clean package."""

    def tree(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src" / "pkg"
        for name in ("", *LAYERS):
            (src / name).mkdir(parents=True, exist_ok=True)
            (src / name / "__init__.py").write_text('"""Docstring."""\n', encoding="utf-8")
        for name, text in files.items():
            (src / name).write_text(text, encoding="utf-8")
        return src

    def rules(self, files: dict[str, str]) -> list[str]:
        return [message for _key, message in broken(self.tree(files))]

    def test_a_clean_package_passes(self):
        self.assertEqual(self.rules({
            "i18n.py": "from . import NAME\n",
            "__main__.py": "from . import i18n\ndef main():\n    from pkg.gui.window import run\n",
            "cli.py": "from pkg.app import service\nfrom pkg.core.sums import f\nfrom .i18n import _\n",
            "core/sums.py": "import math\nfrom pkg.i18n import _\nfrom . import other\nfrom .other import x\n",
            "core/other.py": "x = 1\n",
            "data/table.py": "import sqlite3\n",
            "app/service.py": "from pkg import NAME\nfrom pkg.core import sums\nfrom pkg.data.table import T\nimport urllib.request\n",
            "gui/window.py": "import gi\nfrom pkg.app import service\nfrom pkg.core.sums import f\nfrom . import ui\n",
            "gui/ui.py": "from gi.repository import Gtk\n",
        }), [])

    def test_each_rule_is_seen_with_file_and_line(self):
        cases = {
            "core/a.py": ("\nfrom pkg.app import service\n", "core/a.py:2: pkg.app.service: core may not import app"),
            "core/b.py": ("from pkg.data import table\n", "core may not import data"),
            "data/c.py": ("from pkg.core import sums\n", "data may not import core"),
            "app/d.py": ("def f():\n    from pkg.gui import window\n", "app/d.py:2: pkg.gui.window: app may not import gui"),
            "app/e.py": ("def f():\n    from gi.repository import GLib\n", "the toolkit is imported in gui only"),
            "core/f.py": ("import urllib.request\n", "no network in core"),
            "data/g.py": ("import socket\n", "no network in data"),
            "core/h.py": ("from pkg import NAME\n", "core takes the app's names as arguments"),
            "gui/i.py": ("import gi\nfrom ..app import service\n", "an import between folders is absolute"),
            "__main__.py": ("from pkg.gui import window\n", "inside a function"),
            "cli.py": ("from pkg.gui import window\n", "inside a function"),
            "i18n.py": ("from pkg.app import paths\n", "i18n.py imports no layer"),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(name):
                found = self.rules({name: text})
                self.assertEqual(len(found), 1, found)
                self.assertIn(expected, found[0])

    def test_a_shared_module_that_knows_the_app_is_seen(self):
        files = {"app/paths.py": "from pkg import SLUG\n", "app/log.py": "from . import paths\nfrom pkg.i18n import _\n",
                 "app/updates.py": "from . import config\n", "app/config.py": "from pkg import SLUG\n", "i18n.py": "import gettext\n"}
        found = [message for _key, message in broken(self.tree(files), {"i18n.py", "app/paths.py", "app/log.py", "app/updates.py"})]
        self.assertEqual(len(found), 2, found)
        self.assertIn("app/paths.py:1: pkg.SLUG: a shared module knows no app", found[0])
        self.assertIn("app/updates.py:1: pkg.app.config: a shared module knows no app", found[1])

    def test_what_shows_nothing_is_let_into_app_only_where_the_app_says(self):
        files = {"app/engine.py": "import gi\nfrom gi.repository import GLib, Gio\n",
                 "app/about.py": "def f():\n    from gi.repository import Gtk\n",
                 "app/paths.py": "from gi.repository import GLib\n",
                 "core/sizes.py": "from gi.repository import GLib\n",
                 "cli.py": "def f():\n    from gi.repository import Gio\n"}
        loop = ("gi.repository.GLib", "gi.repository.Gio")
        found = [message for _key, message in broken(self.tree(files), {"app/paths.py"}, loop)]
        self.assertEqual([message.split(":")[0] for message in found],
                         ["app/about.py", "app/paths.py", "cli.py", "core/sizes.py"], found)
        self.assertIn("gi.repository.Gtk: the toolkit is imported in gui only (app may import what shows nothing",
                      found[0])
        # and without the app's word nothing of the toolkit is let in
        self.assertEqual(len(broken(self.tree(files))), 7)

    def test_a_layer_init_with_code_is_seen(self):
        self.assertIn("docstring only", self.rules({"app/__init__.py": '"""Doc."""\nfrom .x import y\n', "app/x.py": "y = 1\n"})[0])

    def test_a_gui_module_without_the_toolkit_is_seen(self):
        src = self.tree({"gui/sums.py": "import math\n", "gui/window.py": "import gi\n"})
        self.assertEqual(gui_without_toolkit(src), {"gui/sums.py"})

    def test_values_are_told_from_logic(self):
        src = self.tree({"gui/values.py": '"""Doc."""\n\nSPACE_M = 8\nNAME = "x"\n',
                         "gui/sums.py": "SPACE_M = 8\n\n\ndef twice():\n    return SPACE_M * 2\n"})
        self.assertTrue(only_values(src / "gui" / "values.py"))
        self.assertFalse(only_values(src / "gui" / "sums.py"))


if __name__ == "__main__":
    unittest.main()
