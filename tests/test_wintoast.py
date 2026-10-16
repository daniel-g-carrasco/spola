#!/usr/bin/env python3
"""The toasts of Windows: their text, the COM object that hears a click,
the notifier of the Windows Runtime."""

import ctypes
import sys
import unittest
from unittest import mock

import _setup  # noqa: F401

if sys.platform != "win32":
    raise unittest.SkipTest("the toasts of Windows")

from spola import wintoast  # noqa: E402


def vtable_function(obj, slot, prototype):
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return prototype(vtable[slot])


class ToastTest(unittest.TestCase):
    def test_a_string_with_emoji_reaches_windows_whole(self):
        from spola.cloudfiles import _hstring_text
        for text in ("C:\\Users\\Grasso\\Foto 😀 mare", "àèì 🇮🇹 ok", ""):
            with wintoast._HString(text) as handle:
                self.assertEqual(_hstring_text(handle), text)

    def test_text_is_escaped(self):
        xml = wintoast.toast_xml("“A & B” <ok>", "x > y")
        self.assertIn("<text>“A &amp; B” &lt;ok&gt;</text>", xml)
        self.assertIn("<text>x &gt; y</text>", xml)
        self.assertTrue(xml.startswith("<toast>") and xml.endswith("</toast>"))

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
        toasts = wintoast.Toasts("io.example.spola-test")
        self.assertTrue(toasts._notifier)
        self.assertTrue(toasts._toast_factory)

    def test_only_the_latest_toasts_are_kept(self):
        # real toasts and click handlers, never shown: the notifier's Show
        # alone is left out
        toasts = wintoast.Toasts("io.example.spola-test")
        real = wintoast._call

        def call(obj, slot, *args, **kwargs):
            if obj is toasts._notifier and slot == wintoast.NOTIFIER_SHOW:
                return None
            return real(obj, slot, *args, **kwargs)

        with mock.patch.object(wintoast, "_call", call):
            for i in range(wintoast.KEPT_TOASTS + 5):
                toasts.show(f"Spola {i}", "body", tag=f"t{i}", on_click=lambda: None)
        self.assertEqual(len(toasts._kept), wintoast.KEPT_TOASTS)
        self.assertTrue(all(handler is not None for _toast, handler, _token in toasts._kept))


if __name__ == "__main__":
    unittest.main()
