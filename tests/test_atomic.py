#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Files written whole or not at all, the previous copy kept as .bak and
read back when the file is damaged; a rename Windows refuses for an
instant still saves, one refused for good is raised and leaves no
temporary file."""

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import _setup

atomic = _setup.shared("data.atomic")


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = self.dir / "work.json"
        atomic.write_json(self.file, {"name": "old"})
        self.old = self.file.read_bytes()

    def left(self) -> list:
        return sorted(p.name for p in self.dir.iterdir())

    def test_the_previous_copy_is_kept_and_read_back_when_the_file_is_damaged(self):
        atomic.write_json(self.file, {"name": "new"})
        self.assertEqual(atomic.read_json(self.file), ({"name": "new"}, None))
        self.assertEqual(atomic.read_json(self.dir / "work.json.bak"), ({"name": "old"}, None))
        self.file.write_text('{"name": "ne', encoding="utf-8")         # a crash mid-write elsewhere
        data, why = atomic.read_json(self.file)
        self.assertEqual(data, {"name": "old"})
        self.assertIn("work.json could not be read", why)
        self.file.unlink()
        self.assertEqual(atomic.read_json(self.file)[0], {"name": "old"})    # gone: the previous save stands in

    def test_without_a_backup_a_damaged_file_is_raised(self):
        lone = self.dir / "lone.json"
        atomic.write_json(lone, [1, 2], keep_backup=False)
        self.assertFalse((self.dir / "lone.json.bak").exists())
        lone.write_text("[1,", encoding="utf-8")
        with self.assertRaises(ValueError):
            atomic.read_json(lone)

    def test_a_write_that_fails_half_way_leaves_the_old_file_whole_and_no_temporary_file(self):
        with mock.patch.object(os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                atomic.write_json(self.file, {"name": "new"})
        self.assertEqual(self.file.read_bytes(), self.old)
        self.assertEqual(self.left(), ["work.json"])    # no .tmp left behind, no backup of a save that failed

    def test_a_backup_that_cannot_be_made_stops_the_save_and_leaves_the_old_file(self):
        with mock.patch.object(shutil, "copy2", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                atomic.write_json(self.file, {"name": "new"})
        self.assertEqual(self.file.read_bytes(), self.old)
        self.assertEqual(self.left(), ["work.json"])

    def test_an_optional_backup_that_cannot_be_made_does_not_stop_the_save(self):
        with mock.patch.object(shutil, "copy2", side_effect=PermissionError(13, "Permission denied")):
            atomic.write_json(self.file, {"name": "new"}, backup_optional=True)
        self.assertEqual(atomic.read_json(self.file), ({"name": "new"}, None))
        self.assertEqual(self.left(), ["work.json"])

    def test_a_backup_that_is_the_file_itself_is_left_alone(self):
        backup = self.dir / "work.json.bak"
        try:
            os.link(self.file, backup)          # what a save stopped between the link and the rename left
        except OSError:
            self.skipTest("no hard links here")
        atomic.write_json(self.file, {"name": "new"})
        self.assertEqual(atomic.read_json(self.file), ({"name": "new"}, None))
        self.assertEqual(atomic.read_json(backup), ({"name": "old"}, None))

    def test_the_indent_is_the_callers(self):
        atomic.write_json(self.file, {"a": {"b": 1}}, indent=2)
        self.assertEqual(self.file.read_bytes(), b'{\n  "a": {\n    "b": 1\n  }\n}\n')

    def test_bytes_written_whole(self):
        target = self.dir / "sub" / "out.bin"
        atomic.write_bytes(target, b"\x00\x01data")
        self.assertEqual(target.read_bytes(), b"\x00\x01data")
        with mock.patch.object(os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                atomic.write_bytes(target, b"other")
        self.assertEqual(target.read_bytes(), b"\x00\x01data")
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["out.bin"])


class PatientRename(unittest.TestCase):
    """Windows: an antivirus or the indexer holds the file just written for
    an instant, and the rename fails with PermissionError."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = self.dir / "config.json"
        atomic.write_json(self.file, {"theme": "dark"})

    def test_a_file_held_for_an_instant_by_another_program_is_still_saved(self):
        real, refusals = os.replace, [PermissionError(13, "held"), PermissionError(13, "held")]

        def held_at_first(src, dst):
            if refusals:
                raise refusals.pop()
            return real(src, dst)

        with mock.patch.object(os, "replace", side_effect=held_at_first), \
                mock.patch.object(time, "sleep") as slept:
            atomic.write_json(self.file, {"theme": "light"})
        self.assertEqual(atomic.read_json(self.file), ({"theme": "light"}, None))
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(sorted(os.listdir(self.dir)), ["config.json", "config.json.bak"])

    def test_a_refusal_that_lasts_is_raised_and_leaves_no_temporary_file(self):
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "held")), \
                mock.patch.object(time, "sleep") as slept, self.assertRaises(PermissionError):
            atomic.write_json(self.file, {"theme": "light"})
        self.assertEqual(slept.call_count, len(atomic.REPLACE_WAITS))
        self.assertEqual(atomic.read_json(self.file), ({"theme": "dark"}, None))
        self.assertEqual(sorted(os.listdir(self.dir)), ["config.json", "config.json.bak"])

    def test_another_error_is_not_tried_again(self):
        with mock.patch.object(os, "replace", side_effect=FileNotFoundError(2, "gone")), \
                mock.patch.object(time, "sleep") as slept, self.assertRaises(FileNotFoundError):
            atomic.replace_patiently(self.dir / "a", self.dir / "b")
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
