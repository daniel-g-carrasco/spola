"""Screenshot helper for Files (Nautilus), loaded only by tools/screenshots.sh
from a throw-away extensions folder: after a delay it draws the Files window
at a given scale into a PNG and quits Files. Never installed for users."""

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gio, GLib, Graphene, Gtk  # noqa: E402

SHOT = os.environ.get("SPOLA_NAUTILUS_SHOT")
DELAY = float(os.environ.get("SPOLA_NAUTILUS_SHOT_DELAY", "8"))
SCALE = float(os.environ.get("SPOLA_NAUTILUS_SHOT_SCALE", "2"))


def take():
    windows = [w for w in Gtk.Window.list_toplevels() if w.get_visible() and w.get_width() > 300]
    if windows:
        window = windows[0]
        width, height = window.get_width(), window.get_height()
        snapshot = Gtk.Snapshot()
        snapshot.scale(SCALE, SCALE)
        Gtk.WidgetPaintable.new(window).snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is not None:
            area = Graphene.Rect().init(0, 0, width * SCALE, height * SCALE)
            window.get_renderer().render_texture(node, area).save_to_png(SHOT)
    app = Gio.Application.get_default()
    if app is not None:
        app.quit()
    return False


if SHOT:
    GLib.timeout_add(int(DELAY * 1000), take)
