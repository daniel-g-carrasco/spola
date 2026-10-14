#!/usr/bin/env python3
"""build-aux/windows/spola.ico from the app icon's SVG: one PNG per size,
for Explorer, the taskbar and the installer (GdkPixbuf with librsvg)."""

import struct
import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "io.github.daniel_g_carrasco.spola.svg"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build-aux" / "windows" / "spola.ico"
    images = []
    for size in SIZES:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(SVG), size, size)
        _ok, data = pixbuf.save_to_bufferv("png", [], [])
        images.append((size, bytes(data)))
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    out.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + payload)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
