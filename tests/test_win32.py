# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows only (app/win32.py): the files that run when opened, the title
bars' colours, the screen's dpi, a file on another computer, a shortcut
made by the shell, and the folder chooser of the Windows shell (its COM
calls replaced, no dialog is opened). Nothing changes the process's dpi
awareness, nothing is written in the registry."""

import ctypes
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import _setup

if not _setup.WINDOWS:
    raise unittest.SkipTest("Windows only")

win32 = _setup.shared("app.win32")


class RunsWhenOpenedTest(unittest.TestCase):
    def test_programs_scripts_and_shortcuts_are_told_apart(self):
        for name in ("setup.exe", "Fattura.pdf.exe", "avvio.LNK", "x.bat", "x.cmd", "x.ps1", "x.vbs",
                     "x.js", "x.hta", "x.scr", "x.msi", "x.reg", "x.url"):
            self.assertTrue(win32.runs_when_opened(rf"C:\tmp\{name}"), name)
        for name in ("relazione.pdf", "tavola.dwg", "foglio.xlsx", "note.txt", "foto.jpg", "senza-estensione"):
            self.assertFalse(win32.runs_when_opened(rf"C:\tmp\{name}"), name)


class TitleBarTest(unittest.TestCase):
    def test_a_colour_as_windows_writes_it(self):
        self.assertEqual(win32.colorref((250, 251, 252)), 0x00FCFBFA)       # 0x00BBGGRR
        self.assertEqual(win32.colorref((34, 34, 38)), 0x00262222)
        self.assertEqual(win32.colorref((300, -5, 12.6)), 0x000C00FF)       # held within a byte

    def test_the_bars_of_a_process_without_windows(self):
        win32.title_bars(True, (34, 34, 38), (255, 255, 255))               # nothing to paint, nothing raised
        win32.title_bars(False)

    def test_the_icons_of_a_process_without_windows(self):
        win32.window_icons("")                                              # no exe, no window: nothing raised
        win32.window_icons("", ico=str(Path(tempfile.gettempdir()) / "no-such-icon.ico"))


class ScreenDpiTest(unittest.TestCase):
    def test_no_window_has_no_dpi(self):
        self.assertEqual(win32.window_dpi(0), 0)

    def test_the_desktops_dpi_is_a_screens(self):
        user32 = ctypes.WinDLL("user32")
        user32.GetDesktopWindow.restype = ctypes.c_void_p
        self.assertGreaterEqual(win32.window_dpi(user32.GetDesktopWindow()), 96)

    def test_the_awareness_is_read_without_being_changed(self):
        self.assertIsInstance(win32.per_monitor_aware(), bool)


class NetworkTest(unittest.TestCase):
    def test_a_unc_path_is_on_another_computer(self):
        self.assertTrue(win32.on_network(r"\\server\share\folder\file.wav"))

    def test_a_local_folder_is_not(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(win32.on_network(os.path.join(folder, "file.wav")))


class ShortcutTest(unittest.TestCase):
    def test_the_shell_makes_a_shortcut_with_a_name_in_any_language(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "caff\u00e8 \U0001f600.txt"
            target.write_text("x", encoding="utf-8")
            link = Path(folder) / "Collegamento a caff\u00e8.lnk"
            win32.shortcut(str(target), str(link))
            self.assertTrue(link.is_file())
            self.assertEqual(link.read_bytes()[:4], b"L\x00\x00\x00")         # a shell link's header

    def test_a_shortcut_that_cannot_be_saved_is_an_os_error(self):
        with tempfile.TemporaryDirectory() as folder:
            link = Path(folder) / "missing folder" / "x.lnk"
            with self.assertRaises(OSError):
                win32.shortcut(str(Path(folder)), str(link))


class NativeFolderTest(unittest.TestCase):
    """choose_folder_native with the COM calls answered here."""

    def setUp(self):
        self.calls = []
        self.cancelled = threading.Event()
        self.answer = 0
        self.name = ctypes.create_unicode_buffer("C:\\Call con spazi e caff\u00e8")
        self.ole = mock.Mock()
        self.shell = mock.Mock()
        self.user = mock.Mock()
        self.ole.CoInitializeEx.return_value = 0
        self.ole.CoCreateInstance.side_effect = lambda *args: self.pointer(args[-1], 100)
        self.shell.SHCreateItemFromParsingName.side_effect = lambda *args: self.pointer(args[-1], 200)
        self.user.SetTimer.side_effect = self.timer
        self.user.PostMessageW.return_value = True
        self.timer_callback = None
        self.cancel_during_show = False
        self.bad_result = False
        self.fail_slot = None
        self.patch_dll = mock.patch.object(ctypes, "WinDLL", side_effect=lambda name, **_kw: {
            "ole32": self.ole, "shell32": self.shell, "user32": self.user}[name])
        self.patch_method = mock.patch.object(win32, "_com_method", side_effect=self.method)
        self.patch_dll.start()
        self.patch_method.start()
        self.addCleanup(self.patch_dll.stop)
        self.addCleanup(self.patch_method.stop)

    @staticmethod
    def pointer(output, value):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = value
        return 0

    def timer(self, _owner, _id, _interval, callback):
        self.timer_callback = callback
        return 77

    def method(self, pointer, slot, *_types, **_kwargs):
        pointer = pointer.value

        def invoke(_self, *args):
            self.calls.append((pointer, slot, args))
            if self.fail_slot == slot:
                return -2147024891  # E_ACCESSDENIED
            if slot == 0:
                return self.pointer(args[-1], 400)  # IOleWindow of this dialog
            if slot == 10:
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_uint32))[0] = 0
            if slot == 3:
                if pointer == 400:
                    return self.pointer(args[0], 987654)
                if self.cancel_during_show:
                    self.cancelled.set()
                    self.timer_callback(None, 0, 77, 0)
                    self.timer_callback(None, 0, 77, 0)
                    return -2147023673  # HRESULT_FROM_WIN32(ERROR_CANCELLED)
                return self.answer
            if slot == 20:
                return self.pointer(args[0], 0 if self.bad_result else 300)
            if slot == 5:
                return self.pointer(args[-1], ctypes.addressof(self.name))
            return 0
        return invoke

    def pick(self, initial=None):
        return win32.choose_folder_native("Choose", initial, 123456, self.cancelled)

    def test_unicode_native_initial_path_and_selected_path_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "spazi e caff\u00e8"
            folder.mkdir()
            answer = self.pick(str(folder))
            self.assertEqual(self.shell.SHCreateItemFromParsingName.call_args.args[0], str(folder))
        self.assertEqual(answer, self.name.value)
        shown = [args for pointer, slot, args in self.calls if pointer == 100 and slot == 3]
        self.assertEqual(shown, [(123456,)])
        released = [pointer for pointer, slot, _args in self.calls if slot == 2]
        self.assertCountEqual(released, [100, 200, 300, 400])
        self.ole.CoTaskMemFree.assert_called_once()
        self.ole.CoUninitialize.assert_called_once()
        self.user.KillTimer.assert_called_once_with(None, 77)

    def test_cancelled_show_returns_none_without_reading_a_result(self):
        self.answer = -2147023673
        self.assertIsNone(self.pick())
        self.assertNotIn(20, [slot for _pointer, slot, _args in self.calls])
        self.ole.CoUninitialize.assert_called_once()

    def test_timer_closes_only_the_dialogs_own_ole_window_on_its_sta(self):
        self.cancel_during_show = True
        self.assertIsNone(self.pick())
        self.user.PostMessageW.assert_called_once()
        handle, message, wparam, lparam = self.user.PostMessageW.call_args.args
        self.assertEqual((handle.value, message, wparam, lparam), (987654, 0x10, 0, 0))
        self.assertNotIn(23, [slot for _pointer, slot, _args in self.calls])

    def test_cancellation_before_show_never_opens_a_dialog(self):
        self.cancelled.set()
        self.assertIsNone(self.pick())
        self.assertNotIn(3, [slot for pointer, slot, _args in self.calls if pointer == 100])
        self.user.SetTimer.assert_not_called()

    def test_shell_failure_and_null_result_are_errors_with_resources_released(self):
        self.fail_slot = 12
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(OSError):
                self.pick(temp)
        self.ole.CoUninitialize.assert_called_once()
        self.fail_slot = None
        self.bad_result = True
        with self.assertRaisesRegex(OSError, "folder"):
            self.pick()

    def test_a_missing_initial_folder_uses_the_shells_default_without_fabricating_an_item(self):
        with tempfile.TemporaryDirectory() as temp:
            self.pick(str(Path(temp) / "missing caff\u00e8"))
        self.shell.SHCreateItemFromParsingName.assert_not_called()

    def test_failure_to_register_cancellation_does_not_show_an_unclosable_dialog(self):
        self.user.SetTimer.side_effect = None
        self.user.SetTimer.return_value = 0
        with self.assertRaises(OSError):
            self.pick()
        self.assertNotIn(3, [slot for pointer, slot, _args in self.calls if pointer == 100])
        self.ole.CoUninitialize.assert_called_once()

    def test_com_initialization_failure_is_not_uninitialized(self):
        self.ole.CoInitializeEx.return_value = -2147024891
        with self.assertRaises(OSError):
            self.pick()
        self.ole.CoUninitialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
