#!/usr/bin/env python3
"""Generate Spola's icons into data/icons/hicolor:

  scalable/apps/<APP_ID>.svg                  the app icon, a cleaned copy of
                                              data/icons/source/spola-darkbg.svg
  symbolic/apps/<APP_ID>-symbolic.svg         its symbolic version
  symbolic/apps/<APP_ID>-<state>-symbolic.svg tray states: synced, syncing,
                                              paused, error, attention
  scalable/apps/<APP_ID>-status-<state>.svg   the same states in colour: the
                                              tray when there is news, the
                                              status at the top of the window
  scalable/emblems/emblem-spola-<state>-symbolic.svg
                                              Files (Nautilus) emblems: synced,
                                              syncing, pending, error, conflict,
                                              excluded; installed on the host by
                                              the app with the Files extension

The masters of the app icon are the Inkscape files in data/icons/source/
(spola-darkbg.svg for dark backgrounds, spola-lightbg.svg for light ones):
edit those, never the generated copy. They follow the common style of my
logos (the Lucciola one): a 512 px canvas, a disk of r=240 with no outline
and a vertical two-stop gradient lit from above (ivory #f3f1ea -> #e0ddd4
for dark backgrounds, dark #333333 -> #1a1a1a for light ones), the subject
in the complementary gradient at fill-opacity 0.9 (the stops stay opaque),
gradients named sfumatura-* in bounding-box units, layers "Sfondo" and
"Soggetto", every shape filled (the trails are filled outlines joined to the
balls, no strokes). The installed icon is the dark background version,
because the hicolor theme has no light/dark variants and docks, the GNOME
overview and Noctalia are dark.

The status icons in colour follow the same style: the disk (or the warning
triangle) in a vertical gradient from the GNOME colour lightened by 24 per
channel down to the colour itself, the glyph plain ivory (#f3f1ea) at
fill-opacity 0.9 (graded, it turned grey on the colour). The glyphs: outer shapes rounded (the triangle's corners
have radius 1.4), inner symbols with sharp corners and thin (check, bars
and exclamation mark 1.7 px, arrows 1.6, 1.3 on the emblems), each one
outline without overlaps.

Symbolic icons use filled shapes only, holes cut with fill-rule="evenodd":
GTK and the tray hosts recolour them by filling every path, so a stroke-only
outline would turn into a solid blob.

    python3 tools/make_icons.py
"""

import math
import re
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


def symbolic(*paths: str, cls: str = "", fill: str = FG) -> str:
    """`cls` is a GTK semantic colour class (success, warning, error): the
    file manager colours the emblem accordingly. GTK and GNOME Shell recolour
    every path from the theme and ignore `fill`; renderers that do not
    (Dolphin, Qt trays) show `fill` as it is."""
    attr = f' class="{cls}"' if cls else ""
    body = "".join(f'<path{attr} fill="{fill}" fill-rule="evenodd" d="{p}"/>' for p in paths)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f"{body}</svg>\n")


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


# The glyphs at 16 px, drawn once (by hand, in Inkscape terms): outer shapes
# rounded, inner symbols sharp and thin; no glyph overlaps itself, so each
# works with fill-rule="evenodd" cut out of a disk as much as filled on top.
GLYPHS = {
    "check": "M3.5,8.9 L6.93,12.34 L12.53,6.07 L11.27,4.93 L6.87,9.86 L4.7,7.7 Z",
    "pause": "M5.4,4.5 L7.1,4.5 L7.1,11.5 L5.4,11.5 Z M8.9,4.5 L10.6,4.5 L10.6,11.5 L8.9,11.5 Z",
    "bang": "M7.15,3.6 L8.85,3.6 L8.85,9.2 L7.15,9.2 Z M6.95,11.4 A1.05,1.05 0 1 1 9.05,11.4 A1.05,1.05 0 1 1 6.95,11.4 Z",
    "triangle_bang": "M7.2,5.3 L8.8,5.3 L8.8,10.1 L7.2,10.1 Z M7.05,12.2 A0.95,0.95 0 1 1 8.95,12.2 "
                     "A0.95,0.95 0 1 1 7.05,12.2 Z",
}
# the warning triangle, corners of radius 1.4
TRIANGLE = ("M9.23,3.12 L14.25,12.22 A1.4,1.4 0 0 1 13.03,14.3 L2.97,14.3 A1.4,1.4 0 0 1 1.75,12.22 "
            "L6.77,3.12 A1.4,1.4 0 0 1 9.23,3.12 Z")
# "syncing": the app's trails with arrow heads instead of its balls, 1.6 px
SYNC_ARROWS = ("M12.16,10.4 A4.8,4.8 0 0 1 4.43,11.21 L3.47,12.08 L3.02,8.45 L6.59,9.27 L5.62,10.14 "
               "A3.2,3.2 0 0 0 10.77,9.6 Z M3.84,5.6 A4.8,4.8 0 0 1 11.57,4.79 L12.53,3.92 L12.98,7.55 "
               "L9.41,6.73 L10.38,5.86 A3.2,3.2 0 0 0 5.23,6.4 Z")
# the same for the Files emblem, 1.3 px: Files draws it small
EMBLEM_SYNC_ARROWS = ("M11.96,10.88 A4.9,4.9 0 0 1 3.94,10.74 L2.9,11.44 L2.97,8.14 L6.05,9.31 L5.02,10.01 "
                      "A3.6,3.6 0 0 0 10.91,10.12 Z M4.04,5.12 A4.9,4.9 0 0 1 12.06,5.26 L13.1,4.56 "
                      "L13.03,7.86 L9.95,6.69 L10.98,5.99 A3.6,3.6 0 0 0 5.09,5.88 Z")

# GNOME palette: colour only for the states that are news
COLORS = {"synced": "#26a269", "syncing": "#3584e4", "paused": "#77767b", "error": "#c01c28",
          "attention": "#e66100"}


GLYPH = "#f3f1ea"       # the ivory of the logos, plain: graded, it turned grey on the colour


def lighter(color: str, step: int = 24) -> str:
    """`color` lightened by `step` on each channel: the top of its gradient."""
    r, g, b = (min(255, int(color[i:i + 2], 16) + step) for i in (1, 3, 5))
    return f"#{r:02x}{g:02x}{b:02x}"


def gradient(gid: str, top: str, bottom: str) -> str:
    return (f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
            f'<stop offset="1" stop-color="{bottom}"/></linearGradient>')


def color_icon(state: str, shape: str, glyph: str) -> str:
    """A full-colour status badge in the style of the logos: the shape in a
    vertical gradient of its GNOME colour, the glyph plain ivory at 0.9 on top,
    readable on light and dark panels alike (no recolouring: the name has
    no -symbolic suffix)."""
    color = COLORS[state]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16"><defs>'
            f'{gradient(f"s-{state}", lighter(color), color)}</defs>'
            f'<path fill="url(#s-{state})" d="{shape}"/>'
            f'<path fill="{GLYPH}" fill-opacity="0.9" fill-rule="evenodd" d="{glyph}"/></svg>\n')


def color_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    return {
        "synced": color_icon("synced", disk, GLYPHS["check"]),
        "syncing": color_icon("syncing", disk, SYNC_ARROWS),
        "paused": color_icon("paused", disk, GLYPHS["pause"]),
        "error": color_icon("error", disk, GLYPHS["bang"]),
        "attention": color_icon("attention", TRIANGLE, GLYPHS["triangle_bang"]),
    }


def symbolic_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    icons = {
        "": app_symbolic(),
        "synced": symbolic(disk + " " + GLYPHS["check"]),
        # the app's motif in motion: its two trails, arrow heads instead of the balls,
        # cut out of the same disk as the other states
        "syncing": symbolic(disk + " " + SYNC_ARROWS),
        "paused": symbolic(disk + " " + GLYPHS["pause"]),
        "error": symbolic(disk + " " + GLYPHS["bang"]),
        "attention": symbolic(TRIANGLE + " " + GLYPHS["triangle_bang"]),
    }
    return icons


def clean_app_icon(source: Path) -> str:
    """The Inkscape master as a plain SVG: no editor namespaces, attributes,
    namedview or comments, no ids but those of what <defs> holds (the
    gradients the shapes point to with url(#sfumatura-...): without them the
    icon would come out black), empty <defs> dropped and attribute-less <g>
    layers unwrapped. The drawing itself (elements, styles, path data) is
    copied untouched, so it renders exactly like the master."""
    ET.register_namespace("", SVG_NS)
    root = ET.parse(source).getroot()     # the parser drops XML comments
    if root.get("viewBox") != APP_ICON_VIEWBOX:
        raise SystemExit(f"{source}: expected viewBox {APP_ICON_VIEWBOX!r}, found {root.get('viewBox')!r}")

    def clean(element: ET.Element, in_defs: bool = False) -> None:
        for name in list(element.attrib):
            if (name == "id" and not in_defs) or (name.startswith("{") and not name.startswith("{" + SVG_NS + "}")):
                del element.attrib[name]
        children = []
        for child in element:
            if not isinstance(child.tag, str) or not child.tag.startswith("{" + SVG_NS + "}"):
                continue                       # sodipodi:namedview, inkscape metadata...
            local = child.tag.split("}")[1]
            # the <defs> element's own id goes; the ids of what it holds stay
            clean(child, in_defs=in_defs or element.tag == "{" + SVG_NS + "}defs")
            if local == "defs" and len(child) == 0:
                continue
            if local == "g" and not child.attrib:
                children.extend(child)         # unwrap the Inkscape layer
                continue
            children.append(child)
        element[:] = children

    clean(root)
    text = ET.tostring(root, encoding="unicode")
    kept = set(re.findall(r'id="([^"]+)"', text))
    if (orphans := set(re.findall(r"url\(#([^)]+)\)", text)) - kept):
        raise SystemExit(f"{source}: references without a definition: {sorted(orphans)}")
    attrs = {"width": str(APP_ICON_SIZE), "height": str(APP_ICON_SIZE), "viewBox": APP_ICON_VIEWBOX}
    for name in list(root.attrib):
        del root.attrib[name]
    root.attrib.update(attrs)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def clock_hands(x, top, width, y, right) -> str:
    """The hands at 12 and 3 o'clock as one L-shaped outline: two
    overlapping rectangles would leave a dot where they cross (evenodd).
    The vertical hand starts at (x, top), the horizontal one at height y
    and ends at `right`; both are `width` thick."""
    return poly([(x, top), (x + width, top), (x + width, y), (right, y), (right, y + width), (x, y + width)])


def emblem_icons() -> dict[str, str]:
    """Emblems fill the whole 16 px canvas: Files draws them small and dim."""
    disk = circle(8, 8, 7.5)
    ring = disk + " " + circle(8, 8, 5)
    # the bar about as thick as the ring (2.4 across, the ring 2.5): thinner, it looked frail
    slash = poly([(3.25, 11.05), (11.05, 3.25), (12.75, 4.95), (4.95, 12.75)])
    # the literal colours, for Dolphin: readable on light and dark views
    grey = "#77767b"
    return {
        "synced": symbolic(disk + " " + GLYPHS["check"], cls="success", fill=COLORS["synced"]),
        "syncing": symbolic(disk + " " + EMBLEM_SYNC_ARROWS, fill=COLORS["syncing"]),
        # a full disk like the other states, the hands cut out (1.7, as the other glyphs)
        "pending": symbolic(disk + " " + clock_hands(7.15, 3.8, 1.7, 7.15, 11.6), fill=grey),
        "error": symbolic(disk + " " + GLYPHS["bang"], cls="error", fill=COLORS["error"]),
        "conflict": symbolic(TRIANGLE + " " + GLYPHS["triangle_bang"], cls="warning", fill=COLORS["attention"]),
        # two paths, not one: the bar's ends go into the ring, and with evenodd
        # in a single path the overlap would turn into a gap
        "excluded": symbolic(ring, slash, fill=grey),
    }


def write_pngs(svg: Path, sizes=(64, 128, 256)) -> None:
    """PNG copies of the app icon for the sizes the desktop and the Flatpak
    repository page use (rendered with librsvg through GdkPixbuf)."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    for size in sizes:
        out = ROOT / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), size, size).savev(str(out), "png", [], [])


def main() -> None:
    sym = ROOT / "symbolic" / "apps"
    sym.mkdir(parents=True, exist_ok=True)
    for state, svg in symbolic_icons().items():
        name = f"{APP_ID}-{state}-symbolic.svg" if state else f"{APP_ID}-symbolic.svg"
        (sym / name).write_text(svg, encoding="utf-8")
    emblems = ROOT / "scalable" / "emblems"
    emblems.mkdir(parents=True, exist_ok=True)
    for state, svg in emblem_icons().items():
        (emblems / f"emblem-spola-{state}-symbolic.svg").write_text(svg, encoding="utf-8")
    apps = ROOT / "scalable" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    for state, svg in color_icons().items():
        (apps / f"{APP_ID}-status-{state}.svg").write_text(svg, encoding="utf-8")
    (apps / f"{APP_ID}.svg").write_text(clean_app_icon(SOURCE / "spola-darkbg.svg"), encoding="utf-8")
    write_pngs(apps / f"{APP_ID}.svg")
    print(f"icons written to {ROOT}")


if __name__ == "__main__":
    main()
