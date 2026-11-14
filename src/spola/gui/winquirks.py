"""Two quirks of GTK 4.24 on Windows, worked around per window, and the
window otherwise left to Windows: the main one and the dialogs' (below). Imported only on Windows; nothing here is
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

The dialogs. The main window is GTK's, not libadwaita's (it has the system's
title bar), so a dialog has nothing to open inside and libadwaita gives it a
window, with an empty title bar of its own: GTK then draws the frame, the
shadow and the resize borders, and GDK moves and resizes the window by hand
on each motion of the pointer, badly from a corner. Its header bar, meant to
sit under a title bar, drew its hairline right under the window's edge, and
the window opened at the screen's corner when its parent had just been
shown. `watch_dialog` takes that title bar away before the window exists for
Windows: the frame, the title bar, the resizing and the snapping are then
Windows' own, as for the main window; the header bar loses the close button
the title bar has, and the title the title bar says already (`title_once`);
the window opens centred on its parent. An alert has no
header bar and nothing to resize: it is only centred.

In front when asked from outside. Windows lets a process put its window in
front only if it is the one in front or was just allowed to by it. A click
on File Explorer's menu starts a second Spola.exe, which may: before it
hands the request over it allows it to the others (`allow_front`), and the
running Spola then puts the window it opens in front (`to_front`).
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from ctypes import wintypes

from gi.repository import Adw, GLib, Gtk

HOLD_AGAIN_MS = 250         # the maximized size is given to GTK at once, and once more for a slow first layout
RESUME_CLOSE_MS = (150, 600)    # after notify::suspended -> False: the popover is shown again by then; looked at twice
SW_MAXIMIZE = 3
ASFW_ANY = 0xFFFFFFFF       # AllowSetForegroundWindow: any process
GWL_STYLE, WS_MINIMIZEBOX = -16, 0x00020000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0004, 0x0010, 0x0020
MONITOR_DEFAULTTONEAREST = 2

# our own handle: argtypes set on ctypes.windll.user32 would reach every
# other user of that shared object (win32.py, the tray)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.IsIconic.argtypes = [wintypes.HWND]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]


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


# -- the dialogs' windows -------------------------------------------------------------

def header_bars_below(widget: Gtk.Widget) -> Iterator[Adw.HeaderBar]:
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Adw.HeaderBar):
            yield child
        yield from header_bars_below(child)
        child = child.get_next_sibling()


def title_once(bar: Adw.HeaderBar, window_title: str) -> None:
    """The window's title is in the system's title bar: a header bar under it
    does not say it again. One that would show it by itself shows no title;
    one with the title and a second line (the file's name under "Share")
    keeps that line alone, as its title. Other titles (a page's own, in a
    dialog with several pages) stay."""
    if not window_title:
        return
    widget = bar.get_title_widget()
    if widget is None:
        page = bar.get_ancestor(Adw.NavigationPage)
        if (page.get_title() if page is not None else window_title) == window_title:
            bar.set_show_title(False)
    elif isinstance(widget, Adw.WindowTitle) and widget.get_title() == window_title and widget.get_subtitle():
        widget.set_title(widget.get_subtitle())
        widget.set_subtitle("")


def centred(inner: tuple[int, int], outer: tuple[int, int, int, int],
            area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Where a window of size `inner` goes to sit in the middle of `outer`
    (left, top, right, bottom) without leaving `area`; its top edge, with the
    title bar, stays inside whatever its size."""
    width, height = inner
    x = (outer[0] + outer[2] - width) // 2
    y = (outer[1] + outer[3] - height) // 2
    x = max(area[0], min(x, area[2] - width))
    y = max(area[1], min(y, area[3] - height))
    return x, y


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def centre_on_parent(window: Gtk.Window) -> bool:
    """The window moved to the middle of the one it belongs to, inside that
    one's screen (the work area, without the taskbar). False when there is
    nothing to centre on (no parent shown, or a minimized one)."""
    parent = window.get_transient_for()
    if parent is None or not parent.get_mapped():
        return False
    hwnd, owner = hwnd_of(window), hwnd_of(parent)
    if not hwnd or not owner or _user32.IsIconic(owner):
        return False
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not _user32.GetMonitorInfoW(_user32.MonitorFromWindow(owner, MONITOR_DEFAULTTONEAREST), ctypes.byref(info)):
        return False
    left, top, right, bottom = _rect(hwnd)
    work = info.rcWork
    x, y = centred((right - left, bottom - top), _rect(owner), (work.left, work.top, work.right, work.bottom))
    _user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    return True


def watch_dialog(window: Gtk.Window) -> None:
    """A window libadwaita opens for a dialog: Windows' own frame and title
    bar where it has a header bar, and in the middle of its parent (see the
    module's notes). For every new toplevel: it does nothing to the others."""
    def on_child(w: Gtk.Window, _pspec) -> None:
        dialog = w.get_child()
        if not isinstance(dialog, Adw.Dialog) or getattr(w, "dialog_watched", False):
            return
        w.dialog_watched = True
        framed = not isinstance(dialog, Adw.AlertDialog) and w.get_titlebar() is not None
        if framed:
            w.set_titlebar(None)        # with GTK_CSD=0: the system's title bar and frame

        def on_map(mapped: Gtk.Window) -> None:
            if framed:
                for bar in header_bars_below(mapped):       # the title bar has the close button
                    bar.set_show_start_title_buttons(False)
                    bar.set_show_end_title_buttons(False)
                    title_once(bar, mapped.get_title() or "")
                hwnd = hwnd_of(mapped)
                style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
                if hwnd and style & WS_MINIMIZEBOX:
                    # a dialog is not minimized by itself: its parent would stay behind, not answering
                    _user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~WS_MINIMIZEBOX)
                    _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0,
                                         SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            if not getattr(mapped, "dialog_placed", False):
                mapped.dialog_placed = centre_on_parent(mapped)
        w.connect("map", on_map)
    window.connect("notify::child", on_child)


def allow_front() -> None:
    """Called by a Spola.exe a click has just started (File Explorer's menu,
    the Start menu): the Spola already running, which gets the request, may
    put its window in front."""
    _user32.AllowSetForegroundWindow(ASFW_ANY)


def to_front(widget: Gtk.Widget) -> None:
    """The window `widget` is in (a dialog's own, once it has one), in front
    of the other programs' windows: now if it is shown, else as it shows."""
    def front(*_args) -> bool:
        root = widget.get_root()
        hwnd = hwnd_of(root) if isinstance(root, Gtk.Window) and root.get_surface() is not None else 0
        if hwnd:
            _user32.SetForegroundWindow(hwnd)
        return False
    if widget.get_mapped():
        front()
    else:
        handler = []

        def on_map(w: Gtk.Widget) -> None:
            w.disconnect(handler[0])
            GLib.idle_add(front)        # once the window is on the screen
        handler.append(widget.connect("map", on_map))
