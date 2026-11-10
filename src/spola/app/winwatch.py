"""The change monitor of a pair's folder on Windows: one ReadDirectoryChangesW
over the whole tree, in a thread of its own. Imported only on Windows.

Gio.FileMonitor watches one folder at a time, and on Windows each monitor
holds a buffer of half a megabyte (GWin32FileMonitor: 531 KB measured): a
pair of 2,700 folders took 1.5 GB of memory and as many handles. Windows
watches a subtree natively, with one handle and one buffer, and has no
budget to run out of.

What changed is told as a path relative to the root with "/", as the rest
of Spola writes it (the exclusions, the states), at once and at the main
loop's default priority: what rclone does here during a run must reach the
engine before the end of the run (told with idle_add from the runner's
thread), or it would be taken for a change to sync back. Names that come
while the main loop is busy are told together. When Windows had more than
the buffer holds, the root itself ("") is told: a run then looks at
everything.
"""

from __future__ import annotations

import ctypes
import struct
import threading
from collections.abc import Callable
from ctypes import wintypes

from gi.repository import GLib

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4            # read, write, delete: the folder stays free to change
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000     # a folder
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# names, sizes and writes; not attributes: the sync states Spola sets in
# File Explorer (cloudfiles.py) are attributes, and would come back as changes
NOTIFY_FILTER = 0x0001 | 0x0002 | 0x0008 | 0x0010
WAIT_OBJECT_0, INFINITE = 0, 0xFFFFFFFF
ERROR_OPERATION_ABORTED, ERROR_NOTIFY_ENUM_DIR = 995, 1022


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(OVERLAPPED), ctypes.c_void_p]
kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                            wintypes.DWORD]
kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD


def names_in(data: bytes) -> set[str]:
    """The paths of FILE_NOTIFY_INFORMATION records, with "/"."""
    names, offset = set(), 0
    while offset + 12 <= len(data):
        following, _action, length = struct.unpack_from("<III", data, offset)
        names.add(data[offset + 12:offset + 12 + length].decode("utf-16-le", "replace").replace("\\", "/"))
        if not following:
            break
        offset += following
    return names


class WinTreeWatcher:
    BUFFER_BYTES = 64 * 1024        # the most a network share takes
    RETRY_S = (5, 15, 30, 60)       # a folder that cannot be watched: tried again after these, then every minute

    def __init__(self, root: str, on_change: Callable[[str], None],
                 ignore: Callable[[str], bool] = lambda rel: False, budget: int | None = None):
        self.root = root
        self.on_change = on_change
        self.ignore = ignore
        self.overflow = False               # never: the whole tree is watched
        self._thread: threading.Thread | None = None
        self._stop_event = None
        self._stopped = True
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    @property
    def count(self) -> int:
        return 1 if self._thread is not None and self._thread.is_alive() else 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event = kernel32.CreateEventW(None, True, False, None)
        self._stopped = False
        self._thread = threading.Thread(target=self._run, args=(self._stop_event,), name="spola-watch",
                                        daemon=True)
        self._thread.start()

    def _open(self):
        handle = kernel32.CreateFileW(self.root, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None, OPEN_EXISTING,
                                      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OVERLAPPED, None)
        return None if handle in (None, INVALID_HANDLE_VALUE) else handle

    def _run(self, stop_event) -> None:
        """Watch until stopped. A folder that cannot be watched (gone, its
        drive taken away, no longer readable) is tried again, every few
        seconds and then every minute; when it can be watched again the
        root is told, so that a run looks at what changed meanwhile. Before,
        the thread ended at the first error and the pair was left to the
        periodic check until Spola started again, without a word."""
        failures = 0
        while True:
            handle = self._open()
            if handle is not None:
                if failures:
                    self._tell({""})
                failures = 0
                if self._watch(handle, stop_event):
                    return
            delay = self.RETRY_S[min(failures, len(self.RETRY_S) - 1)]
            failures += 1
            if kernel32.WaitForSingleObject(stop_event, int(delay * 1000)) == WAIT_OBJECT_0:
                return

    def stop(self) -> None:
        self._stopped = True
        if self._thread is None:
            return
        kernel32.SetEvent(self._stop_event)
        self._thread.join()                 # at once: the read is cancelled
        kernel32.CloseHandle(self._stop_event)
        self._thread = self._stop_event = None

    def _watch(self, handle, stop_event) -> bool:
        """The changes under the root, until stopped (True) or until the
        folder cannot be watched any more (False, the root told)."""
        io_event = kernel32.CreateEventW(None, True, False, None)
        buffer = ctypes.create_string_buffer(self.BUFFER_BYTES)
        overlapped = OVERLAPPED(hEvent=io_event)
        waiting = (wintypes.HANDLE * 2)(io_event, stop_event)
        got = wintypes.DWORD()
        try:
            while True:
                kernel32.ResetEvent(io_event)
                if not kernel32.ReadDirectoryChangesW(handle, buffer, len(buffer), True, NOTIFY_FILTER,
                                                      None, ctypes.byref(overlapped), None):
                    self._tell({""})        # the folder is gone, or no longer readable: a run will say
                    return False
                if kernel32.WaitForMultipleObjects(2, waiting, False, INFINITE) != WAIT_OBJECT_0:
                    kernel32.CancelIoEx(handle, ctypes.byref(overlapped))
                    kernel32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(got), True)
                    return True
                if not kernel32.GetOverlappedResult(handle, ctypes.byref(overlapped), ctypes.byref(got), False):
                    error = ctypes.get_last_error()
                    if error == ERROR_OPERATION_ABORTED:
                        return True
                    self._tell({""})
                    if error == ERROR_NOTIFY_ENUM_DIR:
                        continue            # more than the buffer held: told, and on
                    return False
                # nothing in the buffer: more than it held
                self._tell(names_in(buffer.raw[:got.value]) if got.value else {""})
        finally:
            kernel32.CloseHandle(handle)
            kernel32.CloseHandle(io_event)

    def _tell(self, names: set[str]) -> None:
        with self._lock:
            first = not self._pending
            self._pending |= names
        if first:
            GLib.idle_add(self._flush, priority=GLib.PRIORITY_DEFAULT)

    def _flush(self) -> bool:
        with self._lock:
            names, self._pending = self._pending, set()
        for rel in sorted(names):
            if self._stopped:
                break
            if not rel or not self.ignore(rel):
                self.on_change(rel)
        return False
