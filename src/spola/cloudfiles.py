"""File Explorer's sync status (Windows): each synced folder a sync root of
the Cloud Files platform, as OneDrive's, so that Explorer shows its own
emblems: a green tick on what is in sync, blue arrows on what changed here
and waits for a run (Windows sets those by itself), and the folder in the
navigation pane under Spola's name and icon.

Tried before it was written (on a scratch folder): a file made a placeholder
stays an ordinary file for every program and for rclone (the platform hides
the reparse point of a full placeholder), and a sync root unregistered,
even without reverting its placeholders, leaves ordinary files behind. The
policies say so: always full, never hydrated nor populated on demand, so
Spola never answers a callback; it only connects while it runs (Explorer
shows the states of a connected provider) and marks in sync, after each
run, what the run touched.

The shell's registration is the Windows Runtime's StorageProviderSyncRoot-
Manager (IIDs and method order read from Windows' own metadata), the rest
cldapi.dll; both through ctypes. Imported only on Windows.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import winreg
from ctypes import wintypes

from . import wintoast as rt
from .wintoast import HSTRING, _call, _check, _factory, _guid, _HString, _query, _release

cld = ctypes.WinDLL("cldapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

PROVIDER = "Spola"
P = ctypes.c_void_p

IID_IStorageFolderStatics = _guid("08f327ff-85d5-48b9-aee9-28511e339f9f")
IID_IAsyncInfo = _guid("00000036-0000-0000-c000-000000000046")
IID_ISyncRootInfo = _guid("7c1305c4-99f9-41ac-8904-ab055d654926")
IID_ISyncRootManagerStatics = _guid("3e99fbbf-8fe3-4b40-abc7-f6fc3d74c98e")

# IStorageProviderSyncRootInfo
INFO_GET_ID, INFO_PUT_ID, INFO_PUT_PATH = 6, 7, 11
INFO_PUT_DISPLAY_NAME, INFO_PUT_ICON = 13, 15
INFO_PUT_HYDRATION, INFO_PUT_POPULATION, INFO_PUT_INSYNC, INFO_PUT_HARDLINK = 17, 21, 23, 25
INFO_PUT_VERSION = 29
# IStorageProviderSyncRootManagerStatics
MANAGER_REGISTER, MANAGER_UNREGISTER, MANAGER_CURRENT = 6, 7, 10
# IVectorView<T>: GetAt, get_Size
VIEW_GET_AT, VIEW_SIZE = 6, 7
HYDRATION_ALWAYS_FULL, POPULATION_ALWAYS_FULL = 3, 2
ASYNC_STARTED, ASYNC_COMPLETED = 0, 1

CF_IN_SYNC_STATE_IN_SYNC = 1
CF_CONVERT_FLAG_MARK_IN_SYNC = 0x1
ERROR_NOT_A_CLOUD_FILE = 0x80070178
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT = 3, 0x02000000, 0x00200000
INVALID_HANDLE = ctypes.c_void_p(-1).value


class CALLBACK_REGISTRATION(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_int), ("Callback", ctypes.c_void_p)]


for _name in ("CfConnectSyncRoot", "CfDisconnectSyncRoot", "CfSetInSyncState", "CfConvertToPlaceholder"):
    getattr(cld, _name).restype = ctypes.c_long
cld.CfConnectSyncRoot.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(CALLBACK_REGISTRATION), ctypes.c_void_p,
                                  wintypes.DWORD, ctypes.POINTER(ctypes.c_int64)]
cld.CfDisconnectSyncRoot.argtypes = [ctypes.c_int64]
cld.CfSetInSyncState.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p]
cld.CfConvertToPlaceholder.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, ctypes.c_void_p]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]


def user_sid() -> str:
    """The SID of the user running Spola: part of a sync root's id."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):      # QUERY
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))                         # TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(buffer).value          # TOKEN_USER: SID_AND_ATTRIBUTES, the SID first
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def root_id(sid: str, pair_id: str) -> str:
    """[provider]![user's SID]![account], as the shell wants it: the pair's
    id for the account (one sync root per synced folder)."""
    return f"{PROVIDER}!{sid}!{pair_id}"


def winrt_thread() -> None:
    """The calling thread in the Windows Runtime's multithreaded apartment
    (a worker thread: the shell's calls block)."""
    rt.combase.RoInitialize(1)


def windows_path(path: str) -> str:
    """With backslashes: MSYS2's Python, which the Windows build runs on,
    writes "/" (os.sep), and the shell's StorageFolder refuses it."""
    return os.path.normpath(path).replace("/", "\\")


def _folder(path: str) -> P:
    """StorageFolder.GetFolderFromPathAsync(path), waited for."""
    statics = _factory("Windows.Storage.StorageFolder", IID_IStorageFolderStatics)
    operation, info = P(), None
    try:
        with _HString(path) as text:
            _call(statics, 6, text, ctypes.byref(operation), argtypes=(HSTRING, ctypes.POINTER(P)))
        info = _query(operation, IID_IAsyncInfo)
        status = ctypes.c_int(ASYNC_STARTED)
        deadline = time.monotonic() + 30
        while status.value == ASYNC_STARTED and time.monotonic() < deadline:
            _call(info, 7, ctypes.byref(status), argtypes=(ctypes.POINTER(ctypes.c_int),))
            if status.value == ASYNC_STARTED:
                time.sleep(0.02)
        if status.value != ASYNC_COMPLETED:
            raise rt.WinRTError(f"the folder could not be opened ({status.value}): {path}")
        folder = P()
        _call(operation, 8, ctypes.byref(folder), argtypes=(ctypes.POINTER(P),))      # GetResults
        return folder
    finally:
        _release(info)
        _release(operation)
        _release(statics)


def _manager() -> P:
    return _factory("Windows.Storage.Provider.StorageProviderSyncRootManager", IID_ISyncRootManagerStatics)


def register(identity: str, path: str, display_name: str, icon: str, version: str) -> None:
    """The folder, a sync root of Spola's in the shell (raises WinRTError:
    not NTFS, inside another provider's root, ...)."""
    inspectable, info, folder, manager = P(), None, None, None
    try:
        with _HString("Windows.Storage.Provider.StorageProviderSyncRootInfo") as text:
            _check(rt.combase.RoActivateInstance(text, ctypes.byref(inspectable)), "StorageProviderSyncRootInfo")
        info = _query(inspectable, IID_ISyncRootInfo)

        def put_text(slot: int, value: str) -> None:
            with _HString(value) as text:
                _call(info, slot, text, argtypes=(HSTRING,))

        def put_int(slot: int, value: int) -> None:
            _call(info, slot, value, argtypes=(ctypes.c_int,))

        put_text(INFO_PUT_ID, identity)
        folder = _folder(windows_path(path))
        _call(info, INFO_PUT_PATH, folder, argtypes=(P,))
        put_text(INFO_PUT_DISPLAY_NAME, display_name)
        # never empty: the shell's Register crashes the process on an empty icon
        put_text(INFO_PUT_ICON, icon or f"{windows_path(sys.executable)},0")
        put_int(INFO_PUT_HYDRATION, HYDRATION_ALWAYS_FULL)
        put_int(INFO_PUT_POPULATION, POPULATION_ALWAYS_FULL)
        put_int(INFO_PUT_INSYNC, 0)
        put_int(INFO_PUT_HARDLINK, 0)
        put_text(INFO_PUT_VERSION, version)
        manager = _manager()
        _call(manager, MANAGER_REGISTER, info, argtypes=(P,))
    finally:
        for obj in (manager, folder, info, inspectable):
            _release(obj)


NAMESPACE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Desktop\NameSpace"
SYNC_ROOT_MANAGER = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\SyncRootManager"


def unregister(identity: str) -> None:
    """The root out of the shell. A root whose folder is gone the shell no
    longer finds (ERROR_NOT_FOUND) and leaves in the navigation pane: its
    keys are then taken out here, the user's and (when allowed) the
    machine's."""
    manager = _manager()
    try:
        with _HString(identity) as text:
            _call(manager, MANAGER_UNREGISTER, text, argtypes=(HSTRING,))
    except rt.WinRTError:
        forget_keys(identity)
    finally:
        _release(manager)


def _delete_tree(root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path) as key:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
        for child in children:
            _delete_tree(root, f"{path}\\{child}")
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def left_behind(prefix: str) -> set[str]:
    """Ids starting with prefix the shell still keeps keys of, also those it
    no longer lists (their folder gone): the machine's root keys and the
    navigation pane's entries."""
    found: set[str] = set()
    for root, path, by_name in ((winreg.HKEY_LOCAL_MACHINE, SYNC_ROOT_MANAGER, True),
                                (winreg.HKEY_CURRENT_USER, NAMESPACE, False)):
        try:
            with winreg.OpenKey(root, path) as key:
                i = 0
                while True:
                    try:
                        child = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    if by_name:
                        name = child
                    else:
                        try:
                            with winreg.OpenKey(key, child) as sub:
                                name = winreg.QueryValueEx(sub, "")[0]
                        except OSError:
                            continue
                    if isinstance(name, str) and name.startswith(prefix):
                        found.add(name)
        except OSError:
            continue
    return found


def forget_keys(identity: str) -> None:
    """What the shell keeps of a sync root: the navigation pane's entry
    (a namespace CLSID named after the root's id) and the root's own key."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NAMESPACE) as key:
            clsids = []
            i = 0
            while True:
                try:
                    clsids.append(winreg.EnumKey(key, i))
                except OSError:
                    break
                i += 1
    except OSError:
        clsids = []
    for clsid in clsids:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{NAMESPACE}\\{clsid}") as key:
                name = winreg.QueryValueEx(key, "")[0]
        except OSError:
            continue
        if name == identity:
            _delete_tree(winreg.HKEY_CURRENT_USER, f"{NAMESPACE}\\{clsid}")
            _delete_tree(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\CLSID\\{clsid}")
    _delete_tree(winreg.HKEY_LOCAL_MACHINE, f"{SYNC_ROOT_MANAGER}\\{identity}")


def registered() -> list[str]:
    """The ids of the sync roots registered for this user (any provider)."""
    manager, view, ids = _manager(), P(), []
    try:
        _call(manager, MANAGER_CURRENT, ctypes.byref(view), argtypes=(ctypes.POINTER(P),))
        size = ctypes.c_uint32()
        _call(view, VIEW_SIZE, ctypes.byref(size), argtypes=(ctypes.POINTER(ctypes.c_uint32),))
        for i in range(size.value):
            item = P()
            _call(view, VIEW_GET_AT, i, ctypes.byref(item), argtypes=(ctypes.c_uint32, ctypes.POINTER(P)))
            try:
                info = _query(item, IID_ISyncRootInfo)
                text = HSTRING()
                _call(info, INFO_GET_ID, ctypes.byref(text), argtypes=(ctypes.POINTER(HSTRING),))
                ids.append(_hstring_text(text))
                rt.combase.WindowsDeleteString(text)
                _release(info)
            finally:
                _release(item)
    finally:
        _release(view)
        _release(manager)
    return ids


rt.combase.WindowsGetStringRawBuffer.argtypes = [HSTRING, ctypes.POINTER(ctypes.c_uint32)]
rt.combase.WindowsGetStringRawBuffer.restype = ctypes.c_void_p


def _hstring_text(handle) -> str:
    length = ctypes.c_uint32()
    buffer = rt.combase.WindowsGetStringRawBuffer(handle, ctypes.byref(length))
    return ctypes.wstring_at(buffer, length.value) if buffer else ""


def connect(path: str) -> int:
    """Spola connected as the root's provider while it runs (no callbacks:
    always full, nothing is ever fetched): the key for disconnect()."""
    table = (CALLBACK_REGISTRATION * 1)(CALLBACK_REGISTRATION(-1, None))      # CF_CALLBACK_REGISTRATION_END
    key = ctypes.c_int64()
    _check(cld.CfConnectSyncRoot(windows_path(path), table, None, 0, ctypes.byref(key)), "CfConnectSyncRoot")
    return key.value


def disconnect(key: int) -> None:
    cld.CfDisconnectSyncRoot(key)


def mark_in_sync(path: str) -> bool:
    """A file or folder, in sync: made a placeholder (always full) the
    first time. False when it cannot be opened (gone, in use)."""
    handle = kernel32.CreateFileW(windows_path(path), GENERIC_READ | GENERIC_WRITE, 7, None, OPEN_EXISTING,
                                  FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle in (None, INVALID_HANDLE):
        return False
    try:
        hr = cld.CfSetInSyncState(handle, CF_IN_SYNC_STATE_IN_SYNC, 0, None)
        if hr & 0xFFFFFFFF == ERROR_NOT_A_CLOUD_FILE:
            hr = cld.CfConvertToPlaceholder(handle, None, 0, CF_CONVERT_FLAG_MARK_IN_SYNC, None, None)
        return hr >= 0
    finally:
        kernel32.CloseHandle(handle)
