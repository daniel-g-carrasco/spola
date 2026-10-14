"""The icon in the Windows notification area, with its menu and the
notifications, through Shell_NotifyIcon (ctypes, no extra module).

The same interface as tray.TrayIcon (the D-Bus StatusNotifierItem of Linux
desktops), so the app drives both the same way. The menu is built when it
is opened, from the items of that moment: a click can never land on an item
that has moved. A notification is a balloon of the icon, which Windows 10
and 11 show as a toast; a click on it calls back.

The icon's window is an ordinary hidden window of the GTK thread: GTK's own
message loop delivers its messages, nothing else has to run.
"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from .tray import MenuItem  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_NULL, WM_DESTROY, WM_CONTEXTMENU, WM_USER = 0x0000, 0x0002, 0x007B, 0x0400
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK = WM_USER, WM_USER + 1, WM_USER + 5
CALLBACK_MESSAGE = WM_USER + 100
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x10, 0x80
NOTIFYICON_VERSION_4 = 4
NIIF_NONE, NIIF_RESPECT_QUIET_TIME = 0x00, 0x80
MF_STRING, MF_GRAYED, MF_POPUP, MF_SEPARATOR = 0x0000, 0x0001, 0x0010, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON = 49


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)


def ico_from_svg(svg: str, ico: Path) -> None:
    """An .ico with one PNG per size (Windows picks the one for the screen's
    scale), drawn from the SVG of the icon theme."""
    images = []
    for size in ICON_SIZES:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(svg, size, size)
        _ok, data = pixbuf.save_to_bufferv("png", [], [])
        images.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(payload))
        payload += png
    ico.parent.mkdir(parents=True, exist_ok=True)
    ico.write_bytes(header + entries + payload)


class WinTray:
    def __init__(self, app_id: str, title: str, on_activate: Callable[[], None],
                 icon_theme_path: str = "", cache: Path | None = None):
        self.app_id, self.title, self.on_activate = app_id, title, on_activate
        self.icon_theme_path = icon_theme_path
        self.cache = cache or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Spola" / "icons"
        self.items: list[MenuItem] = []
        self.icon_name = ""
        self.tooltip = title
        self._icons: dict[str, int] = {}
        self._on_balloon: Callable[[], None] | None = None
        self._hwnd = None
        self._added = False
        self._wndproc = WNDPROC(self._window_proc)      # kept: Windows calls it
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # -- the same interface as tray.TrayIcon ----------------------------------------

    def start(self, _connection=None) -> None:
        instance = kernel32.GetModuleHandleW(None)
        name = f"{self.app_id}.tray"
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc,
                         hInstance=instance, lpszClassName=name)
        user32.RegisterClassExW(ctypes.byref(wc))
        self._hwnd = user32.CreateWindowExW(0, name, self.title, 0, 0, 0, 0, 0, None, None, instance, None)
        self._add()

    def stop(self) -> None:
        if self._added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
            self._added = False
        for handle in self._icons.values():
            user32.DestroyIcon(handle)
        self._icons.clear()
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None

    def set_icon(self, name: str) -> None:
        if name != self.icon_name:
            self.icon_name = name
            self._modify(NIF_ICON)

    def set_tooltip(self, title: str, body: str) -> None:
        text = f"{title}\n{body}" if body and body != title else title
        if text != self.tooltip:
            self.tooltip = text
            self._modify(NIF_TIP | NIF_SHOWTIP)

    def set_menu(self, items: list[MenuItem]) -> None:
        self.items = items          # built into a menu when it is opened

    def notify(self, title: str, body: str, on_click: Callable[[], None] | None = None) -> None:
        """A toast from the icon; on_click when the user clicks it."""
        if not self._added:
            return
        self._on_balloon = on_click
        data = self._data(NIF_INFO)
        data.szInfoTitle = title[:63]
        data.szInfo = (body or " ")[:255]
        data.dwInfoFlags = NIIF_NONE | NIIF_RESPECT_QUIET_TIME
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # -- internals ------------------------------------------------------------------------

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self._hwnd, uID=1, uFlags=flags)
        if flags & NIF_MESSAGE:
            data.uCallbackMessage = CALLBACK_MESSAGE
        if flags & NIF_ICON:
            data.hIcon = self._icon(self.icon_name or self.app_id)
        if flags & NIF_TIP:
            data.szTip = self.tooltip[:127]
        return data

    def _add(self) -> None:
        data = self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        self._added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
        if self._added:
            data.uVersion = NOTIFYICON_VERSION_4
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))

    def _modify(self, flags: int) -> None:
        if self._added:
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(flags)))

    def _icon(self, name: str) -> int:
        """The HICON of an icon of the theme, through an .ico in the cache."""
        if name in self._icons:
            return self._icons[name]
        handle = 0
        svg = self._svg(name)
        if svg:
            ico = self.cache / f"{name}.ico"
            try:
                if not ico.exists() or ico.stat().st_mtime < os.stat(svg).st_mtime:
                    ico_from_svg(svg, ico)
                size = user32.GetSystemMetrics(SM_CXSMICON)
                handle = user32.LoadImageW(None, str(ico), IMAGE_ICON, size, size, LR_LOADFROMFILE) or 0
            except (OSError, GLib.Error):
                handle = 0
        self._icons[name] = handle
        return handle

    def _svg(self, name: str) -> str | None:
        if self.icon_theme_path:
            candidate = Path(self.icon_theme_path) / "hicolor" / "scalable" / "apps" / f"{name}.svg"
            if candidate.is_file():
                return str(candidate)
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        paintable = theme.lookup_icon(name, None, 64, 1, Gtk.TextDirection.NONE, 0)
        file = paintable.get_file() if paintable else None
        path = file.get_path() if isinstance(file, Gio.File) else None
        return path if path and path.endswith(".svg") else None

    def _window_proc(self, hwnd, message, wparam, lparam):
        if message == CALLBACK_MESSAGE:
            event = lparam & 0xFFFF
            if event in (NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONUP):
                GLib.idle_add(lambda: self.on_activate() and False)
            elif event in (WM_CONTEXTMENU, WM_RBUTTONUP):
                self._show_menu()
            elif event == NIN_BALLOONUSERCLICK and self._on_balloon is not None:
                callback = self._on_balloon
                GLib.idle_add(lambda: callback() and False)
            return 0
        if message == self._taskbar_created:
            self._add()                 # Explorer restarted: the icon again
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self) -> None:
        actions: dict[int, Callable[[], None]] = {}
        menus = []

        def build(items: list[MenuItem]) -> int:
            menu = user32.CreatePopupMenu()
            menus.append(menu)
            for item in items:
                if item.separator:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                label = item.label.replace("&", "&&")
                if item.children:
                    user32.AppendMenuW(menu, MF_POPUP, build(item.children), label)
                    continue
                command = len(actions) + 1
                if item.action is not None and item.enabled:
                    actions[command] = item.action
                    user32.AppendMenuW(menu, MF_STRING, command, label)
                else:
                    user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, label)
            return menu

        root = build(self.items)
        point = POINT()
        user32.GetCursorPos(ctypes.byref(point))
        user32.SetForegroundWindow(self._hwnd)      # else the menu does not close on a click elsewhere
        chosen = user32.TrackPopupMenu(root, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                       point.x, point.y, 0, self._hwnd, None)
        user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(root)        # with its submenus
        if chosen in actions:
            action = actions[chosen]
            GLib.idle_add(lambda: action() and False)
