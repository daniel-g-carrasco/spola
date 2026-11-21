#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""build-aux/windows/<slug>.ico from the app icon's SVG: one PNG per size,
for the title bars, Explorer, the taskbar and the installer. The title bar
draws 16 pixels at 100% and 20 at 125%, and shrinks what it is given
without a filter: every size the system draws must be in the file. An
icon with thin lines needs a version redrawn for the small sizes
(SMALL_SVG); a tile with thick shapes reads at 16 pixels as it is.

    python3 tools/make_ico.py [OUT.ico]

The SVG is drawn by what the app already has: GdkPixbuf with librsvg in a
GTK app, Qt's QSvgRenderer (offscreen: no window, no screen) in a Qt app,
where GdkPixbuf is not in the venv; Pillow reads no SVG. The app's APP_ID
and SLUG are read from the one package under src/, without importing it.
"""

import ast
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMALL = (16, 20, 24)
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
# The SVG keeps the icon theme's margin (the family's disc, radius 240 of
# 256, or the square canvas, 448 of 512 with its side below), which on Linux
# sits well beside the other apps' icons. Windows shrinks the icon to 16-24
# pixels in the taskbar and the notification area, where a margin is only a
# smaller icon (13/11/2026: 22 pixels drawn in the taskbar against Chrome's
# 24): there the view is closed on what is drawn, a square round the drawing's
# box, whatever its shape (`windows_view`). Only the 512 master is closed in:
# a drawing with another viewBox (a small SVG drawn on the 16 pixel grid) is
# taken as it is.
MASTER_VIEW = 'viewBox="0 0 512 512"'


def package(root: Path) -> Path:
    """The app's package: the one folder under src/ with an __init__.py."""
    src = Path(root) / "src"
    found = sorted(p for p in src.iterdir() if (p / "__init__.py").is_file()) if src.is_dir() else []
    if len(found) != 1:
        raise SystemExit(f"{src}: one package expected, {len(found)} found")
    return found[0]


def identity(root: Path) -> dict[str, str]:
    """The names the package's __init__.py sets to a text (APP_ID, SLUG,
    TEXTDOMAIN...), read with ast: nothing of the app is imported."""
    tree = ast.parse((package(root) / "__init__.py").read_text(encoding="utf-8"))
    names: dict[str, str] = {}

    def value(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = value(node.left), value(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
                    part = value(part.value)
                elif isinstance(part, ast.Constant):
                    part = part.value
                else:
                    part = None
                if not isinstance(part, str):
                    return None
                parts.append(part)
            return "".join(parts)
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, found = node.targets[0].id, value(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, found = node.target.id, value(node.value)
        else:
            continue
        if found is not None:
            names[target] = found
    return names


def icon_paths(root: Path = ROOT) -> tuple[Path, Path]:
    """The app icon's SVG, and the version drawn for 16-24 pixels (used only when it is there)."""
    names = identity(root)
    svg = Path(root) / "data" / "icons" / "hicolor" / "scalable" / "apps" / f"{names['APP_ID']}.svg"
    return svg, Path(root) / "build-aux" / "windows" / f"{names['SLUG']}-small.svg"


def _gdk_pixbuf():
    """GdkPixbuf, or None where there is none (a Qt app's venv)."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return None
    return GdkPixbuf


def _drawn_box(svg: Path) -> tuple[int, int, int, int] | None:
    """Where the master SVG draws on its 512 pixel square: the box of the pixels not transparent (x0, y0, x1, y1),
    or None when nothing is drawn."""
    png = render_png(svg, 512)
    GdkPixbuf = _gdk_pixbuf()
    if GdkPixbuf is not None:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        pixbuf = loader.get_pixbuf()
        data, stride, n = pixbuf.get_pixels(), pixbuf.get_rowstride(), pixbuf.get_n_channels()
        alpha = lambda x, y: data[y * stride + n * x + 3] if n == 4 else 255  # noqa: E731
        width, height = pixbuf.get_width(), pixbuf.get_height()
    else:
        from PySide6.QtGui import QImage
        image = QImage.fromData(png, "PNG")
        alpha = lambda x, y: image.pixelColor(x, y).alpha()  # noqa: E731
        width, height = image.width(), image.height()
    xs, ys = [], []
    for y in range(height):
        row = [x for x in range(width) if alpha(x, y) > 0]
        if row:
            xs += (row[0], row[-1])
            ys.append(y)
    return (min(xs), min(ys), max(xs) + 1, max(ys) + 1) if xs else None


def windows_view(svg: Path) -> str:
    """The SVG's text with the view closed on what it draws, as Windows shows the icon: a square round the
    drawing's box, centred on it. The family's disc gives viewBox 16 16 480 480, the square canvas
    32 23 448 466 widened to a square. Anything but a 512 master is given back as it is."""
    text = Path(svg).read_text(encoding="utf-8")
    if MASTER_VIEW not in text:
        return text
    box = _drawn_box(svg)
    if box is None:
        return text
    x0, y0, x1, y1 = box
    side = max(x1 - x0, y1 - y0)
    left, top = (x0 + x1 - side) / 2, (y0 + y1 - side) / 2
    return text.replace(MASTER_VIEW, f'viewBox="{left:g} {top:g} {side:g} {side:g}"', 1)


def windows_pixbuf(svg: Path, size: int):
    """The icon at `size` pixels as Windows shows it: what is drawn to the edge (GdkPixbuf)."""
    GdkPixbuf = _gdk_pixbuf()
    loader = GdkPixbuf.PixbufLoader()
    loader.set_size(size, size)
    loader.write(windows_view(svg).encode("utf-8"))
    loader.close()
    return loader.get_pixbuf()


_app = None


def _qt_png(svg: Path, size: int, full_view: bool) -> bytes:
    global _app
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    if QGuiApplication.instance() is None:
        _app = QGuiApplication([sys.argv[0]])
    if full_view:
        renderer = QSvgRenderer(QByteArray(windows_view(svg).encode("utf-8")))
    else:
        renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError(f"{svg}: not an SVG Qt can draw")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def render_png(svg: Path, size: int, full_view: bool = False) -> bytes:
    """The SVG drawn on a transparent square of `size` pixels, as PNG bytes; `full_view`: as Windows shows it,
    what is drawn to the edge (windows_view)."""
    GdkPixbuf = _gdk_pixbuf()
    if GdkPixbuf is not None:
        if full_view:
            pixbuf = windows_pixbuf(svg, size)
        else:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), size, size)
        _ok, data = pixbuf.save_to_bufferv("png", [], [])
        return bytes(data)
    try:
        import PySide6  # noqa: F401
    except ImportError:
        raise SystemExit("an SVG renderer is needed: GdkPixbuf (PyGObject) or PySide6's QtSvg") from None
    return _qt_png(svg, size, full_view)


def ico(images: list[tuple[int, bytes]]) -> bytes:
    """An .ico holding each (size, PNG) as it is (Windows Vista and later read PNG entries)."""
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    return struct.pack("<HHH", 0, 1, len(images)) + entries + payload


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    argv = sys.argv[1:] if argv is None else argv
    svg, small_svg = icon_paths(root)
    out = Path(argv[0]) if argv else Path(root) / "build-aux" / "windows" / f"{identity(root)['SLUG']}.ico"
    images = [(size, render_png(small_svg if size in SMALL and small_svg.is_file() else svg, size, full_view=True))
              for size in SIZES]
    out.write_bytes(ico(images))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
