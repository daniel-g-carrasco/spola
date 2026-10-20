#!/usr/bin/env python3
"""rclone from the window on Windows: a console without a window (no flash
where Windows Terminal hosts the consoles), still reached by Ctrl+Break."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import _setup

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

from spola import win32  # noqa: E402

CHILD = textwrap.dedent("""
    import os, subprocess, sys, time
    sys.path.insert(0, {src!r})
    from spola import win32
    assert not win32.has_console()
    env = {{k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}}
    p = subprocess.Popen([{rclone!r}, "rcd", "--rc-addr", "127.0.0.1:0", "--rc-no-auth", "-v"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         text=True, env=env, **win32.popen_options())
    time.sleep(3)
    sent = win32.interrupt(p)
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()
    print("sent", sent)
    print(p.stderr.read())
""")


class PopenOptionsTest(unittest.TestCase):
    def test_no_console_window_for_rclone(self):
        with mock.patch.object(win32, "has_console", return_value=False):
            options = win32.popen_options()
        flags = options["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertFalse(flags & subprocess.CREATE_NEW_CONSOLE)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertNotIn("startupinfo", options)

    @unittest.skipUnless(_setup.rclone_binary(), "rclone")
    def test_the_window_stops_rclone_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            script, out = Path(tmp) / "child.py", Path(tmp) / "out.txt"
            script.write_text(CHILD.format(src=str(_setup.ROOT / "src"), rclone=_setup.rclone_binary()))
            with open(out, "w") as f:
                # as the window: a process without a console
                subprocess.run([sys.executable, str(script)], stdout=f, stderr=subprocess.STDOUT,
                               creationflags=subprocess.DETACHED_PROCESS, timeout=60,
                               env={k: v for k, v in os.environ.items() if k != "MSYSTEM"})
            text = out.read_text(errors="replace")
        self.assertIn("sent True", text)
        self.assertIn("Signal received: interrupt", text)


class RunsWhenOpenedTest(unittest.TestCase):
    def test_programs_scripts_and_shortcuts_are_not_opened(self):
        # a kept copy shown in its folder instead of run ("Open a Copy")
        for name in ("setup.exe", "Fattura.pdf.exe", "avvio.LNK", "x.bat", "x.cmd", "x.ps1", "x.vbs",
                     "x.js", "x.hta", "x.scr", "x.msi", "x.reg", "x.url"):
            self.assertTrue(win32.runs_when_opened(rf"C:\tmp\{name}"), name)
        for name in ("relazione.pdf", "tavola.dwg", "foglio.xlsx", "note.txt", "foto.jpg", "senza-estensione"):
            self.assertFalse(win32.runs_when_opened(rf"C:\tmp\{name}"), name)


if __name__ == "__main__":
    unittest.main()
