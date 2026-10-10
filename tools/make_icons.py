#!/usr/bin/env python3
"""Generate Spola's icons into data/icons/hicolor:

  scalable/apps/<APP_ID>.svg                  the app icon (a weaving shuttle)
  symbolic/apps/<APP_ID>-symbolic.svg         its symbolic version
  symbolic/apps/<APP_ID>-<state>-symbolic.svg tray states: synced, syncing,
                                              paused, error, attention

Symbolic icons use filled shapes only, holes cut with fill-rule="evenodd":
GTK and the tray hosts recolour them by filling every path, so a stroke-only
outline would turn into a solid blob.

    python3 tools/make_icons.py
"""

import math
from pathlib import Path

APP_ID = "io.github.daniel_g_carrasco.spola"
ROOT = Path(__file__).resolve().parent.parent / "data" / "icons" / "hicolor"
FG = "#2e3436"


def fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def circle(cx, cy, r, clockwise=True) -> str:
    sweep = 1 if clockwise else 0
    return (f"M{fmt(cx - r)},{fmt(cy)} A{fmt(r)},{fmt(r)} 0 1 {sweep} {fmt(cx + r)},{fmt(cy)} "
            f"A{fmt(r)},{fmt(r)} 0 1 {sweep} {fmt(cx - r)},{fmt(cy)} Z")


def poly(points) -> str:
    return "M" + " L".join(f"{fmt(x)},{fmt(y)}" for x, y in points) + " Z"


def rect(x, y, w, h) -> str:
    return poly([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])


def symbolic(*paths: str) -> str:
    body = "".join(f'<path fill="{FG}" fill-rule="evenodd" d="{p}"/>' for p in paths)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f"{body}</svg>\n")


def ring_arrow(cx, cy, r_out, r_in, start, end) -> str:
    """A ring segment from `start` to `end` degrees (clockwise, 0 = east)
    with an arrow head at `end`."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    large = 1 if (end - start) % 360 > 180 else 0
    o1, o2 = pt(r_out, start), pt(r_out, end)
    i1, i2 = pt(r_in, start), pt(r_in, end)
    mid = (r_out + r_in) / 2
    width = r_out - r_in
    tip = pt(mid, end + 32)
    head_out, head_in = pt(r_out + width * 0.9, end), pt(r_in - width * 0.9, end)
    return (f"M{fmt(o1[0])},{fmt(o1[1])} A{fmt(r_out)},{fmt(r_out)} 0 {large} 1 {fmt(o2[0])},{fmt(o2[1])} "
            f"L{fmt(head_out[0])},{fmt(head_out[1])} L{fmt(tip[0])},{fmt(tip[1])} "
            f"L{fmt(head_in[0])},{fmt(head_in[1])} L{fmt(i2[0])},{fmt(i2[1])} "
            f"A{fmt(r_in)},{fmt(r_in)} 0 {large} 0 {fmt(i1[0])},{fmt(i1[1])} Z")


def shuttle_outline(cx=8, cy=8, half_len=7.5, half_h=3.6) -> str:
    """The spola seen from above: a long body with pointed ends."""
    k = half_h * 1.33
    return (f"M{fmt(cx - half_len)},{fmt(cy)} "
            f"C{fmt(cx - half_len / 2)},{fmt(cy - k)} {fmt(cx + half_len / 2)},{fmt(cy - k)} {fmt(cx + half_len)},{fmt(cy)} "
            f"C{fmt(cx + half_len / 2)},{fmt(cy + k)} {fmt(cx - half_len / 2)},{fmt(cy + k)} {fmt(cx - half_len)},{fmt(cy)} Z")


def rounded_rect(x, y, w, h, r) -> str:
    return (f"M{fmt(x + r)},{fmt(y)} H{fmt(x + w - r)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + w)},{fmt(y + r)} "
            f"V{fmt(y + h - r)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + w - r)},{fmt(y + h)} H{fmt(x + r)} "
            f"A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x)},{fmt(y + h - r)} V{fmt(y + r)} "
            f"A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + r)},{fmt(y)} Z")


def symbolic_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    icons = {
        "": symbolic(shuttle_outline(8, 6.6, 7.6, 2.9) + " " + rounded_rect(4.9, 5.7, 6.2, 1.8, 0.9),
                     rounded_rect(1, 12, 14, 1.6, 0.8)),
        "synced": symbolic(disk + " " + poly([(3.9, 8.3), (5.3, 6.9), (7, 8.6), (10.7, 4.9), (12.1, 6.3), (7, 11.4)])),
        "syncing": symbolic(ring_arrow(8, 8, 7, 4.9, 200, 330), ring_arrow(8, 8, 7, 4.9, 20, 150)),
        "paused": symbolic(disk + " " + rect(5.2, 4.5, 2, 7) + " " + rect(8.8, 4.5, 2, 7)),
        "error": symbolic(disk + " " + rect(7, 3.6, 2, 5.6) + " " + circle(8, 11.4, 1.15)),
        "attention": symbolic(poly([(8, 0.6), (15.6, 14.6), (0.4, 14.6)]) + " " + rect(7.1, 5.2, 1.8, 5)
                              + " " + circle(8, 12.2, 1.05)),
    }
    return icons


APP_ICON = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="wood" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#e0a96d"/>
      <stop offset="0.55" stop-color="#b9783f"/>
      <stop offset="1" stop-color="#7d4a22"/>
    </linearGradient>
    <linearGradient id="thread" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#7fd0c4"/>
      <stop offset="1" stop-color="#1f8a7d"/>
    </linearGradient>
    <linearGradient id="tip" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#eeeeec"/>
      <stop offset="1" stop-color="#9a9996"/>
    </linearGradient>
  </defs>
  <!-- the thread, leaving the shuttle and weaving across -->
  <path d="M70 68 C 84 92, 96 98, 114 90" fill="none" stroke="#1f8a7d" stroke-width="4" stroke-linecap="round"/>
  <path d="M14 104 C 34 92, 52 112, 72 102 S 104 96, 118 106" fill="none" stroke="#5e5c64" stroke-opacity="0.35" stroke-width="3" stroke-linecap="round"/>
  <!-- shadow -->
  <ellipse cx="64" cy="80" rx="54" ry="5" fill="#000" fill-opacity="0.12"/>
  <!-- body -->
  <path d="M4 62 C 30 40, 98 40, 124 62 C 98 84, 30 84, 4 62 Z" fill="url(#wood)"/>
  <path d="M4 62 C 30 40, 98 40, 124 62 C 98 48, 30 48, 4 62 Z" fill="#fff" fill-opacity="0.2"/>
  <!-- metal tips -->
  <path d="M4 62 C 8 58, 12 56, 17 54.5 L 17 69.5 C 12 68, 8 66, 4 62 Z" fill="url(#tip)"/>
  <path d="M124 62 C 120 58, 116 56, 111 54.5 L 111 69.5 C 116 68, 120 66, 124 62 Z" fill="url(#tip)"/>
  <!-- cavity with the bobbin -->
  <rect x="36" y="53" width="56" height="18" rx="7" fill="#4a2c14"/>
  <rect x="41" y="55.5" width="46" height="13" rx="4.5" fill="url(#thread)"/>
  <g stroke="#14615a" stroke-opacity="0.45" stroke-width="2">
    <path d="M49 55.5 V68.5 M57 55.5 V68.5 M65 55.5 V68.5 M73 55.5 V68.5 M81 55.5 V68.5"/>
  </g>
  <rect x="38" y="60" width="52" height="4" rx="2" fill="#d9a066"/>
</svg>
"""


def main() -> None:
    sym = ROOT / "symbolic" / "apps"
    sym.mkdir(parents=True, exist_ok=True)
    for state, svg in symbolic_icons().items():
        name = f"{APP_ID}-{state}-symbolic.svg" if state else f"{APP_ID}-symbolic.svg"
        (sym / name).write_text(svg, encoding="utf-8")
    apps = ROOT / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"{APP_ID}.svg").write_text(APP_ICON, encoding="utf-8")
    print(f"icons written to {ROOT}")


if __name__ == "__main__":
    main()
