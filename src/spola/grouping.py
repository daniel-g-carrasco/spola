"""How the folder pairs are laid out in the window: one group per parent
folder, so it is clear where each synced folder lives, with no tree to
expand (a closed branch would hide a pair that needs attention).

A parent with a single pair joins the nearest ancestor that has a group,
and the row then shows the path from there (~/data/media + photos/camera-roll)
instead of a group of one row.
"""

from __future__ import annotations

import os


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def group_pairs(pairs) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(parent folder, [(pair id, title relative to it), ...]), ...], both sorted."""
    groups: dict[str, list] = {}
    for pair in pairs:
        groups.setdefault(os.path.dirname(pair.local.rstrip("/")), []).append(pair)
    merged = True
    while merged:
        merged = False
        for parent in sorted(groups, key=len, reverse=True):
            if len(groups[parent]) != 1:
                continue
            ancestor = next((g for g in sorted(groups, key=len, reverse=True)
                             if g != parent and _within(parent, g)), None)
            if ancestor is not None:
                groups[ancestor].extend(groups.pop(parent))
                merged = True
                break
    out = []
    for parent in sorted(groups, key=str.casefold):
        rows = [(p.id, os.path.relpath(p.local, parent)) for p in groups[parent]]
        out.append((parent, sorted(rows, key=lambda r: r[1].casefold())))
    return out


def expected_remote(local: str, home: str) -> str | None:
    """The Koofr path a pair would have if it mirrored the home folder
    (~/data/work -> /data/work): when it does, the window does not repeat it."""
    local, home = os.path.realpath(local), os.path.realpath(home)
    if not local.startswith(home + "/"):
        return None
    return "/" + os.path.relpath(local, home)
