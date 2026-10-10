#!/usr/bin/env python3
"""Generate Spola's icons into data/icons/hicolor:

  scalable/apps/<APP_ID>.svg                  the app icon, a cleaned copy of
                                              data/icons/source/spola-darkbg.svg
  symbolic/apps/<APP_ID>-symbolic.svg         its symbolic version
  symbolic/apps/<APP_ID>-<state>-symbolic.svg tray states: synced, syncing,
                                              paused, error, attention

The masters of the app icon are the Inkscape files in data/icons/source/
(spola-darkbg.svg for dark backgrounds, spola-lightbg.svg for light ones):
edit those, never the generated copy. The installed icon is the dark
background version, because the hicolor theme has no light/dark variants
and docks, the GNOME overview and Noctalia are dark.

Symbolic icons use filled shapes only, holes cut with fill-rule="evenodd":
GTK and the tray hosts recolour them by filling every path, so a stroke-only
outline would turn into a solid blob.

    python3 tools/make_icons.py
"""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

APP_ID = "io.github.daniel_g_carrasco.spola"
ICONS = Path(__file__).resolve().parent.parent / "data" / "icons"
ROOT = ICONS / "hicolor"
SOURCE = ICONS / "source"
SVG_NS = "http://www.w3.org/2000/svg"
APP_ICON_VIEWBOX = "0 0 512 512"
APP_ICON_SIZE = 128
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


def ring_arrow(cx, cy, r_out, r_in, start, end, tip_deg=32) -> str:
    """A ring segment from `start` to `end` degrees (clockwise, 0 = east)
    with an arrow head at `end`, its tip `tip_deg` further on."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    large = 1 if (end - start) % 360 > 180 else 0
    o1, o2 = pt(r_out, start), pt(r_out, end)
    i1, i2 = pt(r_in, start), pt(r_in, end)
    mid = (r_out + r_in) / 2
    width = r_out - r_in
    tip = pt(mid, end + tip_deg)
    head_out, head_in = pt(r_out + width * 0.9, end), pt(r_in - width * 0.9, end)
    return (f"M{fmt(o1[0])},{fmt(o1[1])} A{fmt(r_out)},{fmt(r_out)} 0 {large} 1 {fmt(o2[0])},{fmt(o2[1])} "
            f"L{fmt(head_out[0])},{fmt(head_out[1])} L{fmt(tip[0])},{fmt(tip[1])} "
            f"L{fmt(head_in[0])},{fmt(head_in[1])} L{fmt(i2[0])},{fmt(i2[1])} "
            f"A{fmt(r_in)},{fmt(r_in)} 0 {large} 0 {fmt(i1[0])},{fmt(i1[1])} Z")


def trail_and_ball(cx, cy, orbit, width, ball, start, end) -> str:
    """One closed outline for the symbolic motif: a ring sector of `width` on
    the circle of radius `orbit`, from `start` to `end` degrees (clockwise,
    0 = east, y down, as in the master), merged into the ball of radius
    `ball` centred on the orbit at `end`. Flat cap at `start`, like the
    master's butt-capped strokes. A single outline, so trail and ball never
    overlap (with evenodd an overlap would open a hole)."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    r_out, r_in = orbit + width / 2, orbit - width / 2
    phi_out = math.degrees(math.acos((r_out**2 + orbit**2 - ball**2) / (2 * r_out * orbit)))
    phi_in = math.degrees(math.acos((r_in**2 + orbit**2 - ball**2) / (2 * r_in * orbit)))
    p0, p1 = pt(r_out, start), pt(r_out, end - phi_out)
    p2, p3 = pt(r_in, end - phi_in), pt(r_in, start)
    large_out = 1 if end - phi_out - start > 180 else 0
    large_in = 1 if end - phi_in - start > 180 else 0
    return (f"M{fmt(p0[0])},{fmt(p0[1])} A{fmt(r_out)},{fmt(r_out)} 0 {large_out} 1 {fmt(p1[0])},{fmt(p1[1])} "
            f"A{fmt(ball)},{fmt(ball)} 0 1 1 {fmt(p2[0])},{fmt(p2[1])} "
            f"A{fmt(r_in)},{fmt(r_in)} 0 {large_in} 0 {fmt(p3[0])},{fmt(p3[1])} Z")


def app_symbolic() -> str:
    """The app icon at 16 px: the ball with the two small balls and their
    trails cut out. Proportions adapted to the pixel grid (a 1:1 scale would
    give trails under 1 px): trails 1.5 px, small balls r 2.25 on an orbit of
    4.25, same angles as the master (40-180 and 220-360 degrees)."""
    motif = " ".join(trail_and_ball(8, 8, 4.25, 1.5, 2.25, start, end) for start, end in ((40, 180), (220, 360)))
    return symbolic(circle(8, 8, 7.5) + " " + motif)


def symbolic_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    icons = {
        "": app_symbolic(),
        "synced": symbolic(disk + " " + poly([(3.9, 8.3), (5.3, 6.9), (7, 8.6), (10.7, 4.9), (12.1, 6.3), (7, 11.4)])),
        # the app's motif in motion: its two trails, arrow heads instead of the balls,
        # cut out of the same disk as the other states; arcs shortened so a tip
        # never touches the start of the other trail
        "syncing": symbolic(disk + " " + " ".join(ring_arrow(8, 8, 5, 3.5, start, end, tip_deg=26)
                                                  for start, end in ((40, 160), (220, 340)))),
        "paused": symbolic(disk + " " + rect(5.2, 4.5, 2, 7) + " " + rect(8.8, 4.5, 2, 7)),
        "error": symbolic(disk + " " + rect(7, 3.6, 2, 5.6) + " " + circle(8, 11.4, 1.15)),
        "attention": symbolic(poly([(8, 0.6), (15.6, 14.6), (0.4, 14.6)]) + " " + rect(7.1, 5.2, 1.8, 5)
                              + " " + circle(8, 12.2, 1.05)),
    }
    return icons


def clean_app_icon(source: Path) -> str:
    """The Inkscape master as a plain SVG: no editor namespaces, attributes,
    namedview, comments or ids, empty <defs> and attribute-less <g> layers
    unwrapped. The drawing itself (elements, styles, path data) is copied
    untouched, so it renders exactly like the master."""
    ET.register_namespace("", SVG_NS)
    root = ET.parse(source).getroot()     # the parser drops XML comments
    if root.get("viewBox") != APP_ICON_VIEWBOX:
        raise SystemExit(f"{source}: expected viewBox {APP_ICON_VIEWBOX!r}, found {root.get('viewBox')!r}")

    def clean(element: ET.Element) -> None:
        for name in list(element.attrib):
            if name == "id" or (name.startswith("{") and not name.startswith("{" + SVG_NS + "}")):
                del element.attrib[name]
        children = []
        for child in element:
            if not isinstance(child.tag, str) or not child.tag.startswith("{" + SVG_NS + "}"):
                continue                       # sodipodi:namedview, inkscape metadata...
            clean(child)
            local = child.tag.split("}")[1]
            if local == "defs" and len(child) == 0:
                continue
            if local == "g" and not child.attrib:
                children.extend(child)         # unwrap the Inkscape layer
                continue
            children.append(child)
        element[:] = children

    clean(root)
    attrs = {"width": str(APP_ICON_SIZE), "height": str(APP_ICON_SIZE), "viewBox": APP_ICON_VIEWBOX}
    for name in list(root.attrib):
        del root.attrib[name]
    root.attrib.update(attrs)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def main() -> None:
    sym = ROOT / "symbolic" / "apps"
    sym.mkdir(parents=True, exist_ok=True)
    for state, svg in symbolic_icons().items():
        name = f"{APP_ID}-{state}-symbolic.svg" if state else f"{APP_ID}-symbolic.svg"
        (sym / name).write_text(svg, encoding="utf-8")
    apps = ROOT / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"{APP_ID}.svg").write_text(clean_app_icon(SOURCE / "spola-darkbg.svg"), encoding="utf-8")
    print(f"icons written to {ROOT}")


if __name__ == "__main__":
    main()
