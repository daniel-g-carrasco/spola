#!/usr/bin/env python3
"""The version about to be released has its notes: the release's page and
the update window take them from CHANGELOG.md (tools/release_notes.py),
and a release without them would not be made."""

import importlib.util
import unittest
from pathlib import Path

import _setup

from spola import __version__

ROOT = Path(_setup.ROOT)
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)


class ReleaseNotesTest(unittest.TestCase):
    def test_this_version_has_its_section_in_the_changelog_and_in_the_metainfo(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        notes = release_notes.notes(__version__, changelog)
        self.assertTrue(notes, f"CHANGELOG.md has no section for {__version__}")
        self.assertNotIn("\n## [", notes)
        metainfo = next((ROOT / "data").glob("*.metainfo.xml")).read_text(encoding="utf-8")
        self.assertIn(f'<release version="{__version__}"', metainfo)

    def test_a_section_ends_where_the_next_begins(self):
        text = "# Changelog\n\n## [2.0] - today\n\n### Fixed\n- b\n\n## [1.0] - before\n\n- a\n\n[2.0]: https://x\n"
        self.assertEqual(release_notes.notes("2.0", text), "### Fixed\n- b")
        self.assertEqual(release_notes.notes("1.0", text), "- a")
        self.assertEqual(release_notes.notes("3.0", text), "")


if __name__ == "__main__":
    unittest.main()
