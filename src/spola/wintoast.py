"""Notifications on Windows 10 and 11: the toasts of the Windows Runtime
(Windows.UI.Notifications), through ctypes, no extra module.

A balloon of the notification area icon reaches the notification centre
only now and then, and without "Do not disturb" it is lost: a toast is
Spola's own, with its name and icon, and stays in the centre. Windows shows
the toasts of an app that has an AppUserModelID: Setup gives the Start menu
shortcut Spola's (the documented way), and Spola writes the same identity
under HKCU\\Software\\Classes\\AppUserModelId, for the portable build and a
run from the sources. A click on a toast comes back through its Activated
event, while Spola runs.

Every interface is called through its vtable: the IIDs and the order of the
methods are those of windows.ui.notifications.h and windows.data.xml.dom.h
(mingw-w64).
"""

from __future__ import annotations

import ctypes
import uuid
import winreg
from collections.abc import Callable
from ctypes import wintypes
from xml.sax.saxutils import escape

combase = ctypes.WinDLL("combase")
shell32 = ctypes.WinDLL("shell32")

HSTRING = ctypes.c_void_p
S_OK, S_FALSE = 0, 1
E_NOINTERFACE = -2147467262          # 0x80004002
RPC_E_CHANGED_MODE = -2147417850     # 0x80010106: COM already set up otherwise on this thread
RO_INIT_SINGLETHREADED = 0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


def _guid(text: str) -> GUID:
    return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


IID_IUnknown = _guid("00000000-0000-0000-c000-000000000046")
IID_IAgileObject = _guid("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IToastNotificationManagerStatics = _guid("50ac103f-d235-4598-bbef-98fe4d1a3ad4")
IID_IToastNotificationFactory = _guid("04124b20-82c6-4229-b109-fd9ed4662b53")
IID_IToastNotification2 = _guid("9dfb9fd1-143a-490e-90bf-b9fba7132de7")
IID_IXmlDocument = _guid("f7f3a506-1e87-42d6-bcfb-b8c809fa5494")
IID_IXmlDocumentIO = _guid("6cd0e74e-ee65-4489-9ebf-ca43e87ba637")
# TypedEventHandler<ToastNotification, IInspectable>: the Activated event
IID_ActivatedHandler = _guid("ab54de2d-97d9-5528-b6ad-105afe156530")

# vtable slots: IUnknown 0-2, IInspectable 3-5, then each interface's own
QUERY_INTERFACE, RELEASE = 0, 2
STATICS_CREATE_NOTIFIER_WITH_ID = 7
NOTIFIER_SHOW = 6
FACTORY_CREATE = 6
TOAST_ADD_ACTIVATED = 11
TOAST2_PUT_TAG, TOAST2_PUT_GROUP = 6, 8
XMLIO_LOAD_XML = 6

combase.RoInitialize.argtypes = [ctypes.c_int]
combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, wintypes.UINT, ctypes.POINTER(HSTRING)]
combase.WindowsDeleteString.argtypes = [HSTRING]
combase.RoGetActivationFactory.argtypes = [HSTRING, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
combase.RoActivateInstance.argtypes = [HSTRING, ctypes.POINTER(ctypes.c_void_p)]
for _f in (combase.RoInitialize, combase.WindowsCreateString, combase.WindowsDeleteString,
           combase.RoGetActivationFactory, combase.RoActivateInstance):
    _f.restype = ctypes.c_long
shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [wintypes.LPCWSTR]
shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long


class WinRTError(OSError):
    def __init__(self, message: str, hresult: int = 0):
        super().__init__(message)
        self.hresult = hresult & 0xFFFFFFFF


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise WinRTError(f"{what}: HRESULT 0x{hr & 0xFFFFFFFF:08X}", hr)


def utf16_length(text: str) -> int:
    """What the Windows API counts: UTF-16 units, two for an emoji."""
    return len(text.encode("utf-16-le")) // 2


class _HString:
    def __init__(self, text: str):
        self.handle = HSTRING()
        # never len(text): an emoji in a folder name would cut its path short
        # and name another folder
        _check(combase.WindowsCreateString(text, utf16_length(text), ctypes.byref(self.handle)),
               "WindowsCreateString")

    def __enter__(self):
        return self.handle

    def __exit__(self, *_exc):
        combase.WindowsDeleteString(self.handle)


def _call(obj, slot: int, *args, argtypes=()) -> None:
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[slot])
    _check(method(obj, *args), f"slot {slot}")


def _release(obj) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[RELEASE])(obj)


def _query(obj, iid: GUID) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    _call(obj, QUERY_INTERFACE, ctypes.byref(iid), ctypes.byref(out),
          argtypes=(ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)))
    return out


def _factory(class_name: str, iid: GUID) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    with _HString(class_name) as name:
        _check(combase.RoGetActivationFactory(name, ctypes.byref(iid), ctypes.byref(out)), class_name)
    return out


_QI = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_REF = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_INVOKE = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class _ActivatedHandler:
    """A COM object of our own for the Activated event. Kept for the whole
    session (Windows may call it, or release it, late), so its count of
    references is not kept. Called on a thread of the Windows Runtime's."""

    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self._functions = (_QI(self._query_interface), _REF(lambda _this: 1), _REF(lambda _this: 1),
                           _INVOKE(self._invoke))
        self._vtable = (ctypes.c_void_p * 4)(*(ctypes.cast(f, ctypes.c_void_p) for f in self._functions))
        self._object = ctypes.c_void_p(ctypes.addressof(self._vtable))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _query_interface(self, this, riid, out):
        if bytes(riid.contents) in (bytes(IID_IUnknown), bytes(IID_IAgileObject), bytes(IID_ActivatedHandler)):
            out[0] = this
            return S_OK
        out[0] = None
        return E_NOINTERFACE

    def _invoke(self, _this, _sender, _args):
        try:
            self.callback()
        except Exception:  # an exception must not cross into Windows
            pass
        return S_OK


def toast_xml(title: str, body: str) -> str:
    return ('<toast><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(body)}</text>"
            "</binding></visual></toast>")


def register_app(app_id: str, display_name: str, icon: str = "") -> None:
    """The identity the toasts show: the process's AppUserModelID (also
    the taskbar's) and its name and icon in the registry."""
    shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{app_id}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        if icon:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon)


class Toasts:
    """Raises WinRTError when the Windows Runtime cannot be reached (an old
    Windows): the caller falls back to the icon's balloons."""

    def __init__(self, app_id: str, group: str = "spola"):
        self.app_id, self.group = app_id, group
        hr = combase.RoInitialize(RO_INIT_SINGLETHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            _check(hr, "RoInitialize")
        statics = _factory("Windows.UI.Notifications.ToastNotificationManager", IID_IToastNotificationManagerStatics)
        try:
            self._notifier = ctypes.c_void_p()
            with _HString(app_id) as name:
                _call(statics, STATICS_CREATE_NOTIFIER_WITH_ID, name, ctypes.byref(self._notifier),
                      argtypes=(HSTRING, ctypes.POINTER(ctypes.c_void_p)))
        finally:
            _release(statics)
        self._toast_factory = _factory("Windows.UI.Notifications.ToastNotification", IID_IToastNotificationFactory)
        self._kept: list = []       # the toasts and their handlers, for the session

    def show(self, title: str, body: str, tag: str = "", on_click: Callable[[], None] | None = None) -> None:
        """A toast; one with the same tag replaces the one before."""
        document = ctypes.c_void_p()
        with _HString("Windows.Data.Xml.Dom.XmlDocument") as name:
            _check(combase.RoActivateInstance(name, ctypes.byref(document)), "XmlDocument")
        io = xml = toast = toast2 = None
        try:
            io = _query(document, IID_IXmlDocumentIO)
            with _HString(toast_xml(title, body)) as text:
                _call(io, XMLIO_LOAD_XML, text, argtypes=(HSTRING,))
            xml = _query(document, IID_IXmlDocument)
            toast = ctypes.c_void_p()
            _call(self._toast_factory, FACTORY_CREATE, xml, ctypes.byref(toast),
                  argtypes=(ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)))
            if tag:
                toast2 = _query(toast, IID_IToastNotification2)
                with _HString(tag[:64]) as text:
                    _call(toast2, TOAST2_PUT_TAG, text, argtypes=(HSTRING,))
                with _HString(self.group) as text:
                    _call(toast2, TOAST2_PUT_GROUP, text, argtypes=(HSTRING,))
            handler = None
            if on_click is not None:
                handler = _ActivatedHandler(on_click)
                token = ctypes.c_int64()
                _call(toast, TOAST_ADD_ACTIVATED, handler.pointer, ctypes.byref(token),
                      argtypes=(ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)))
            _call(self._notifier, NOTIFIER_SHOW, toast, argtypes=(ctypes.c_void_p,))
            self._kept.append((toast, handler))
            toast = None
        finally:
            for obj in (toast2, toast, xml, io, document):
                _release(obj)
