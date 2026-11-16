#!/usr/bin/env python3
"""Spola's own side of the updates: its project finds its own installer in
a release and nothing else, the CI checks the signatures with the key Spola
trusts, that key is not the tests' one. The rest is the shared module's
test (test_updates.py, a copy of the model's, with a made-up project)."""

import unittest

import _setup
from test_minisign import PAYLOAD, SIGNED_V9, digest_of

from spola import PUBLIC_KEY as SPOLA_KEY            # the real key: the tests sign with a key of their own
from spola.app import minisign, services, updates

DOWNLOADS = "https://github.com/daniel-g-carrasco/spola/releases/download/v0.4.0/"
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
         "browser_download_url": DOWNLOADS + "spola-setup-v0.4.0-windows-x64.exe",
         "size": 30, "digest": "sha256:BB"},
        {"name": "spola-setup-v0.4.0-windows-x64.exe.minisig",
         "browser_download_url": DOWNLOADS + "spola-setup-v0.4.0-windows-x64.exe.minisig", "size": 300},
    ],
}
SETUP, SIGNATURE = RELEASE["assets"][2], RELEASE["assets"][3]
PROJECT = services.project()


class ProjectTest(unittest.TestCase):
    def test_spolas_project_finds_its_own_installer_in_its_release(self):
        release = updates.parse_release(PROJECT, RELEASE)
        self.assertEqual((release.version, release.notes), ("0.4.0", "What is new"))
        installer = updates.installer_of(PROJECT, release)
        self.assertEqual(installer.name, "spola-setup-v0.4.0-windows-x64.exe")
        self.assertEqual(installer.sha256, "BB")
        self.assertEqual(installer.signature_url, SIGNATURE["browser_download_url"])
        self.assertEqual(PROJECT.installer_name("v0.4.0"), "spola-setup-v0.4.0-windows-x64.exe")
        # the names Spola had before the shared module: the same requests, the same files
        self.assertEqual(PROJECT.dev_release_variable, "SPOLA_DEV_RELEASE_API")
        self.assertEqual(PROJECT.download_folder_prefix, "spola-update-")
        self.assertEqual(PROJECT.latest_release_api,
                         "https://api.github.com/repos/daniel-g-carrasco/spola/releases/latest")

    def test_only_a_released_installer_of_spola_with_its_checksum_and_signature(self):
        def installer(signature=SIGNATURE, **changes):
            assets = [{**SETUP, **changes}] + ([signature] if signature else [])
            return updates.installer_of(PROJECT, updates.parse_release(PROJECT, {**RELEASE, "assets": assets}))
        self.assertIsNotNone(installer())
        self.assertIsNone(installer(digest=None))                                     # nothing to check it with
        self.assertIsNone(installer(signature=None))                                  # not signed
        self.assertIsNone(installer(signature={**SIGNATURE, "browser_download_url": "https://example.com/s.minisig"}))
        self.assertIsNone(installer(browser_download_url="https://example.com/spola-setup-v0.4.0-windows-x64.exe"))
        self.assertIsNone(installer(name="spola-setup-vdev-windows-x64.exe"))          # a CI build
        # another version's installer, duly signed, offered under a newer tag: not this release's
        self.assertIsNone(installer(name="spola-setup-v0.3.9-windows-x64.exe"))
        self.assertIsNone(installer(name="spola-setup-v0.4.0-evil-windows-x64.exe"))


class KeyTest(unittest.TestCase):
    def test_the_ci_checks_its_signatures_with_the_key_spola_trusts(self):
        workflow = (_setup.ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")
        self.assertIn(f"PUBLIC_KEY: {SPOLA_KEY}\n", workflow)
        self.assertEqual(PROJECT.public_key, SPOLA_KEY)

    def test_spolas_key_is_not_the_one_the_tests_sign_with(self):
        with self.assertRaisesRegex(minisign.SignatureError, "another key"):
            minisign.verify(digest_of(PAYLOAD), SIGNED_V9, SPOLA_KEY)


if __name__ == "__main__":
    unittest.main()
