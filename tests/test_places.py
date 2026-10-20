"""Where things are (places.py): a path inside another here and on Koofr,
"~" for the home folder, a free name; on Windows whatever the case and the
separators, which the helpers it replaced got wrong with a fixed "/"."""

import os
import tempfile
import unittest
from unittest import mock

import _setup

from spola import grouping, places, remote_folders
from spola.config import Pair


class PlacesTest(unittest.TestCase):
    def test_inside_or_not(self):
        self.assertTrue(places.within("/h/u/data/work", "/h/u/data"))
        self.assertTrue(places.within("/h/u/data/", "/h/u/data"))
        self.assertTrue(places.within("/anything", "/"))
        self.assertFalse(places.within("/h/u/database", "/h/u/data"))     # not a prefix of names
        self.assertFalse(places.within("/h/u/data/../other", "/h/u/data"))
        self.assertEqual(places.relative("/h/u/data/work/a.txt", "/h/u/data"), "work/a.txt")
        self.assertEqual(places.relative("/h/u/data", "/h/u/data/"), "")
        self.assertIsNone(places.relative("/h/u/other", "/h/u/data"))
        self.assertEqual(places.home_relative("/h/u/data/work", "/h/u"), "~/data/work")
        self.assertEqual(places.home_relative("/h/u", "/h/u"), "~")
        self.assertEqual(places.home_relative("/srv/x", "/h/u"), "/srv/x")

    def test_koofr_ignores_case(self):
        self.assertTrue(places.remote_within("/Data/Work", "/data"))
        self.assertTrue(places.remote_within("/data", "/"))
        self.assertFalse(places.remote_within("/database", "/data"))

    @unittest.skipUnless(_setup.WINDOWS, "Windows paths")
    def test_windows_case_and_separators(self):
        home = "C:\\Users\\Me"
        self.assertTrue(places.within("c:/users/ME/Elettrico", home))
        self.assertEqual(places.relative("C:\\USERS\\me\\data\\Work\\a.txt", home), "data/Work/a.txt")
        self.assertEqual(places.home_relative("C:\\Users\\Me\\data\\work", home), "~\\data\\work")
        self.assertEqual(places.home_relative("C:/Users/Me/data/work", home), "~/data/work")
        self.assertFalse(places.within("C:\\Users\\Mena", home))
        self.assertFalse(places.within("D:\\Users\\Me", home))
        # NTFS folds case letter by letter: "ß" is not "ss"
        self.assertFalse(places.within("C:\\Straße\\x", "C:\\Strasse"))
        # the window: the home folder by "~", "Same place" on Koofr, groups merged
        self.assertEqual(grouping.expected_remote("C:\\Users\\Me\\data\\work", home), "/data/work")
        pairs = [Pair("a", "C:\\Users\\Me\\data\\work", "/data/work"),
                 Pair("b", "c:/users/me/data/media/photos", "/data/media/photos")]
        (parent, rows), = grouping.group_pairs(pairs)
        self.assertEqual(places.local_key(parent), places.local_key("C:\\Users\\Me\\data"))
        self.assertEqual(sorted(r[1].replace("\\", "/") for r in rows), ["media/photos", "work"])

    def test_the_same_place_on_koofr_is_suggested(self):
        # the home folder found in a path written either way (B-07: with a
        # fixed "/" Windows never got this suggestion)
        class Folders:
            def list(self, path):
                return {"/": ["data"], "/data": ["work"], "/data/work": []}.get(path, [])
        with tempfile.TemporaryDirectory() as home:
            local = os.path.join(home, "data", "work")
            os.makedirs(local)
            found = remote_folders.suggestions(Folders(), local, [], home=home)
        self.assertEqual(found[0].path, "/data/work")
        self.assertTrue(found[0].exists)

    def test_a_free_name(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(places.free_name(root, "a.txt"), "a.txt")
            open(os.path.join(root, "a.txt"), "w").close()
            open(os.path.join(root, "a (2).txt"), "w").close()
            self.assertEqual(places.free_name(root, "a.txt"), "a (3).txt")
            with mock.patch("os.path.lexists", return_value=True), self.assertRaises(FileExistsError):
                places.free_name(root, "a.txt")


if __name__ == "__main__":
    unittest.main()
