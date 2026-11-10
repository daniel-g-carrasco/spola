"""Two quirks of GTK 4.24 on Windows, worked around per window, and the
window otherwise left to Windows. Imported only on Windows; nothing here is
needed once GTK fixes them (GTK 4.24.1 has both; measured with real windows
on 2026-11-10, probe 13 of the Spola audit).

The maximized window and the menu. A window maximized by Windows (the title
bar's button, Win+Up, a double click on the title bar, a drag to the top
edge) goes back to its normal size when a popover opens (the main menu),
while GTK and Windows both still call it maximized: GTK falls back to its
default size. So while the window is maximized its default size is the size
it has, and the fall is to where it already stands; the normal size comes
back with the restore, and `normal_size` gives it to whoever saves the
window's state meanwhile.

The popover and the minimize. A popover open when the window is minimized
comes back without its grab: no click outside closes it, only Esc or one of
its items. Closing it at the minimize does nothing (it is hidden already);
closed on the resume, it is simply gone.

The maximize at the start. A window maximized by GTK (`maximize()`, as an app
does to come back as it was left) is restored wrong by Windows from then on:
the title bar's button, Win+Down or a drag leave it as big as the screen, a
floating window whose borders drag wrong. Maximized by Windows instead
(`maximize_at_start`), it restores right for good.

What is no longer here, and why. Until 2026-11-10 a maximize made by Windows
was made GTK's own, so that the menu would not shrink the window: that turned
every restore into the broken kind, and a second workaround put the size
back after it, acting after the user's drag. Tried side by side by Daniel,
the build with no workaround at all resized, maximized and dragged best; the
two quirks of the menu were all it lacked. A maximize that stays Windows' own
needs no repair.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from ctypes import wintypes

from gi.repository import GLib, Gtk

HOLD_AGAIN_MS = 250         # the maximized size is given to GTK at once, and once more for a slow first layout
RESUME_CLOSE_MS = (150, 600)    # after notify::suspended -> False: the popover is shown again by then; looked at twice
SW_MAXIMIZE = 3

# our own handle: argtypes set on ctypes.windll.user32 would reach every
# other user of that shared object (win32.py, the tray)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


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


def hwnd_of(window: Gtk.Window) -> int:
    """The window's HWND: from GDK where the binding gives it, else by title."""
    surface = window.get_surface()
    try:
        import gi
        gi.require_version("GdkWin32", "4.0")
        from gi.repository import GdkWin32
        return int(GdkWin32.Win32Surface.get_handle(surface))
    except Exception:       # no typelib, no method in this GTK: the title is the next best thing
        return int(_user32.FindWindowW(None, window.get_title() or "") or 0)


def normal_size(window: Gtk.Window) -> tuple[int, int]:
    """The size to remember for the window: the one it had before it was
    maximized, not the maximized one `watch` lends to GTK meanwhile."""
    kept = getattr(window, "size_before_maximize", None)
    return tuple(kept) if kept else tuple(window.get_default_size())


def maximize_at_start(window: Gtk.Window) -> None:
    """The window maximized as it first shows, by Windows and not by GTK."""
    def on_map(w: Gtk.Window) -> None:
        w.disconnect(handler[0])
        hwnd = hwnd_of(w)
        if hwnd:
            _user32.ShowWindow(hwnd, SW_MAXIMIZE)
        else:               # no handle to ask Windows with: GTK's own maximize, as before
            w.maximize()
    handler = [window.connect("map", on_map)]


def watch(window: Gtk.Window) -> None:
    """Work around both quirks for this window, for as long as it lives."""
    def hold(w: Gtk.Window) -> bool:
        # while maximized, the default size is the size the window has
        if w.is_maximized() and w.get_width() > 0 and w.get_height() > 0:
            if tuple(w.get_default_size()) != (w.get_width(), w.get_height()):
                w.set_default_size(w.get_width(), w.get_height())
        return False

    def on_maximized(w: Gtk.Window, _pspec) -> None:
        if w.is_maximized():
            if getattr(w, "size_before_maximize", None) is None:
                w.size_before_maximize = tuple(w.get_default_size())
            GLib.idle_add(hold, w)                      # once GTK has the maximized size
            GLib.timeout_add(HOLD_AGAIN_MS, hold, w)
        elif getattr(w, "size_before_maximize", None) is not None:
            width, height = w.size_before_maximize
            w.size_before_maximize = None
            w.set_default_size(width, height)

    def on_suspended(w: Gtk.Window, _pspec) -> None:
        if not w.get_property("suspended"):
            for delay in RESUME_CLOSE_MS:
                GLib.timeout_add(delay, lambda: close_open_popovers(w) and False)

    window.size_before_maximize = None
    window.connect("notify::maximized", on_maximized)
    window.connect("notify::suspended", on_suspended)
    if window.is_maximized():
        on_maximized(window, None)
