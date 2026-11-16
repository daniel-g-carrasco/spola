# SPDX-License-Identifier: GPL-3.0-or-later
"""What the app needs from Windows itself, through ctypes: the title bars
in the content's colour, the executable's icon on every window, the files
that would run when opened. Imported only on Windows.

Every function sets the argtypes and restype of what it calls: without
them ctypes passes and returns C ints, and a HANDLE (64 bit) comes back
truncated to 32, a mistake that shows only now and then.
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from ctypes import wintypes


# -- the title bar -----------------------------------------------------------------

DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR = 35, 36      # Windows 11 (build 22000) and later
GWL_STYLE, WS_CAPTION = -16, 0x00C00000             # a window with a title bar (not a popover's surface)


class MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


def colorref(rgb: tuple[int, int, int]) -> int:
    """(red, green, blue), each 0-255, as Windows writes a colour: 0x00BBGGRR."""
    red, green, blue = (max(0, min(255, int(part))) for part in rgb)
    return red | green << 8 | blue << 16


def title_bars(dark: bool, background: tuple[int, int, int] | None = None,
               text: tuple[int, int, int] | None = None) -> None:
    """The system title bars of this process's windows as the content is:
    dark or light (Windows 10 1809 and later: the buttons' glyphs follow
    it), and on Windows 11 in the content's very colours, `background` and
    `text` as (red, green, blue), so that the bar and the window read as
    one surface. Where Windows does not know the colour attributes
    (Windows 10) the dark or light bar stays; ignored elsewhere."""
    # our own handles: argtypes set on ctypes.windll's shared objects would
    # reach every other module that calls the same functions
    user32, dwmapi = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("dwmapi")
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.POINTER(MARGINS)]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    pid, value = os.getpid(), ctypes.c_int(1 if dark else 0)
    colours = [(attribute, wintypes.DWORD(colorref(rgb)))
               for attribute, rgb in ((DWMWA_CAPTION_COLOR, background), (DWMWA_TEXT_COLOR, text)) if rgb is not None]

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            for attribute in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE, then its 1809 number
                if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            for attribute, colour in colours:
                dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(colour), ctypes.sizeof(colour))
            # one pixel of DWM's frame under the content, at the top. When
            # Windows stretches the window (a system DPI aware process on a
            # screen whose scale is not the main one's, as the app was until
            # 2026-11-13) the seam between the title bar and the content
            # showed what was behind the window
            if user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION:
                dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(0, 0, 1, 0)))
        return True

    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.EnumWindows(visit, 0)


# -- the screen's dpi ---------------------------------------------------------------

PER_MONITOR_AWARE_V2 = -4               # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Windows 10 1703)
PROCESS_PER_MONITOR_DPI_AWARE = 2       # Windows 8.1's per monitor, and DPI_AWARENESS_PER_MONITOR_AWARE


def per_monitor_dpi() -> bool:
    """The process follows each screen's dpi (per monitor v2, else Windows
    8.1's per monitor), as the manifest asks for the built exe. Called
    before the toolkit opens the display, it makes a run from the sources
    behave as the exe (without it GTK's GDK makes the process system DPI
    aware). False when it was set already (the manifest) or Windows does
    not know it."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        return bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(PER_MONITOR_AWARE_V2)))
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        return shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0
    except (AttributeError, OSError):
        return False


def per_monitor_aware() -> bool:
    """This thread's windows follow each screen's dpi (per monitor, v1 or v2);
    False when system aware or unaware (Windows stretches the window)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user32.GetThreadDpiAwarenessContext.argtypes = []
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        context = user32.GetThreadDpiAwarenessContext()
        return user32.GetAwarenessFromDpiAwarenessContext(context) == PROCESS_PER_MONITOR_DPI_AWARE
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.GetProcessDpiAwareness.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        awareness = ctypes.c_int()
        return (shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness)) == 0
                and awareness.value == PROCESS_PER_MONITOR_DPI_AWARE)
    except (AttributeError, OSError):
        return False


def window_dpi(hwnd: int) -> int:
    """The dpi of the screen the window is on, as Windows draws it (240 at
    250%); 0 when unknown (no window, Windows before 10 1607)."""
    if not hwnd:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
    except AttributeError:
        return 0
    return int(user32.GetDpiForWindow(hwnd))


SM_CXICON, SM_CXSMICON = 11, 49
ICON_SMALL, ICON_BIG, WM_SETICON = 0, 1, 0x0080
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
_icons: dict[tuple[str, int], int] = {}     # kept for the life of the process: a window does not copy its icon


def _exe_icon(exe: str, size: int) -> int:
    """The exe's own icon at this size (its .ico holds 16, 20, 24 ... 256,
    the small ones drawn for small sizes); 0 when there is none."""
    if (exe, size) not in _icons:
        handles, ids = (wintypes.HICON * 1)(), (wintypes.UINT * 1)()
        got = ctypes.windll.user32.PrivateExtractIconsW(exe, 0, size, size, handles, ids, 1, 0)
        _icons[(exe, size)] = (handles[0] or 0) if got == 1 else 0
    return _icons[(exe, size)]


def _file_icon(ico: str, size: int) -> int:
    """The icon of an .ico file at this size; 0 when it cannot be read."""
    if (ico, size) not in _icons:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE
        _icons[(ico, size)] = user32.LoadImageW(None, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE) or 0
    return _icons[(ico, size)]


def window_icons(exe: str, ico: str = "") -> None:
    """This process's windows with the exe's icon, at the sizes Windows draws
    for each window's DPI (the title bar's small one; the big one, which
    Alt+Tab and the taskbar button draw: measured on Windows 11, a window
    with a red small icon and a blue big one has a blue taskbar button).
    GTK gives Windows a 48-pixel icon, which the title bar shrinks badly to
    16. `ico`, when given, is an .ico file that takes the exe's place (a
    run from the sources, which has no exe of its own)."""
    user32 = ctypes.windll.user32
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            dpi = user32.GetDpiForWindow(hwnd) or 96
            for kind, metric in ((ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)):
                size = user32.GetSystemMetricsForDpi(metric, dpi)
                icon = _file_icon(ico, size) if ico else _exe_icon(exe, size) if exe else 0
                if icon:
                    user32.SendMessageW(hwnd, WM_SETICON, kind, icon)
        return True

    user32.EnumWindows(visit, 0)


# -- a file that runs when it is opened ---------------------------------------------

# What Windows runs, rather than shows, when such a file is opened: programs,
# scripts, installers, shortcuts, settings files. AssocIsDangerous gives the
# system's own list, which policies can change; these always count.
RUNS_WHEN_OPENED = frozenset((
    ".exe", ".com", ".scr", ".pif", ".cpl", ".msc", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".mst", ".appx", ".msix", ".lnk", ".url", ".scf",
    ".reg", ".inf", ".jar", ".application", ".appref-ms", ".gadget", ".chm", ".library-ms",
    ".settingcontent-ms", ".diagcab", ".xbap", ".website",
))


def runs_when_opened(path: str) -> bool:
    """Opening this file would run it (ShellExecute): a program, a script, a
    shortcut. "Fattura.pdf.exe" counts by its last extension, as Windows
    reads it. An app that opens files it did not make (downloads, files
    shared by others) shows such a file in its folder instead."""
    ext = os.path.splitext(path)[1].lower()
    if not ext:
        return False
    if ext in RUNS_WHEN_OPENED:
        return True
    try:
        shlwapi = ctypes.WinDLL("shlwapi")
        shlwapi.AssocIsDangerous.argtypes = [wintypes.LPCWSTR]
        shlwapi.AssocIsDangerous.restype = wintypes.BOOL
        return bool(shlwapi.AssocIsDangerous(ext))
    except (AttributeError, OSError):
        return False


# -- a file on another computer -----------------------------------------------------

DRIVE_REMOTE = 4


def on_network(path: str) -> bool:
    """The file is on another computer: a UNC path, or a drive letter
    mapped to a share (GetDriveTypeW says DRIVE_REMOTE). What an app
    writes for such a file can go to the computer's own folders instead:
    a shared server's folder belongs to everybody."""
    full = os.path.abspath(path)
    sep = os.sep
    if full.startswith(sep + sep):
        return True
    drive = os.path.splitdrive(full)[0]
    if not drive:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    return kernel32.GetDriveTypeW(drive + sep) == DRIVE_REMOTE


# -- a folder chosen by the Windows shell -------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16), ("data3", ctypes.c_uint16),
                ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_FILE_OPEN_DIALOG = _GUID.parse("dc1c5a9c-e88a-4dde-a5a1-60f82a20aef7")
_IID_FILE_OPEN_DIALOG = _GUID.parse("d57c7288-d4ad-4768-be02-9d969532d960")
_IID_SHELL_ITEM = _GUID.parse("43826d1e-e718-42ee-bc55-a1e261c37bfe")
_IID_OLE_WINDOW = _GUID.parse("00000114-0000-0000-C000-000000000046")
_CANCELLED = 0x800704C7


def _com_checked(hr):
    if hr & 0x80000000:
        code = hr & 0xffffffff
        # Win32 HRESULTs carry the system error code in the low word.
        native_code = code & 0xffff if (code >> 16) & 0x1fff == 7 else code
        message = ctypes.FormatError(native_code).strip()
        raise OSError(f"{message} (0x{code:08X})")


def _com_method(pointer, slot, *types, result=ctypes.c_long):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *types)(table[slot])


def _com_release(pointer):
    if pointer:
        _com_method(pointer, 2, result=ctypes.c_ulong)(pointer)


_SHELL_LINK = _GUID.parse("00021401-0000-0000-C000-000000000046")
_IID_SHELL_LINK_W = _GUID.parse("000214F9-0000-0000-C000-000000000046")
_IID_PERSIST_FILE = _GUID.parse("0000010b-0000-0000-C000-000000000046")
_RPC_E_CHANGED_MODE = 0x80010106


def shortcut(target: str, link: str) -> None:
    """A shortcut (.lnk) at `link` to the file `target`, made by the shell
    (IShellLinkW::SetPath, IPersistFile::Save): what Explorer makes with
    "Create shortcut", for a name in any language. OSError when it cannot
    be made. COM is set up for this call's thread and left as it was."""
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    started = ole.CoInitializeEx(None, 2)       # STA; a thread already in another apartment keeps it
    if started < 0 and started & 0xffffffff != _RPC_E_CHANGED_MODE:
        _com_checked(started)
    made, persist = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _com_checked(ole.CoCreateInstance(ctypes.byref(_SHELL_LINK), None, 1, ctypes.byref(_IID_SHELL_LINK_W),
                                          ctypes.byref(made)))
        _com_checked(_com_method(made, 20, wintypes.LPCWSTR)(made, target))                 # SetPath
        _com_checked(_com_method(made, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            made, ctypes.byref(_IID_PERSIST_FILE), ctypes.byref(persist)))
        _com_checked(_com_method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(persist, link, True))   # Save
    finally:
        _com_release(persist)
        _com_release(made)
        if started >= 0:
            ole.CoUninitialize()


def choose_folder_native(title: str, initial: str | None = None, owner: int = 0,
                         cancelled: threading.Event | None = None) -> str | None:
    """Run on a dedicated worker: path on accept, None on cancel, OSError on failure.

    The worker owns every COM interface and memory allocation for its complete
    lifetime. The integer owner HWND may be obtained by the window's main thread.
    https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-imodalwindow-show
    """
    ole = ctypes.WinDLL("ole32")
    shell = ctypes.WinDLL("shell32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_GUID),
                                                ctypes.POINTER(ctypes.c_void_p)]
    shell.SHCreateItemFromParsingName.restype = ctypes.c_long

    dialog, initial_item, selected = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    name = ctypes.c_void_p()
    timer = 0
    timer_errors = []
    close_posted = False
    ole_window = ctypes.c_void_p()
    user = ctypes.WinDLL("user32", use_last_error=True)
    timer_proc = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD)
    user.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, timer_proc]
    user.SetTimer.restype = ctypes.c_size_t
    user.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user.KillTimer.restype = wintypes.BOOL
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.PostMessageW.restype = wintypes.BOOL

    @timer_proc
    def on_timer(_hwnd, _message, _timer, _time):
        nonlocal close_posted
        if cancelled is not None and cancelled.is_set() and not close_posted:
            try:
                handle = wintypes.HWND()
                _com_checked(_com_method(ole_window, 3, ctypes.POINTER(wintypes.HWND))(ole_window, ctypes.byref(handle)))
                if handle:
                    # Close() from a TIMERPROC returns S_OK without closing on
                    # Windows 11. IOleWindow identifies our own dialog without
                    # searching by title/class; posting lets its modal loop exit.
                    if not user.PostMessageW(handle, 0x0010, 0, 0):
                        raise ctypes.WinError(ctypes.get_last_error())
                    close_posted = True
            except OSError as error:
                timer_errors.append(error)

    # A dedicated worker has no previously selected incompatible apartment.
    _com_checked(ole.CoInitializeEx(None, 2 | 4))  # STA, disable obsolete OLE1 DDE
    try:
        _com_checked(ole.CoCreateInstance(ctypes.byref(_FILE_OPEN_DIALOG), None, 1,
                                      ctypes.byref(_IID_FILE_OPEN_DIALOG), ctypes.byref(dialog)))
        _com_checked(_com_method(dialog, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
            dialog, ctypes.byref(_IID_OLE_WINDOW), ctypes.byref(ole_window)))
        flags = wintypes.DWORD()
        _com_checked(_com_method(dialog, 10, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(flags)))
        # Folder, filesystem, existing path, unchanged CWD, no Recent Items entry.
        options = flags.value | 0x20 | 0x40 | 0x800 | 0x8 | 0x02000000
        _com_checked(_com_method(dialog, 9, wintypes.DWORD)(dialog, options))
        _com_checked(_com_method(dialog, 17, wintypes.LPCWSTR)(dialog, title))
        if initial and os.path.isdir(initial):
            _com_checked(shell.SHCreateItemFromParsingName(initial, None, ctypes.byref(_IID_SHELL_ITEM),
                                                       ctypes.byref(initial_item)))
            _com_checked(_com_method(dialog, 12, ctypes.c_void_p)(dialog, initial_item))
        if cancelled is not None:
            if cancelled.is_set():
                return None
            timer = user.SetTimer(None, 0, 100, on_timer)
            if not timer:
                raise ctypes.WinError(ctypes.get_last_error())
        answer = _com_method(dialog, 3, wintypes.HWND)(dialog, owner or None)
        if timer_errors:
            raise timer_errors[0]
        if answer & 0xffffffff == _CANCELLED:
            return None
        _com_checked(answer)
        _com_checked(_com_method(dialog, 20, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(selected)))
        if not selected:
            raise OSError("Windows returned no selected folder.")
        _com_checked(_com_method(selected, 5, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            selected, ctypes.c_int(0x80058000), ctypes.byref(name)))  # SIGDN_FILESYSPATH
        if not name:
            raise OSError("Windows returned no filesystem path for the folder.")
        return ctypes.wstring_at(name)
    finally:
        if timer:
            user.KillTimer(None, timer)
        if name:
            ole.CoTaskMemFree(name)
        _com_release(ole_window)
        _com_release(selected)
        _com_release(initial_item)
        _com_release(dialog)
        ole.CoUninitialize()
