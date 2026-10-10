"""Shared test helpers: put src/ on sys.path, find fixtures and rclone."""

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT / "src"))


def fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def rclone_binary() -> str | None:
    return os.environ.get("SPOLA_RCLONE") or shutil.which("rclone")
