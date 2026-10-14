"""A metered connection and power saving, as Windows tells them.

GLib on Windows knows whether there is a network, not what it costs, and
has no power profile: Spola's "pause on metered connections" and "pause in
power saver mode" read Windows itself. The cost of the connection comes
from the Network List Manager (INetworkCostManager::GetCost, the same that
Settings shows as "metered connection"); energy saver, battery saver before
Windows 11 24H2, from GetSystemPowerStatus. Both are read every few
seconds, and a change calls back. Imported only on Windows.
"""

from __future__ import annotations

import ctypes
import uuid
from collections.abc import Callable
from ctypes import wintypes

from gi.repository import GLib

ole32 = ctypes.WinDLL("ole32")
kernel32 = ctypes.WinDLL("kernel32")

POLL_S = 15

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_ALL = 0x17

# NLM_CONNECTION_COST: anything but "unrestricted" costs (netlistmgr.h)
NLM_CONNECTION_COST_UNRESTRICTED = 0x1
NLM_CONNECTION_COST_FIXED = 0x2
NLM_CONNECTION_COST_VARIABLE = 0x4
NLM_CONNECTION_COST_OVERDATALIMIT = 0x10000
NLM_CONNECTION_COST_ROAMING = 0x40000
METERED = (NLM_CONNECTION_COST_FIXED | NLM_CONNECTION_COST_VARIABLE | NLM_CONNECTION_COST_OVERDATALIMIT
           | NLM_CONNECTION_COST_ROAMING)
GET_COST = 3            # INetworkCostManager: IUnknown 0-2, then GetCost


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_NetworkListManager = _guid("dcb00c01-570f-4a9b-8d69-199fdba5723b")
IID_INetworkCostManager = _guid("dcb00008-570f-4a9b-8d69-199fdba5723b")


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long
kernel32.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
kernel32.GetSystemPowerStatus.restype = wintypes.BOOL


def is_metered(cost: int) -> bool:
    return bool(cost & METERED)


class CostManager:
    """The Network List Manager's cost of the connection; None when it
    cannot be asked (taken as not metered)."""

    def __init__(self):
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)       # GTK may have done it already
        self._manager = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(CLSID_NetworkListManager), None, CLSCTX_ALL,
                                    ctypes.byref(IID_INetworkCostManager), ctypes.byref(self._manager))
        if hr < 0:
            self._manager = None

    def cost(self) -> int | None:
        if not self._manager:
            return None
        vtable = ctypes.cast(self._manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get_cost = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.c_void_p)(vtable[GET_COST])
        cost = wintypes.DWORD()
        if get_cost(self._manager, ctypes.byref(cost), None) < 0:
            return None
        return cost.value


def power_saver_on() -> bool:
    """Energy saver (battery saver before Windows 11 24H2) is on."""
    status = SYSTEM_POWER_STATUS()
    return bool(kernel32.GetSystemPowerStatus(ctypes.byref(status))) and status.SystemStatusFlag == 1


class Conditions:
    """What the engine asks Gio's monitors for on Linux, read from Windows."""

    def __init__(self, on_change: Callable[[], None], poll_s: int = POLL_S):
        self.on_change = on_change
        self._costs = CostManager()
        self.metered, self.power_saver = self._read()
        self._source = GLib.timeout_add_seconds(poll_s, self._poll)

    def _read(self) -> tuple[bool, bool]:
        cost = self._costs.cost()
        return (cost is not None and is_metered(cost)), power_saver_on()

    def _poll(self) -> bool:
        now = self._read()
        if now != (self.metered, self.power_saver):
            self.metered, self.power_saver = now
            self.on_change()
        return True

    def stop(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
