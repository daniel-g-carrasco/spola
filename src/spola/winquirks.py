"""Three quirks of GTK 4.24 on Windows, worked around per window: a maximize
made by Windows that a popover then undoes, a restore made by Windows that
leaves the window at the maximized size, and a popover that comes back from
a minimize without its grab. Imported only on Windows; nothing here is
needed once GTK fixes them (GTK 4.24.1 has all three).

The maximize (measured 2026-11-06 in P7M Extractor): a window maximized by
Windows (the title bar's button, Win+Up, a double click on the title bar,
a drag to the top edge) is put back to its normal size by the first popover
that opens (the main menu), while GTK and Windows both still call it
maximized: a floating window with the maximized state. Maximized by GTK,
`maximize()`, it keeps its size. So a maximize that Windows made is made
GTK's own.

The restore (probe8, probe11 and probe12 of the review in spola-audit/
prove/gtk-windows): after `maximize()` from GTK, a restore made by Windows
(the title bar's button, Win+Down, a double click on the title bar, the
title bar dragged down) leaves the window as big as the screen, a floating
window, while GTK keeps its surface at the maximized size and
`get_default_size` at the old one; from there `SetWindowPos` and
`set_default_size` do nothing and a drag of a border goes wrong (GTK issue
7695, fixed in 4.20 for the path through GTK, not for this one). Done again
through GTK, `maximize()` then `unmaximize()`, the window gets its
remembered size back.

Both are done only once Windows is finished with the window: not during a
move or a resize (a restore by dragging the title bar down happens in the
middle of one), nor with the mouse button still held. The size that decides
is the window's own (GetWindowRect), not the surface's, which lags behind
during an interactive move. And the restore is put right once per restore:
a second look that still finds the window big gives up, rather than
maximize and restore again and again (a window that pulsed, 2026-11-06).

The popover: GTK hides an open popover when the window is minimized and
shows it again when the window comes back, without the grab that closes it
on a click outside; only Escape or one of its items closes it. Closed on
the resume, it is simply gone.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Iterator
from ctypes import wintypes

from gi.repository import GLib, Gtk

SETTLE_MS = 100             # between looks at whether Windows is done with the window
SETTLE_TRIES = 50           # at most 5 s of waiting for a move or a drag to end
UNMAXIMIZE_MS = 60          # between the maximize() and the unmaximize() that put the size back
JUDGE_MS = 300              # after Windows is done: GTK's own last word on the size has landed by then
RESUME_CLOSE_MS = 150       # after notify::suspended -> False: the popover is shown again by then
SLACK = 20                  # logical pixels: a window larger than the remembered size by more is Windows' doing

# our own handles: argtypes set on ctypes.windll.user32 would reach every
# other user of that shared object (win32.py, the tray) with our RECT type
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
GUI_INMOVESIZE = 0x2
VK_LBUTTON = 0x01


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", RECT)]


_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
_user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short


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


def windows_is_busy_with(window: Gtk.Window) -> bool:
    """A move or a resize of the window in progress (Windows' own loop), or
    the mouse button still down (a drag about to end)."""
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if _user32.GetGUIThreadInfo(_kernel32.GetCurrentThreadId(), ctypes.byref(info)) and info.flags & GUI_INMOVESIZE:
        return True
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def window_pixels(window: Gtk.Window) -> tuple[int, int]:
    """The window's size as Windows has it (pixels), (0, 0) if unknown."""
    hwnd = hwnd_of(window)
    rect = RECT()
    if not hwnd or not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def when_settled(window: Gtk.Window, then: Callable[[], None]) -> None:
    """Run `then` once Windows is done with the window (at once if it is)."""
    tries = {"left": SETTLE_TRIES}

    def look() -> bool:
        tries["left"] -= 1
        if windows_is_busy_with(window) and tries["left"] > 0:
            return True
        then()
        return False
    GLib.timeout_add(SETTLE_MS, look)


def oversized(window: Gtk.Window, size: tuple[int, int]) -> bool:
    """Is the window, as Windows has it, well beyond `size` (logical pixels)?"""
    surface = window.get_surface()
    if surface is None:
        return False
    scale = surface.get_scale_factor() or 1
    width, height = size
    pixels = window_pixels(window)
    return pixels[0] > (width + SLACK) * scale or pixels[1] > (height + SLACK) * scale


def watch(window: Gtk.Window) -> None:
    """Connect the workarounds to the window (once, when it is created)."""
    # own: the maximize in force is GTK's own; size: the normal size to put
    # back after a restore; fixing: a maximize() + unmaximize() of ours is
    # under way (its own notifications are not Windows' doing)
    state: dict[str, object] = {}

    def on_maximized(w: Gtk.Window, _pspec) -> None:
        if w.is_maximized():
            if state.get("own") or state.get("fixing"):
                return
            # made by Windows, or by GTK: GTK's own from now on, or the first
            # popover puts the window back to its normal size
            state["own"] = True
            state["size"] = w.get_default_size()

            def make_own() -> None:
                if w.is_maximized() and state.get("own") and not state.get("fixing"):
                    w.maximize()
            when_settled(w, make_own)
            return
        state.pop("own", None)
        if state.get("fixing"):
            return
        size = state.get("size")
        if size is None:
            return

        def check() -> None:
            if w.is_maximized() or state.get("fixing") or state.get("size") != size:
                return
            state.pop("size", None)             # one try per restore: no pulsing
            GLib.timeout_add(JUDGE_MS, judge)

        def judge() -> bool:
            if w.is_maximized() or state.get("fixing") or not oversized(w, size):
                return False
            # Windows' restore left the maximized size in place: through GTK it works
            state["fixing"] = True
            w.maximize()

            def back() -> bool:
                w.unmaximize()
                GLib.timeout_add(SETTLE_MS, lambda: (state.pop("fixing", None), False)[1])
                return False
            GLib.timeout_add(UNMAXIMIZE_MS, back)
            return False
        when_settled(w, check)

    def on_suspended(w: Gtk.Window, _pspec) -> None:
        if not w.get_property("suspended"):
            GLib.timeout_add(RESUME_CLOSE_MS, lambda: (close_open_popovers(w), False)[1])

    window.connect("notify::maximized", on_maximized)
    window.connect("notify::suspended", on_suspended)
