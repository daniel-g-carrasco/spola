"""The network, a metered connection and power saving, as Windows tells them.

GLib on Windows knows whether there is a network, not what it costs, and
has no power profile: Spola's "pause on metered connections" and "pause in
power saver mode" read Windows itself. The cost of the connection comes
from the Network List Manager (INetworkCostManager::GetCost, the same that
Settings shows as "metered connection"); energy saver, battery saver before
Windows 11 24H2, from GetSystemPowerStatus. Whether there is a network at
all comes from the Network List Manager too (INetworkListManager::
GetConnectivity, what the taskbar's network icon shows): GLib's monitor
stayed at "no network" after a change of network (19/10/2026, Ethernet and
Wi-Fi on the same router), and Spola waited "offline" with Koofr reachable.
All are read every few seconds, and a change calls back. Imported only on
Windows.
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
# NLM_CONNECTIVITY: a subnet, a local network or the internet, IPv4 or IPv6;
# "no traffic" alone is no network
NLM_CONNECTIVITY_DISCONNECTED = 0x0
NLM_CONNECTIVITY_IPV4_NOTRAFFIC = 0x1
NLM_CONNECTIVITY_IPV6_NOTRAFFIC = 0x2
NLM_CONNECTIVITY_IPV4_INTERNET = 0x40
NLM_CONNECTIVITY_IPV6_INTERNET = 0x400
CONNECTED = 0x10 | 0x20 | 0x40 | 0x100 | 0x200 | 0x400
GET_CONNECTIVITY = 13   # INetworkListManager: IUnknown 0-2, IDispatch 3-6, GetNetworks 7 ... GetConnectivity 13


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_NetworkListManager = _guid("dcb00c01-570f-4a9b-8d69-199fdba5723b")
IID_INetworkCostManager = _guid("dcb00008-570f-4a9b-8d69-199fdba5723b")
IID_INetworkListManager = _guid("dcb00000-570f-4a9b-8d69-199fdba5723b")


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


def is_connected(connectivity: int) -> bool:
    return bool(connectivity & CONNECTED)


def _network_list_manager(iid: GUID):
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)           # GTK may have done it already
    manager = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_NetworkListManager), None, CLSCTX_ALL,
                                ctypes.byref(iid), ctypes.byref(manager))
    return manager if hr >= 0 and manager.value else None


class NetworkList:
    """The Network List Manager's connectivity (NLM_CONNECTIVITY flags);
    None when it cannot be asked (then GLib's monitor is asked instead)."""

    def __init__(self):
        self._manager = _network_list_manager(IID_INetworkListManager)

    def connectivity(self) -> int | None:
        if not self._manager:
            return None
        vtable = ctypes.cast(self._manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        get = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))(
            vtable[GET_CONNECTIVITY])
        value = ctypes.c_int()
        if get(self._manager, ctypes.byref(value)) < 0:
            return None
        return value.value


class CostManager:
    """The Network List Manager's cost of the connection; None when it
    cannot be asked (taken as not metered)."""

    def __init__(self):
        self._manager = _network_list_manager(IID_INetworkCostManager)

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
        self._networks = NetworkList()
        # connected: None when Windows cannot be asked
        self.metered, self.power_saver, self.connected = self._read()
        self._source = GLib.timeout_add_seconds(poll_s, self._poll)

    def _read(self) -> tuple[bool, bool, bool | None]:
        cost, connectivity = self._costs.cost(), self._networks.connectivity()
        return ((cost is not None and is_metered(cost)), power_saver_on(),
                None if connectivity is None else is_connected(connectivity))

    def _poll(self) -> bool:
        now = self._read()
        if now != (self.metered, self.power_saver, self.connected):
            self.metered, self.power_saver, self.connected = now
            self.on_change()
        return True

    def stop(self) -> None:
        if self._source:
            GLib.source_remove(self._source)
            self._source = 0
