#!/usr/bin/env python3
"""A newer Spola from the GitHub releases: versions, the release's files,
the installer downloaded and checked against GitHub's SHA-256."""

import hashlib
import tempfile
import unittest
from pathlib import Path

import _setup  # noqa: F401

from spola import updates

RELEASE = {
    "tag_name": "v0.4.0",
    "html_url": "https://github.com/daniel-g-carrasco/spola/releases/tag/v0.4.0",
    "body": "What is new",
    "assets": [
        {"name": "spola.flatpak", "browser_download_url": "https://example/spola.flatpak", "size": 10,
         "digest": "sha256:aa"},
        {"name": "spola-v0.4.0-windows-x64-portable.zip", "browser_download_url": "https://example/zip",
         "size": 20},
        {"name": "spola-setup-v0.4.0-windows-x64.exe",
         "browser_download_url": "https://github.com/daniel-g-carrasco/spola/releases/download/v0.4.0/"
                                 "spola-setup-v0.4.0-windows-x64.exe",
         "size": 30, "digest": "sha256:BB"},
    ],
}
SETUP = RELEASE["assets"][2]


class VersionTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(updates.is_newer("0.4.0", "0.3.10"))
        self.assertTrue(updates.is_newer("v0.3.11", "0.3.10"))
        self.assertFalse(updates.is_newer("0.3.10", "0.3.10"))
        self.assertFalse(updates.is_newer("0.3.9", "0.3.10"))           # not as text: 9 < 10
        self.assertTrue(updates.is_newer("1.0", "0.9.9"))
        self.assertEqual(updates.parse_version("v1.2.0-rc1"), (1, 2, 0))


class ReleaseTest(unittest.TestCase):
    def test_the_release_and_its_installer(self):
        release = updates.parse_release(RELEASE)
        self.assertEqual((release.version, release.notes), ("0.4.0", "What is new"))
        installer = updates.installer_of(release)
        self.assertEqual(installer.name, "spola-setup-v0.4.0-windows-x64.exe")
        self.assertEqual(installer.sha256, "BB")
        self.assertIsNone(updates.installer_of(updates.parse_release({**RELEASE, "assets": RELEASE["assets"][:2]})))
        with self.assertRaises(updates.UpdateError):
            updates.parse_release({"assets": []})

    def test_only_a_released_installer_of_spola_with_its_checksum(self):
        def installer(**changes):
            return updates.installer_of(updates.parse_release({**RELEASE, "assets": [{**SETUP, **changes}]}))
        self.assertIsNotNone(installer())
        self.assertIsNone(installer(digest=None))                                     # nothing to check it with
        self.assertIsNone(installer(browser_download_url="https://example.com/spola-setup-v0.4.0-windows-x64.exe"))
        self.assertIsNone(installer(name="spola-setup-vdev-windows-x64.exe"))          # a CI build
        self.assertIsNone(installer(name="spola-setup-v0.4.0-evil-windows-x64.exe"))

    def test_a_download_cut_short_or_without_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "setup.exe"
            source.write_bytes(b"installer" * 1000)
            good = hashlib.sha256(source.read_bytes()).hexdigest()
            longer = updates.Asset("s.exe", source.as_uri(), source.stat().st_size + 10, good)
            with self.assertRaises(updates.UpdateError):
                updates.download(longer, Path(tmp) / "short.exe")
            self.assertFalse((Path(tmp) / "short.exe").exists())
            unchecked = updates.Asset("s.exe", source.as_uri(), source.stat().st_size, "")
            with self.assertRaises(updates.UpdateError):
                updates.download(unchecked, Path(tmp) / "unchecked.exe")
            self.assertFalse((Path(tmp) / "unchecked.exe").exists())

    def test_download_checked_against_its_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "setup.exe"
            source.write_bytes(b"installer" * 1000)
            good = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = updates.Asset("spola-setup-v9-windows-x64.exe", source.as_uri(), source.stat().st_size, good)
            seen = []
            dest = updates.download(asset, Path(tmp) / "got.exe", lambda done, total: seen.append((done, total)))
            self.assertEqual(dest.read_bytes(), source.read_bytes())
            self.assertEqual(seen[-1], (9000, 9000))
            bad = updates.Asset(asset.name, asset.url, asset.size, "0" * 64)
            with self.assertRaises(updates.UpdateError):
                updates.download(bad, Path(tmp) / "bad.exe")
            self.assertFalse((Path(tmp) / "bad.exe").exists())
            self.assertFalse((Path(tmp) / "bad.exe.part").exists())

    def test_installed_build_only_when_frozen(self):
        self.assertFalse(updates.installed_build())     # the tests run from the sources


if __name__ == "__main__":
    unittest.main()
