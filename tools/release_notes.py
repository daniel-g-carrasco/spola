#!/usr/bin/env python3
"""The notes of one version, from CHANGELOG.md: what its release page says
and what the update window shows (GitHub's own "generated notes" are one
line with a link).

    python3 tools/release_notes.py 0.3.45 [CHANGELOG.md]
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def notes(version: str, text: str) -> str:
    """The section of `version`, without its heading; "" if it has none."""
    found = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)", text, re.S | re.M)
    return found.group(1).strip() if found else ""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "CHANGELOG.md"
    found = notes(sys.argv[1].lstrip("v"), path.read_text(encoding="utf-8"))
    if not found:
        print(f"CHANGELOG.md has no section for {sys.argv[1]}", file=sys.stderr)
        return 1
    print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
