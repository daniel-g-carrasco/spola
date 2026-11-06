"""Three quirks of GTK 4.24 on Windows, worked around per window: a maximize
made by Windows that a popover then undoes, a restore made by Windows that
leaves the window at the maximized size, and a popover that comes back from
a minimize without its grab. Imported only on Windows; nothing here is
needed once GTK fixes them (GTK 4.24.1 has all three).

The maximize (measured 2026-11-06 in P7M Extractor): a window maximized by
Windows (the title bar's button, Win+Up, a double click on the title bar)
is put back to its normal size by the first popover that opens (the main
menu), while GTK and Windows both still call it maximized: a floating
window with the maximized state. Maximized by GTK, `maximize()`, it keeps
its size. So a maximize that Windows made is made GTK's own at once.

The restore (probe8 and probe11 of the review in spola-audit/prove/
gtk-windows): after `maximize()` from GTK, a restore made by Windows (the
title bar's button, Win+Down, a double click on the title bar) leaves the
window as big as the screen, a floating window, while GTK keeps its surface
at the maximized size and `get_default_size` at the old one; from there
`SetWindowPos` and `set_default_size` do nothing and a drag of a border
goes wrong (GTK issue 7695, fixed in 4.20 for the path through GTK, not for
this one). Done again through GTK, `maximize()` then `unmaximize()`, the
window gets its remembered size back.

The popover: GTK hides an open popover when the window is minimized and
shows it again when the window comes back, without the grab that closes it
on a click outside; only Escape or one of its items closes it. Closed on
the resume, it is simply gone.
"""

from __future__ import annotations

from collections.abc import Iterator

from gi.repository import GLib, Gtk

RESTORE_CHECK_MS = 80       # after notify::maximized -> False: Windows' resize has landed by then
UNMAXIMIZE_MS = 60          # between the maximize() and the unmaximize() that put the size back
RESUME_CLOSE_MS = 150       # after notify::suspended -> False: the popover is shown again by then
SLACK = 20                  # logical pixels: a surface larger than the remembered size by more is Windows' doing


def popovers_below(widget: Gtk.Widget) -> Iterator[Gtk.Popover]:
    """Every popover in the widget tree below `widget` (a menu button's, a
    popover menu's, an entry's completion...)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover):
            yield child
        yield from popovers_below(child)
        child = child.get_next_sibling()


def close_open_popovers(window: Gtk.Window) -> int:
    """Close the popovers shown in the window; how many."""
    closed = 0
    for popover in popovers_below(window):
        if popover.get_visible():
            popover.popdown()
            closed += 1
    return closed


def watch(window: Gtk.Window) -> None:
    """Connect the workarounds to the window (once, when it is created)."""
    remembered: dict[str, object] = {}

    def on_maximized(w: Gtk.Window, _pspec) -> None:
        if w.is_maximized():
            if not remembered.get("own"):
                # made by Windows, or by GTK: either way, GTK's own from now on,
                # or the first popover puts the window back to its normal size
                remembered["own"] = True
                remembered["size"] = w.get_default_size()
                w.maximize()
            return
        remembered.pop("own", None)
        size = remembered.get("size")
        if size is None:
            return

        def check() -> bool:
            surface = w.get_surface()
            if w.is_maximized() or surface is None or remembered.get("size") != size:
                return False
            width, height = size
            if surface.get_width() > width + SLACK or surface.get_height() > height + SLACK:
                # Windows' restore left the maximized size in place: through GTK it works
                remembered.pop("size", None)
                w.maximize()
                GLib.timeout_add(UNMAXIMIZE_MS, lambda: (w.unmaximize(), False)[1])
            return False
        GLib.timeout_add(RESTORE_CHECK_MS, check)

    def on_suspended(w: Gtk.Window, _pspec) -> None:
        if not w.get_property("suspended"):
            GLib.timeout_add(RESUME_CLOSE_MS, lambda: (close_open_popovers(w), False)[1])

    window.connect("notify::maximized", on_maximized)
    window.connect("notify::suspended", on_suspended)
