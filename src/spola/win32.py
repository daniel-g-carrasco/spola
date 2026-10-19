"""What Spola needs from Windows itself, through ctypes and winreg: the lock
on a pair, a graceful stop of rclone, the app password in the Credential
Manager, the start at login. Imported only on Windows.

The graceful stop. On Linux Spola sends rclone SIGINT, and bisync finishes
or cancels its transfers and saves its state. On Windows the same comes from
a console control event (Go delivers Ctrl+Break as os.Interrupt), which only
reaches a process attached to the same console. Spola from a terminal (the
CLI) starts rclone in its own process group on that console and sends it
Ctrl+Break. The window has no console: rclone gets one of its own, without
a window, and Spola attaches to it for the moment it takes to send the event.
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import signal
import struct
import subprocess
import time
import winreg
from ctypes import wintypes
from dataclasses import dataclass

from . import APP_ID, APP_NAME

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

CTRL_BREAK_EVENT = 1

kernel32.GetConsoleWindow.restype = wintypes.HWND
kernel32.GetConsoleCP.restype = wintypes.UINT
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]


# -- the title bar -----------------------------------------------------------------

def dark_title_bars(dark: bool) -> None:
    """The system title bars of this process's windows, dark or light, as
    the content is (Windows 10 1809 and later; ignored elsewhere)."""
    user32, dwmapi = ctypes.windll.user32, ctypes.windll.dwmapi
    pid, value = os.getpid(), ctypes.c_int(1 if dark else 0)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            for attribute in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE, then its 1809 number
                if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        return True

    user32.EnumWindows(visit, 0)


# -- the lock on a pair ----------------------------------------------------------

def lock(fd: int) -> bool:
    """An exclusive lock on the file's first byte, released when the process
    ends however it ends. False if another process holds it."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# -- rclone as a child -------------------------------------------------------------

def has_console() -> bool:
    """Attached to a console, with a window or not (a terminal, a service
    runner); the window built without one has none."""
    return bool(kernel32.GetConsoleCP())


def popen_options() -> dict:
    """How to start rclone so that interrupt() can reach it. Without a
    console of ours, rclone gets one without a window (CREATE_NO_WINDOW):
    a new console with its window hidden is shown all the same where
    Windows Terminal hosts the consoles (the default since Windows 11
    22H2), flashing at every run."""
    if has_console():
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}


def interrupt(proc: subprocess.Popen) -> bool:
    """Ctrl+Break to rclone: the graceful stop. False if it could not be sent
    (the caller then stops the process)."""
    if getattr(proc, "spola_shared_console", False):
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return True
        except OSError:
            return False
    if not kernel32.AttachConsole(proc.pid):
        return False
    try:
        kernel32.SetConsoleCtrlHandler(None, True)      # the event is not for Spola
        return bool(kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, proc.pid))
    finally:
        kernel32.FreeConsole()
        kernel32.SetConsoleCtrlHandler(None, False)


# -- the app password, in the Credential Manager ---------------------------------------

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
advapi32.CredFree.argtypes = [ctypes.c_void_p]


def _target(user: str) -> str:
    return f"{APP_ID}:{user}"


def credential_store(user: str, password: str) -> None:
    blob = password.encode("utf-16-le")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL(Type=CRED_TYPE_GENERIC, TargetName=_target(user),
                      Comment=f"{APP_NAME}: Koofr ({user})", CredentialBlobSize=len(blob),
                      CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
                      Persist=CRED_PERSIST_LOCAL_MACHINE, UserName=user)
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "the password could not be saved in the Credential Manager")


def credential_lookup(user: str) -> str | None:
    found = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(_target(user), CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        if ctypes.get_last_error() == ERROR_NOT_FOUND:
            return None
        raise OSError(ctypes.get_last_error(), "the Credential Manager could not be read")
    try:
        size = found.contents.CredentialBlobSize
        return ctypes.string_at(found.contents.CredentialBlob, size).decode("utf-16-le")
    finally:
        advapi32.CredFree(found)


def credential_clear(user: str) -> None:
    if not advapi32.CredDeleteW(_target(user), CRED_TYPE_GENERIC, 0) and ctypes.get_last_error() != ERROR_NOT_FOUND:
        raise OSError(ctypes.get_last_error(), "the password could not be removed from the Credential Manager")


# -- start at login ----------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(command: str | None) -> None:
    """The command started at login (None: none), in the user's Run key.
    A new profile may not have the key yet (a CI runner has none): made
    when there is a command to write, nothing to remove otherwise."""
    if command:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass


def autostart_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP_NAME)[0]
    except FileNotFoundError:
        return None


# -- Koofr Desktop: is it running, or started at login? -----------------------------------

TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_names() -> set[str]:
    """The programs running now, lower case, without ".exe"."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return set()
    names = set()
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            name = entry.szExeFile.casefold()
            names.add(name[:-4] if name.endswith(".exe") else name)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"
RUN32_KEY = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"


def _values(root, path: str) -> dict[str, object]:
    found = {}
    try:
        with winreg.OpenKey(root, path) as key:
            i = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                found[name] = value
                i += 1
    except OSError:
        pass
    return found


def approved(data) -> bool:
    """A StartupApproved value: the first byte odd is an entry turned off in
    Task Manager or in Settings (3), even is on (2, 6); none is on."""
    return not (isinstance(data, bytes) and data and data[0] & 1)


def shortcut_text(path: str) -> str:
    """What a shortcut (.lnk) holds, as text to look into: its target is
    stored both as ANSI and as UTF-16 strings."""
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)
    except OSError:
        return ""
    return data.decode("latin-1") + " " + data.decode("utf-16-le", errors="replace")


def _folder_entries(folder: str, approvals: dict[str, object]) -> list[tuple[str, str]]:
    """(file name, text) of the files of a Startup folder not turned off."""
    off = {name.casefold() for name, data in approvals.items() if not approved(data)}
    found = []
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        if name.casefold() in off or name.casefold() == "desktop.ini":
            continue
        path = os.path.join(folder, name)
        found.append((name, f"{name} {shortcut_text(path)}" if name.casefold().endswith(".lnk") else name))
    return found


def startup_folder_entries(folder: str, approvals: dict[str, object]) -> list[str]:
    """The files of a Startup folder that are not turned off, each as its
    name and, for a shortcut, what it points to."""
    return [text for _name, text in _folder_entries(folder, approvals)]


@dataclass(frozen=True)
class LoginEntry:
    """Something started at login, not turned off."""
    text: str           # what it starts, to look into
    root: int           # the hive of its StartupApproved value
    approval: str       # "Run", "Run32" or "StartupFolder"
    name: str           # the name of its value: the Run value's, the file's


def login_entries() -> list[LoginEntry]:
    """What starts at login for this user and is not turned off: the Run
    keys (the user's and the machine's, 64 and 32 bit) and the Startup
    folders."""
    found = []
    for root, run, approval in ((winreg.HKEY_CURRENT_USER, RUN_KEY, "Run"),
                                (winreg.HKEY_LOCAL_MACHINE, RUN_KEY, "Run"),
                                (winreg.HKEY_LOCAL_MACHINE, RUN32_KEY, "Run32")):
        approvals = _values(root, APPROVED_KEY + "\\" + approval)
        found += [LoginEntry(f"{name} {value}", root, approval, name)
                  for name, value in _values(root, run).items() if approved(approvals.get(name))]
    for base, root in ((os.environ.get("APPDATA"), winreg.HKEY_CURRENT_USER),
                       (os.environ.get("ProgramData"), winreg.HKEY_LOCAL_MACHINE)):
        if base:
            startup = os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
            found += [LoginEntry(text, root, "StartupFolder", name)
                      for name, text in _folder_entries(startup, _values(root, APPROVED_KEY + "\\StartupFolder"))]
    return found


def login_commands() -> list[str]:
    """The entries of login_entries(), as text to look into."""
    return [entry.text for entry in login_entries()]


def turn_off(entry: LoginEntry, now: float | None = None) -> None:
    """Turned off as Task Manager does it: the entry stays where it is and
    Windows skips it; Task Manager or Settings turn it on again. One of the
    machine's needs an administrator (PermissionError)."""
    filetime = int(((time.time() if now is None else now) + 11644473600) * 10_000_000)
    with winreg.CreateKeyEx(entry.root, APPROVED_KEY + "\\" + entry.approval, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, entry.name, 0, winreg.REG_BINARY, struct.pack("<IQ", 3, filetime))
