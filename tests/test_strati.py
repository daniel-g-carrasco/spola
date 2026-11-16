# SPDX-License-Identifier: GPL-3.0-or-later
"""The layers, held by reading the imports (the AST: nothing is imported,
no toolkit is needed). The same file in every app of mine; what is the
app's own (its toolkit, its debts) is in tests/_strati.py.

    core/   works things out     may import: core, i18n
    data/   formats and tables   may import: data, i18n
    app/    services, no window  may import: app, core, data, the package's names, i18n
    gui/    the windows          may import: everything

The arrows go one way: gui uses app, app uses core and data, core and data
know nobody. The toolkit is imported in gui only, also inside a function;
where an app says so (WINDOWLESS, and WINDOWLESS_FILES to name the files),
its services in app/ may import the parts of the toolkit's library that
show nothing: a main loop, a bus, a keyring. Never core, data or a shared
module, and never a window's part. core and data do not touch the network.
A layer's __init__.py holds a docstring and nothing else (no re-exports:
they hide who uses whom). An import between folders is absolute and names
the layer (`from <package>.app import config`); a relative one stays in its
folder (`from . import ui`). The package's __init__ (the app's names) and
i18n import no layer. The entry point and the command line (__main__.py,
cli.py) are a front end as the window is: they may use every layer, but
the gui inside a function only, so that by themselves they never load the
toolkit.

The shared modules (the copies of the model's comune/, the files under
src/<package>/ of the manifests impronte/*.json: one list, the manifest's)
know no app. They import the rest of the package relatively, never by its
name, and only other shared modules; for them a relative import between
folders is the rule (`from ..i18n import _`, `from ..data import atomic`),
for every other module it stays forbidden. The arrows hold for them too,
and the toolkit never, not even with WINDOWLESS. In the common folder
itself (src/ is the package) every module but the package's and the
layers' __init__ is shared.

A broken rule fails with file, line and rule. DEBTS lists the imports
against the rules that are there today, one per line with the reason: a
new one is not accepted, and one that is gone must leave the list, so the
list can only get shorter.
"""

import ast
import json
import tempfile
import unittest
from pathlib import Path

import _strati

ROOT = Path(__file__).resolve().parent.parent
LAYERS = ("core", "data", "app", "gui")
MAY = {"core": {"core"}, "data": {"data"}, "app": {"app", "core", "data"}, "gui": set(LAYERS)}
NETWORK = ("urllib.request", "http.client", "http.server", "socket", "ssl", "ftplib", "smtplib", "requests")
ROOT_FOR_ALL = ("i18n",)                        # the one module of the root every layer may import
COMMON = "comune"                               # the name the common folder's tests/_setup.py gives src/

# the app's own, from tests/_strati.py
TOOLKIT = tuple(_strati.TOOLKIT)
WINDOWLESS = tuple(getattr(_strati, "WINDOWLESS", ()))
WINDOWLESS_FILES = tuple(getattr(_strati, "WINDOWLESS_FILES", ()))
DEBTS = dict(getattr(_strati, "DEBTS", {}))
GUI_WITHOUT_TOOLKIT = getattr(_strati, "GUI_WITHOUT_TOOLKIT", {})
GUI_VALUES = set(getattr(_strati, "GUI_VALUES", set()))


def package_dir(root: Path = ROOT) -> Path:
    """The one package under src/, or src/ itself in the common folder."""
    src = root / "src"
    if (src / "__init__.py").is_file():
        return Path(src.as_posix())
    found = [p for p in sorted(src.iterdir()) if (p / "__init__.py").is_file()]
    assert len(found) == 1, found
    # MSYS2's Python mixes "/" and "\\" in what iterdir() gives, and then
    # relative_to() does not find a file under its own folder
    return Path(found[0].as_posix())


def package_name(src: Path, root: Path = ROOT) -> str:
    return COMMON if Path(src) == Path((root / "src").as_posix()) else src.name


def shared_modules(root: Path = ROOT) -> set[str]:
    """The shared modules, as paths below the package: the targets under
    src/<package>/ of the manifests; in the common folder, every module but
    the package's and the layers' __init__."""
    src = package_dir(root)
    if package_name(src, root) == COMMON:
        return {p.relative_to(src).as_posix() for p in src.rglob("*.py")
                if not (p.name == "__init__.py" and len(p.relative_to(src).parts) <= 2)}
    prefix = f"src/{src.name}/"
    out = set()
    folder = root / "impronte"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        if path.name == "locali.json":
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("files", []):
            target = item["target"]
            if target.startswith(prefix) and target.endswith(".py"):
                out.add(target[len(prefix):])
    return out


def layer_of(parts: tuple[str, ...]) -> str | None:
    return parts[0] if parts and parts[0] in LAYERS else None


def module_of(parts: tuple[str, ...], src: Path) -> str:
    """The file below the package that a dotted name (without the package)
    lives in: app/minisign.py, app/work/__init__.py, or __init__.py for the
    package's own names."""
    for count in range(len(parts), 0, -1):
        path = src.joinpath(*parts[:count])
        if path.with_suffix(".py").is_file():
            return "/".join(parts[:count]) + ".py"
        if (path / "__init__.py").is_file():
            return "/".join(parts[:count]) + "/__init__.py"
    return "__init__.py"


def imports(path: Path, src: Path, name: str | None = None):
    """(line, dotted name, relative level, inside a function) of every
    import of the file; the name is absolute, starting at the package."""
    package = (name or src.name, *path.relative_to(src).parts[:-1])
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


def broken(src: Path, shared=frozenset(), windowless=(), windowless_files=(), toolkit=TOOLKIT,
           name: str | None = None) -> list[tuple[str, str]]:
    """(key for DEBTS, message with file, line and rule) of every import
    against the rules, and of every layer __init__ that holds code."""
    name = name or src.name
    out = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src)
        mine = layer_of(rel.parts)
        here = rel.parts[:-1]
        common = rel.as_posix() in shared
        if mine and rel.name == "__init__.py":
            body = ast.parse(path.read_text(encoding="utf-8")).body
            if any(not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)) for n in body):
                out.append((f"{rel.as_posix()} -> code", f"{rel.as_posix()}:1: a layer's __init__ holds a docstring only"))
        for line, dotted, level, in_function in imports(path, src, name):
            parts = tuple(dotted.split("."))
            inside = parts[0] == name
            theirs = layer_of(parts[1:]) if inside else None
            rule = None
            if starts(dotted, toolkit) and mine != "gui":
                shows_nothing = windowless and (dotted in toolkit or starts(dotted, windowless))
                allowed = not windowless_files or rel.as_posix() in windowless_files
                if not (mine == "app" and shows_nothing and allowed and not common):
                    rule = "the toolkit is imported in gui only"
                    if common:
                        rule += " (and never in a shared module)"
                    elif windowless and mine == "app":
                        rule += " (app may import what shows nothing: " + ", ".join(windowless)
                        rule += (" in " + ", ".join(windowless_files) if windowless_files else "") + ")"
            elif inside and common and not level:
                rule = "a shared module imports the package relatively (from . import x, from ..i18n import _), " \
                       "never by its name"
            elif inside and common and module_of(parts[1:], src) not in shared:
                rule = "a shared module knows no app: its names are arguments, and it imports shared modules only"
            elif mine in ("core", "data") and starts(dotted, NETWORK):
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
            if rule is None and inside and level and not common:
                target = parts[1:len(here) + 1]
                deeper = len(parts) > len(here) + 2 and (src.joinpath(*parts[1:len(here) + 2])).is_dir()
                if level > 1 or target != here or deeper:
                    rule = "an import between folders is absolute: it names the layer"
            if rule:
                what = "/".join(parts[1:3]) if inside else parts[0]
                out.append((f"{rel.as_posix()} -> {what}", f"{rel.as_posix()}:{line}: {dotted}: {rule}"))
    return out


def gui_without_toolkit(src: Path, toolkit=TOOLKIT, name: str | None = None) -> set[str]:
    out = set()
    if not (src / "gui").is_dir():
        return out
    for path in sorted((src / "gui").rglob("*.py")):
        if path.name != "__init__.py" and not any(starts(dotted, toolkit) for _l, dotted, _r, _f in imports(path, src, name)):
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
        common = package_name(src) == COMMON
        for name in LAYERS:
            if common and name == "gui":
                continue                                    # the common modules have no window
            self.assertTrue((src / name / "__init__.py").is_file(), f"{name}/ with its __init__.py (one line saying "
                                                                    f"what goes there, when it is empty)")

    def test_every_import_goes_one_way(self):
        src = package_dir()
        shared = shared_modules()
        self.assertEqual(sorted(name for name in shared if not (src / name).is_file()), [],
                         "the manifest names modules that are gone")
        found = broken(src, shared, WINDOWLESS, WINDOWLESS_FILES, TOOLKIT, package_name(src))
        new = [message for key, message in found if key not in DEBTS]
        self.assertEqual(new, [], "imports against the layers:\n" + "\n".join(new))
        paid = sorted(set(DEBTS) - {key for key, _message in found})
        self.assertEqual(paid, [], "debts paid: take these lines out of DEBTS")

    def test_the_gui_holds_no_plain_logic(self):
        src = package_dir()
        values = {name for name in GUI_VALUES if (src / name).is_file()}
        self.assertEqual(sorted(name for name in values if not only_values(src / name)), [],
                         "GUI_VALUES is for names set to numbers and strings: this one does more")
        if isinstance(GUI_WITHOUT_TOOLKIT, dict):
            self.assertEqual(sorted(name for name, why in GUI_WITHOUT_TOOLKIT.items() if not str(why).strip()), [],
                             "GUI_WITHOUT_TOOLKIT: say why each one stays in gui")
        frozen = set(GUI_WITHOUT_TOOLKIT)
        found = gui_without_toolkit(src, TOOLKIT, package_name(src)) - values
        self.assertEqual(sorted(found - frozen), [],
                         "gui modules without the toolkit: plain logic belongs in core, data or app")
        self.assertEqual(sorted(frozen - found), [], "moved or gone: take these out of GUI_WITHOUT_TOOLKIT")


class TheCheckItself(unittest.TestCase):
    """The rules tried on a package written for the purpose: a check that
    lets everything through would otherwise look like a clean package."""

    TOOLKIT = ("gi",)

    def tree(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = Path(tmp.name) / "src" / "pkg"
        for name in ("", *LAYERS):
            (src / name).mkdir(parents=True, exist_ok=True)
            (src / name / "__init__.py").write_text('"""Docstring."""\n', encoding="utf-8")
        for name, text in files.items():
            (src / name).parent.mkdir(parents=True, exist_ok=True)
            (src / name).write_text(text, encoding="utf-8")
        return src

    def found(self, files: dict[str, str], shared=frozenset(), windowless=(), windowless_files=()) -> list[str]:
        src = self.tree(files)
        return [message for _key, message in broken(src, shared, windowless, windowless_files, self.TOOLKIT)]

    def rules(self, files: dict[str, str]) -> list[str]:
        return self.found(files)

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

    def test_a_shared_module_imports_relatively_and_only_shared_modules(self):
        shared = {"i18n.py", "app/paths.py", "app/log.py", "app/updates.py", "data/atomic.py", "core/sizes.py",
                  "app/work/__init__.py", "app/work/run.py"}
        files = {
            "i18n.py": "import gettext\n",
            "app/paths.py": "from pkg import SLUG\n",                                       # by its name
            "app/log.py": "from . import paths\nfrom ..i18n import _\nfrom ..data import atomic\n"
                          "from ..data.atomic import write\nfrom .work import run\nfrom . import work\n",
            "app/updates.py": "from . import config\n",                                     # not shared
            "app/config.py": "from pkg import SLUG\n",
            "app/work/__init__.py": "",
            "app/work/run.py": "from ...i18n import _\nfrom .. import paths\n",
            "data/atomic.py": "import os\n",
            "core/sizes.py": "from ..i18n import _\nfrom .. import NAME\n",                  # the app's names
            "app/other.py": "from ..data import atomic\n",                                  # not shared: absolute
        }
        found = self.found(files, shared)
        self.assertEqual([message.split(":")[0] for message in found],
                         ["app/other.py", "app/paths.py", "app/updates.py", "core/sizes.py"], found)
        self.assertIn("app/other.py:1: pkg.data.atomic: an import between folders is absolute", found[0])
        self.assertIn("app/paths.py:1: pkg.SLUG: a shared module imports the package relatively", found[1])
        self.assertIn("app/updates.py:1: pkg.app.config: a shared module knows no app", found[2])
        self.assertIn("core/sizes.py:2: pkg.NAME: a shared module knows no app", found[3])

    def test_the_arrows_hold_for_shared_modules_too(self):
        shared = {"data/atomic.py", "app/paths.py", "core/a.py", "app/b.py"}
        files = {"data/atomic.py": "from ..app import paths\n", "app/paths.py": "import os\n",
                 "core/a.py": "from ..data import atomic\n", "app/b.py": "from ..core import a\nfrom ..data import atomic\n"}
        found = self.found(files, shared)
        self.assertEqual(len(found), 2, found)
        self.assertIn("core/a.py:1: pkg.data.atomic: core may not import data", found[0])
        self.assertIn("data/atomic.py:1: pkg.app.paths: data may not import app", found[1])

    def test_what_shows_nothing_is_let_into_app_only_where_the_app_says(self):
        files = {"app/engine.py": "import gi\nfrom gi.repository import GLib, Gio\n",
                 "app/about.py": "def f():\n    from gi.repository import Gtk\n",
                 "app/paths.py": "from gi.repository import GLib\n",
                 "core/sizes.py": "from gi.repository import GLib\n",
                 "cli.py": "def f():\n    from gi.repository import Gio\n"}
        loop = ("gi.repository.GLib", "gi.repository.Gio")
        found = self.found(files, {"app/paths.py"}, loop)
        self.assertEqual([message.split(":")[0] for message in found],
                         ["app/about.py", "app/paths.py", "cli.py", "core/sizes.py"], found)
        self.assertIn("gi.repository.Gtk: the toolkit is imported in gui only (app may import what shows nothing",
                      found[0])
        self.assertIn("and never in a shared module", found[1])
        # and without the app's word nothing of the toolkit is let in
        self.assertEqual(len(self.found(files)), 7)
        # and with a list of files, only those
        found = self.found(files, {"app/paths.py"}, loop, ("app/other.py",))
        self.assertIn("app/engine.py", [message.split(":")[0] for message in found])
        self.assertIn("in app/other.py", "\n".join(found))

    def test_a_layer_init_with_code_is_seen(self):
        self.assertIn("docstring only", self.rules({"app/__init__.py": '"""Doc."""\nfrom .x import y\n', "app/x.py": "y = 1\n"})[0])

    def test_a_gui_module_without_the_toolkit_is_seen(self):
        src = self.tree({"gui/sums.py": "import math\n", "gui/window.py": "import gi\n"})
        self.assertEqual(gui_without_toolkit(src, self.TOOLKIT), {"gui/sums.py"})

    def test_values_are_told_from_logic(self):
        src = self.tree({"gui/values.py": '"""Doc."""\n\nSPACE_M = 8\nNAME = "x"\n',
                         "gui/sums.py": "SPACE_M = 8\n\n\ndef twice():\n    return SPACE_M * 2\n"})
        self.assertTrue(only_values(src / "gui" / "values.py"))
        self.assertFalse(only_values(src / "gui" / "sums.py"))

    def test_the_shared_modules_are_the_manifests(self):
        src = self.tree({"app/paths.py": "", "i18n.py": ""})
        root = src.parent.parent
        (root / "impronte").mkdir()
        files = [{"target": target, "source": "x", "sha256": None, "baseline_version": "x"}
                 for target in ("src/pkg/app/paths.py", "src/pkg/i18n.py", "tests/test_strati.py", "src/pkg/style.css")]
        (root / "impronte" / "comune.json").write_text(json.dumps({"files": files}), encoding="utf-8")
        (root / "impronte" / "locali.json").write_text(json.dumps({"src/pkg/app/x.py": "why"}), encoding="utf-8")
        self.assertEqual(shared_modules(root), {"app/paths.py", "i18n.py"})

    def test_in_the_common_folder_every_module_is_shared_and_named_comune(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        files = {"__init__.py": '"""Doc."""\n', "i18n.py": "import gettext\n", "app/__init__.py": '"""Doc."""\n',
                 "app/updates.py": "from . import minisign\nfrom ..i18n import _\nfrom comune.app import paths\n",
                 "app/minisign.py": "", "app/paths.py": "", "app/work/__init__.py": "", "app/work/run.py": ""}
        for name, text in files.items():
            (root / "src" / name).parent.mkdir(parents=True, exist_ok=True)
            (root / "src" / name).write_text(text, encoding="utf-8")
        src = package_dir(root)
        self.assertEqual(package_name(src, root), COMMON)
        self.assertEqual(shared_modules(root), {"i18n.py", "app/updates.py", "app/minisign.py", "app/paths.py",
                                                "app/work/__init__.py", "app/work/run.py"})
        found = [m for _k, m in broken(src, shared_modules(root), toolkit=self.TOOLKIT, name=package_name(src, root))]
        self.assertEqual(len(found), 1, found)
        self.assertIn("app/updates.py:3: comune.app.paths: a shared module imports the package relatively", found[0])


if __name__ == "__main__":
    unittest.main()
