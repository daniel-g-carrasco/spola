# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows only: the toasts of the Windows Runtime (app/wintoast.py): a
string reaches Windows whole, their text, the COM object that hears a
click, the notifier. No toast is shown and nothing is written in the
registry (register_app is left to the app)."""

import ctypes
import unittest
from unittest import mock

import _setup

if not _setup.WINDOWS:
    raise unittest.SkipTest("Windows only")

wintoast = _setup.shared("app.wintoast")

# a handle of our own: argtypes set on wintoast's would reach the module
_combase = ctypes.WinDLL("combase")
_combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_combase.WindowsGetStringRawBuffer.restype = ctypes.c_void_p


def hstring_text(handle) -> str:
    length = ctypes.c_uint32()
    buffer = _combase.WindowsGetStringRawBuffer(handle, ctypes.byref(length))
    return ctypes.wstring_at(buffer, length.value) if buffer else ""


def vtable_function(obj, slot, prototype):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return prototype(vtable[slot])


class ToastTest(unittest.TestCase):
    def test_a_string_with_emoji_reaches_windows_whole(self):
        for text in ("C:\\Users\\someone\\Foto \U0001f600 mare", "\u00e0\u00e8\u00ec \U0001f1ee\U0001f1f9 ok", ""):
            with wintoast._HString(text) as handle:
                self.assertEqual(hstring_text(handle), text)

    def test_text_is_escaped(self):
        xml = wintoast.toast_xml("\u201cA & B\u201d <ok>", "x > y")
        self.assertIn("<text>\u201cA &amp; B\u201d &lt;ok&gt;</text>", xml)
        self.assertIn("<text>x &gt; y</text>", xml)
        self.assertTrue(xml.startswith("<toast>") and xml.endswith("</toast>"))

    def test_an_emoji_counts_two_utf16_units(self):
        self.assertEqual(wintoast.utf16_length("ab"), 2)
        self.assertEqual(wintoast.utf16_length("a\U0001f600"), 3)

    def test_the_click_handler_is_a_com_object(self):
        clicked = []
        handler = wintoast._ActivatedHandler(lambda: clicked.append(1))
        query = vtable_function(handler.pointer, 0, wintoast._QI)
        out = ctypes.c_void_p()
        for iid in (wintoast.IID_ActivatedHandler, wintoast.IID_IUnknown, wintoast.IID_IAgileObject):
            self.assertEqual(query(handler.pointer, ctypes.byref(iid), ctypes.byref(out)), wintoast.S_OK)
            self.assertEqual(out.value, handler.pointer.value)
        other = wintoast._guid("11111111-2222-3333-4444-555555555555")
        self.assertEqual(query(handler.pointer, ctypes.byref(other), ctypes.byref(out)), wintoast.E_NOINTERFACE)
        invoke = vtable_function(handler.pointer, 3, wintoast._INVOKE)
        self.assertEqual(invoke(handler.pointer, None, None), wintoast.S_OK)
        self.assertEqual(clicked, [1])

    def test_a_failing_callback_stays_on_this_side(self):
        handler = wintoast._ActivatedHandler(lambda: 1 / 0)
        invoke = vtable_function(handler.pointer, 3, wintoast._INVOKE)
        self.assertEqual(invoke(handler.pointer, None, None), wintoast.S_OK)

    def test_the_notifier_of_the_windows_runtime(self):
        toasts = wintoast.Toasts("io.example.my-app-test", "my-app-test")
        self.assertTrue(toasts._notifier)
        self.assertTrue(toasts._toast_factory)

    def test_only_the_latest_toasts_are_kept(self):
        # real toasts and click handlers, never shown: the notifier's Show
        # alone is left out
        toasts = wintoast.Toasts("io.example.my-app-test", "my-app-test")
        real = wintoast._call

        def call(obj, slot, *args, **kwargs):
            if obj is toasts._notifier and slot == wintoast.NOTIFIER_SHOW:
                return None
            return real(obj, slot, *args, **kwargs)

        with mock.patch.object(wintoast, "_call", call):
            for i in range(wintoast.KEPT_TOASTS + 5):
                toasts.show(f"Toast {i}", "body", tag=f"t{i}", on_click=lambda: None)
        self.assertEqual(len(toasts._kept), wintoast.KEPT_TOASTS)
        self.assertTrue(all(handler is not None for _toast, handler, _token in toasts._kept))


if __name__ == "__main__":
    unittest.main()
