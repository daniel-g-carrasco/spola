"""Unexpected failures said out loud. Spola has no log of its own: what
goes wrong in a thread or in the main loop must at least reach stderr (the
journal, when Spola runs as a service), with its traceback, instead of
vanishing with the thread or the idle source that raised it."""

from __future__ import annotations

import sys
import traceback


def complain(where: str, error: BaseException) -> None:
    print(f"spola: {where}: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
    traceback.print_exception(error, file=sys.stderr)
