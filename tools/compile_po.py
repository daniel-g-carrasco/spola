#!/usr/bin/env python3
"""Compile po/*.po into GNU gettext catalogues with msgfmt (GNU gettext,
also in the Flatpak SDK):

  python3 tools/compile_po.py                 -> locale/<lang>/LC_MESSAGES/spola.mo
  python3 tools/compile_po.py --dest /app/share/locale
  python3 tools/compile_po.py --update        refresh po/spola.pot and merge it into po/*.po
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "spola"


def update() -> None:
    pot = ROOT / "po" / f"{DOMAIN}.pot"
    sources = sorted(str(p.relative_to(ROOT)) for p in [*(ROOT / "src" / "spola").glob("*.py"),
                                                          *(ROOT / "extensions").glob("*.py")])
    subprocess.run(["xgettext", "--language=Python", "--keyword=_", "--keyword=ngettext:1,2", "--keyword=pgettext:1c,2", "--keyword=N_",
                    "--keyword=tr",     # tr = translation_for(language).gettext: a name in a chosen language
                    "--from-code=UTF-8", f"--package-name={DOMAIN}", "--add-comments=TRANSLATORS",
                    "--output", str(pot), *sources], cwd=ROOT, check=True)
    for po in sorted((ROOT / "po").glob("*.po")):
        subprocess.run(["msgmerge", "--quiet", "--update", "--backup=none", str(po), str(pot)], check=True)


def compile_all(dest: Path) -> list[Path]:
    written = []
    for po in sorted((ROOT / "po").glob("*.po")):
        out = dest / po.stem / "LC_MESSAGES" / f"{DOMAIN}.mo"
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["msgfmt", "--check", "--output-file", str(out), str(po)], check=True)
        written.append(out)
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, default=ROOT / "locale")
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    if args.update:
        update()
    for out in compile_all(args.dest):
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
