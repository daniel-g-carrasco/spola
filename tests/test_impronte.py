# SPDX-License-Identifier: GPL-3.0-or-later
"""The copies of the model's files are the manifest's: each file listed in
impronte/<group>.json is there, with its fingerprint (the SHA-256 of the
text with "\\r\\n" turned into "\\n"; a file that is not UTF-8 as it is,
the same rule as tools/impronte.py of the model, written again here so that
this test needs nothing but the standard library).

A file changed by hand fails: the change goes to the model, then the copy
is updated with tools/impronte.py --apply. Never the other way round.

impronte/locali.json (optional, {"path": "why"}) lists the variants meant
to be: those files may differ. An entry without its reason, of a file that
is in no manifest, or of a file that is its fingerprint again, fails: the
list can only get shorter.

The shared modules under src/ import the rest of the package relatively
(`from . import minisign`, `from ..i18n import _`), never by the package's
name: a copy that names it would not be the same file in every app.

Where there is no manifest (the common folder itself) the test is skipped.
"""

import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import _setup

LOCAL = "locali.json"


def fingerprint(data: bytes) -> str:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def manifests(root: Path) -> dict[str, dict]:
    """{file name: manifest} of root/impronte/*.json but locali.json."""
    folder = root / "impronte"
    if not folder.is_dir():
        return {}
    return {path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(folder.glob("*.json")) if path.name != LOCAL}


def local_variants(root: Path) -> dict[str, str]:
    path = root / "impronte" / LOCAL
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def recorded(root: Path) -> dict[str, tuple[str | None, str]]:
    """{target: (fingerprint, group)} of every file of every manifest."""
    out = {}
    for name, manifest in manifests(root).items():
        group = manifest.get("group", Path(name).stem)
        for item in manifest.get("files", []):
            out[item["target"]] = (item.get("sha256"), group)
    return out


def _model_place(group: str) -> str:
    return "comune/ in app-template" if group == "comune" else f"the {group} group in app-template"


def fingerprint_problems(root: Path) -> list[str]:
    """A line for every file of the manifests that is missing or changed,
    but the variants of locali.json."""
    variants = local_variants(root)
    out = []
    for target, (sha256, group) in sorted(recorded(root).items()):
        if target in variants:
            continue
        path = root / target
        if not path.is_file():
            out.append(f"{target}: missing. Take it back with tools/impronte.py --apply, or out of the manifest "
                       f"if the app no longer has it")
        elif sha256 is None:
            out.append(f"{target}: no fingerprint in the manifest. Run tools/impronte.py --apply")
        elif fingerprint(path.read_bytes()) != sha256:
            out.append(f"{target}: changed by hand. Bring the change to the model ({_model_place(group)}), "
                       f"then update the copy with tools/impronte.py --apply")
    return out


def local_problems(root: Path) -> list[str]:
    """A line for every entry of locali.json that has no reason, is in no
    manifest, or is its fingerprint again."""
    files = recorded(root)
    out = []
    for target, why in sorted(local_variants(root).items()):
        if not isinstance(why, str) or not why.strip():
            out.append(f"{LOCAL}: {target}: say why it differs")
        elif target not in files:
            out.append(f"{LOCAL}: {target}: in no manifest, take it out")
        elif (root / target).is_file() and fingerprint((root / target).read_bytes()) == files[target][0]:
            out.append(f"{LOCAL}: {target}: it is the model's copy again, take it out")
    return out


def import_problems(root: Path, package: str) -> list[str]:
    """A line for every import that names the package in a file with a
    fingerprint under src/."""
    out = []
    for target in sorted(recorded(root)):
        path = root / target
        if not target.startswith("src/") or path.suffix != ".py" or not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), target)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name == package or name.startswith(package + "."):
                    out.append(f"{target}:{node.lineno}: {name}: a shared module imports the package relatively "
                               f"(from . import x, from ..i18n import _), never by its name")
    return out


@unittest.skipUnless(manifests(_setup.ROOT), f"no manifest in {_setup.ROOT / 'impronte'}: nothing has a fingerprint "
                                             f"here (the common folder is the source of the copies)")
class TheCopies(unittest.TestCase):
    def test_every_copy_has_its_fingerprint(self):
        found = fingerprint_problems(_setup.ROOT)
        self.assertEqual(found, [], "copies that are not the model's:\n" + "\n".join(found))

    def test_the_local_variants_can_only_get_fewer(self):
        found = local_problems(_setup.ROOT)
        self.assertEqual(found, [], "\n".join(found))

    def test_the_shared_modules_never_name_the_package(self):
        found = import_problems(_setup.ROOT, _setup.PACKAGE)
        self.assertEqual(found, [], "\n".join(found))


class TheCheckItself(unittest.TestCase):
    """The checks tried on an app written for the purpose: a check that
    lets everything through would otherwise look like a clean app."""

    def app(self, files: dict[str, bytes], manifest_files: dict[str, bytes], local: dict | None = None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        (root / "impronte").mkdir(exist_ok=True)
        manifest = {"format": 1, "group": "comune", "upstream_version": "abc", "mappings": [], "replacements": [],
                    "files": [{"source": f"comune/{name}", "target": name, "sha256": fingerprint(data),
                               "baseline_version": "abc"} for name, data in manifest_files.items()]}
        (root / "impronte" / "comune.json").write_text(json.dumps(manifest), encoding="utf-8")
        if local is not None:
            (root / "impronte" / LOCAL).write_text(json.dumps(local), encoding="utf-8")
        return root

    def test_a_clean_app_passes_and_line_ends_do_not_count(self):
        text = b"from . import minisign\nfrom ..i18n import _\n"
        root = self.app({"src/pkg/app/updates.py": text.replace(b"\n", b"\r\n"), "data/icon.png": b"\x89\r\n\xff"},
                        {"src/pkg/app/updates.py": text, "data/icon.png": b"\x89\r\n\xff"})
        self.assertEqual(fingerprint_problems(root) + local_problems(root) + import_problems(root, "pkg"), [])

    def test_a_copy_changed_by_hand_or_missing_is_seen(self):
        root = self.app({"src/pkg/app/paths.py": b"changed\n"},
                        {"src/pkg/app/paths.py": b"model\n", "src/pkg/app/log.py": b"log\n"})
        found = fingerprint_problems(root)
        self.assertEqual(len(found), 2, found)
        self.assertIn("src/pkg/app/log.py: missing", found[0])
        self.assertIn("src/pkg/app/paths.py: changed by hand. Bring the change to the model (comune/ in "
                      "app-template), then update the copy with tools/impronte.py --apply", found[1])

    def test_a_binary_file_is_not_normalized(self):
        root = self.app({"data/icon.png": b"\x89\r\n\xff"}, {"data/icon.png": b"\x89\n\xff"})
        self.assertEqual(len(fingerprint_problems(root)), 1)

    def test_a_local_variant_is_let_through_with_its_reason_only(self):
        files = {"src/pkg/app/paths.py": b"changed\n", "src/pkg/app/log.py": b"log\n"}
        model = {"src/pkg/app/paths.py": b"model\n", "src/pkg/app/log.py": b"log\n"}
        root = self.app(files, model, {"src/pkg/app/paths.py": "the portable folder of this app"})
        self.assertEqual(fingerprint_problems(root) + local_problems(root), [])
        root = self.app(files, model, {"src/pkg/app/paths.py": " ", "src/pkg/app/log.py": "was changed",
                                       "src/pkg/app/gone.py": "never there"})
        found = local_problems(root)
        self.assertEqual(len(found), 3, found)
        self.assertIn("gone.py: in no manifest", found[0])
        self.assertIn("log.py: it is the model's copy again", found[1])
        self.assertIn("paths.py: say why", found[2])

    def test_a_shared_module_that_names_the_package_is_seen(self):
        text = b"import os\nfrom pkg.i18n import _\ndef f():\n    import pkg.app.paths\nfrom . import x\n"
        root = self.app({"src/pkg/app/updates.py": text, "tests/test_x.py": b"import pkg\n"},
                        {"src/pkg/app/updates.py": text, "tests/test_x.py": b"import pkg\n"})
        found = import_problems(root, "pkg")
        self.assertEqual([line.split(": ")[0] for line in found],
                         ["src/pkg/app/updates.py:2", "src/pkg/app/updates.py:4"], found)
        self.assertEqual(import_problems(root, "pkgother"), [])


if __name__ == "__main__":
    unittest.main()
