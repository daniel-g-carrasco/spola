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


def symbolic(*paths: str, cls: str = "") -> str:
    """`cls` is a GTK semantic colour class (success, warning, error): the
    file manager colours the emblem accordingly."""
    attr = f' class="{cls}"' if cls else ""
    body = "".join(f'<path{attr} fill="{FG}" fill-rule="evenodd" d="{p}"/>' for p in paths)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f"{body}</svg>\n")


def ring_arrow(cx, cy, r_out, r_in, start, end, tip_deg=32, head=0.9) -> str:
    """A ring segment from `start` to `end` degrees (clockwise, 0 = east)
    with an arrow head at `end`, its tip `tip_deg` further on; the head is
    wider than the ring by `head` times the ring's width on each side."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    large = 1 if (end - start) % 360 > 180 else 0
    o1, o2 = pt(r_out, start), pt(r_out, end)
    i1, i2 = pt(r_in, start), pt(r_in, end)
    mid = (r_out + r_in) / 2
    width = r_out - r_in
    tip = pt(mid, end + tip_deg)
    head_out, head_in = pt(r_out + width * head, end), pt(r_in - width * head, end)
    return (f"M{fmt(o1[0])},{fmt(o1[1])} A{fmt(r_out)},{fmt(r_out)} 0 {large} 1 {fmt(o2[0])},{fmt(o2[1])} "
            f"L{fmt(head_out[0])},{fmt(head_out[1])} L{fmt(tip[0])},{fmt(tip[1])} "
            f"L{fmt(head_in[0])},{fmt(head_in[1])} L{fmt(i2[0])},{fmt(i2[1])} "
            f"A{fmt(r_in)},{fmt(r_in)} 0 {large} 0 {fmt(i1[0])},{fmt(i1[1])} Z")


def circular_arrow(cx, cy, mid, width, start, end, head_width, head_length) -> str:
    """A ring segment `width` wide on the circle of radius `mid`, from `start`
    to `end` degrees (clockwise, 0 = east, y down), ending in an isosceles
    arrow head: its base is radial, centred on the ring, `head_width` long;
    its tip lies on the tangent at the end, `head_length` from the base, so
    both sides are equal (a tip on the ring's own circle would lean inwards)."""
    def pt(r, a):
        return cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))
    r_out, r_in = mid + width / 2, mid - width / 2
    large = 1 if (end - start) % 360 > 180 else 0
    o1, o2 = pt(r_out, start), pt(r_out, end)
    i1, i2 = pt(r_in, start), pt(r_in, end)
    base_out, base_in = pt(mid + head_width / 2, end), pt(mid - head_width / 2, end)
    a = math.radians(end)
    mx, my = pt(mid, end)
    tip = (mx - head_length * math.sin(a), my + head_length * math.cos(a))     # clockwise tangent
    return (f"M{fmt(o1[0])},{fmt(o1[1])} A{fmt(r_out)},{fmt(r_out)} 0 {large} 1 {fmt(o2[0])},{fmt(o2[1])} "
            f"L{fmt(base_out[0])},{fmt(base_out[1])} L{fmt(tip[0])},{fmt(tip[1])} "
            f"L{fmt(base_in[0])},{fmt(base_in[1])} L{fmt(i2[0])},{fmt(i2[1])} "
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


def sync_arrows() -> str:
    """The two arrows of "syncing", for the tray and the window: the app's
    trails with arrow heads instead of its balls. Heads large enough to read
    at 16 px (the first ones were lost in the tray): the ring sits closer to
    the centre to leave them room inside the disk."""
    return " ".join(circular_arrow(8, 8, 4.0, 1.9, start, end, head_width=4.7, head_length=3.3)
                    for start, end in ((30, 138), (210, 318)))


def emblem_sync_arrows() -> str:
    """The same arrows for the Files emblem, thinner: Files draws it small."""
    return " ".join(circular_arrow(8, 8, 4.25, 1.5, start, end, head_width=4.1, head_length=2.9)
                    for start, end in ((36, 146), (216, 326)))


GLYPHS = {
    "check": poly([(3.9, 8.3), (5.3, 6.9), (7, 8.6), (10.7, 4.9), (12.1, 6.3), (7, 11.4)]),
    "pause": rect(5.2, 4.5, 2, 7) + " " + rect(8.8, 4.5, 2, 7),
    "bang": rect(7, 3.6, 2, 5.6) + " " + circle(8, 11.4, 1.15),
    "triangle_bang": rect(7.1, 5.2, 1.8, 5) + " " + circle(8, 12.2, 1.05),
}
TRIANGLE = poly([(8, 0.6), (15.6, 14.6), (0.4, 14.6)])

# GNOME palette: colour only for the states that are news
COLORS = {"synced": "#26a269", "syncing": "#3584e4", "paused": "#77767b", "error": "#c01c28",
          "attention": "#e66100"}


def color_icon(shape: str, color: str, glyph: str) -> str:
    """A full-colour status badge: the coloured shape with a white glyph on
    top, readable on light and dark panels alike (no recolouring: the name
    has no -symbolic suffix)."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
            f'<path fill="{color}" d="{shape}"/><path fill="#ffffff" fill-rule="evenodd" d="{glyph}"/></svg>\n')


def color_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    return {
        "synced": color_icon(disk, COLORS["synced"], GLYPHS["check"]),
        "syncing": color_icon(disk, COLORS["syncing"], sync_arrows()),
        "paused": color_icon(disk, COLORS["paused"], GLYPHS["pause"]),
        "error": color_icon(disk, COLORS["error"], GLYPHS["bang"]),
        "attention": color_icon(TRIANGLE, COLORS["attention"], GLYPHS["triangle_bang"]),
    }


def symbolic_icons() -> dict[str, str]:
    disk = circle(8, 8, 7.5)
    icons = {
        "": app_symbolic(),
        "synced": symbolic(disk + " " + poly([(3.9, 8.3), (5.3, 6.9), (7, 8.6), (10.7, 4.9), (12.1, 6.3), (7, 11.4)])),
        # the app's motif in motion: its two trails, arrow heads instead of the balls,
        # cut out of the same disk as the other states; arcs shortened so a tip
        # never touches the start of the other trail
        "syncing": symbolic(disk + " " + sync_arrows()),
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
    slash = poly([(3.4, 11.2), (11.2, 3.4), (12.6, 4.8), (4.8, 12.6)])
    return {
        "synced": symbolic(disk + " " + poly([(3.9, 8.3), (5.3, 6.9), (7, 8.6), (10.7, 4.9), (12.1, 6.3), (7, 11.4)]),
                           cls="success"),
        "syncing": symbolic(disk + " " + emblem_sync_arrows()),
        "pending": symbolic(ring + " " + clock_hands(7.3, 4.2, 1.4, 7.3, 11.1)),
        "error": symbolic(disk + " " + rect(7, 3.6, 2, 5.6) + " " + circle(8, 11.4, 1.15), cls="error"),
        "conflict": symbolic(poly([(8, 0.6), (15.6, 14.6), (0.4, 14.6)]) + " " + rect(7.1, 5.2, 1.8, 5)
                             + " " + circle(8, 12.2, 1.05), cls="warning"),
        # two paths, not one: the bar's ends go into the ring, and with evenodd
        # in a single path the overlap would turn into a gap
        "excluded": symbolic(ring, slash),
    }


def rounded_rect(x, y, w, h, r) -> str:
    return (f"M{fmt(x + r)},{fmt(y)} L{fmt(x + w - r)},{fmt(y)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + w)},{fmt(y + r)} "
            f"L{fmt(x + w)},{fmt(y + h - r)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + w - r)},{fmt(y + h)} "
            f"L{fmt(x + r)},{fmt(y + h)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x)},{fmt(y + h - r)} "
            f"L{fmt(x)},{fmt(y + r)} A{fmt(r)},{fmt(r)} 0 0 1 {fmt(x + r)},{fmt(y)} Z")


def thick_polyline(points, width) -> str:
    """The outline of a polyline drawn `width` wide, with mitred joins and
    flat ends (symbolic icons cannot use strokes)."""
    half = width / 2

    def normal(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        return -dy / length, dx / length

    def side(sign):
        out = []
        for i, p in enumerate(points):
            if i == 0 or i == len(points) - 1:
                n = normal(points[0], points[1]) if i == 0 else normal(points[-2], points[-1])
                out.append((p[0] + sign * half * n[0], p[1] + sign * half * n[1]))
                continue
            n1, n2 = normal(points[i - 1], p), normal(p, points[i + 1])
            mx, my = n1[0] + n2[0], n1[1] + n2[1]
            scale = half / ((mx * n1[0] + my * n1[1]) or 1e-9)
            out.append((p[0] + sign * mx * scale, p[1] + sign * my * scale))
        return out
    return poly(side(1) + list(reversed(side(-1))))


def emblem_variants() -> dict[str, dict[str, str]]:
    """Heavier emblems to compare with the current ones in Files' grid,
    where the emblem box is dimmed (dim-label) and drawn at 16 px:
      bold:   no disk, the glyphs themselves thick and as large as the canvas;
      square: the current glyphs cut out of a rounded square, about a third
              more colour than the disk."""
    check_bold = thick_polyline([(1.6, 8.4), (5.9, 12.7), (14.4, 4.2)], 3.0)
    ring_bold = circle(8, 8, 7.6) + " " + circle(8, 8, 5.5)
    bold = {
        "synced": symbolic(check_bold, cls="success"),
        "syncing": symbolic(" ".join(ring_arrow(8, 8, 5.9, 3.7, start, end, tip_deg=28)
                                     for start, end in ((40, 160), (220, 340)))),
        "pending": symbolic(ring_bold + " " + clock_hands(7, 3.9, 2, 6.8, 11.3)),
        "error": symbolic(ring_bold + " " + rect(6.9, 3.8, 2.2, 5) + " " + circle(8, 11.2, 1.3), cls="error"),
        "conflict": symbolic(poly([(8, 0.4), (15.8, 15), (0.2, 15)]) + " " + rect(6.95, 5, 2.1, 5.2)
                             + " " + circle(8, 12.3, 1.2), cls="warning"),
        "excluded": symbolic(ring_bold + " " + poly([(3.6, 11), (11, 3.6), (12.4, 5), (5, 12.4)])),
    }
    square = rounded_rect(0.5, 0.5, 15, 15, 3.5)
    cut_ring = circle(8, 8, 5.4) + " " + circle(8, 8, 4)
    squares = {
        "synced": symbolic(square + " " + poly([(3.6, 8.3), (5.1, 6.8), (7, 8.7), (10.9, 4.8), (12.4, 6.3), (7, 11.7)]),
                           cls="success"),
        "syncing": symbolic(square + " " + " ".join(ring_arrow(8, 8, 5.2, 3.6, start, end, tip_deg=26)
                                                    for start, end in ((40, 160), (220, 340)))),
        "pending": symbolic(square + " " + cut_ring + " " + clock_hands(7.35, 5.2, 1.3, 7.4, 10.35)),
        "error": symbolic(square + " " + rect(6.9, 3.4, 2.2, 5.8) + " " + circle(8, 11.6, 1.25), cls="error"),
        "conflict": symbolic(poly([(8, 0.4), (15.8, 15), (0.2, 15)]) + " " + rect(6.95, 5, 2.1, 5.2)
                             + " " + circle(8, 12.3, 1.2), cls="warning"),
        "excluded": symbolic(square + " " + cut_ring + " " + poly([(4.4, 10.6), (10.6, 4.4), (11.6, 5.4), (5.4, 11.6)])),
    }
    return {"bold": bold, "square": squares}


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
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--emblem-variants":
        for variant, icons in emblem_variants().items():
            out = Path(sys.argv[2]) / variant
            out.mkdir(parents=True, exist_ok=True)
            for state, svg in icons.items():
                (out / f"emblem-spola-{state}-symbolic.svg").write_text(svg, encoding="utf-8")
            print(out)
    else:
        main()
