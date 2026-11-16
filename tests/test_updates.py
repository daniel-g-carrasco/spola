#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A newer version from the GitHub releases: versions, when a check is
due, the release's files, the installer downloaded and checked against
GitHub's SHA-256 and the project's signature, no more read than the
release says; a project with no release yet; where the release is asked
in a developer's trial; how Setup is started; which runs update by
themselves. Nothing goes on the network: the answers are local files or
mocks."""

import base64
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _setup
from test_minisign import PAYLOAD, PUBLIC_KEY, SIGNED_V8, SIGNED_V9

updates = _setup.shared("app.updates")

SLUG = "my-app"
REPO = f"someone/{SLUG}"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/v0.4.0/"
SETUP_NAME = f"{SLUG}-setup-v0.4.0-windows-x64.exe"
RELEASE = {
    "tag_name": "v0.4.0",
    "html_url": f"https://github.com/{REPO}/releases/tag/v0.4.0",
    "body": "What is new",
    "assets": [
        {"name": f"{SLUG}.flatpak", "browser_download_url": "https://example/app.flatpak", "size": 10,
         "digest": "sha256:aa"},
        {"name": f"{SLUG}-v0.4.0-windows-x64-portable.zip", "browser_download_url": "https://example/zip",
         "size": 20},
        {"name": SETUP_NAME, "browser_download_url": DOWNLOADS + SETUP_NAME, "size": 30, "digest": "sha256:BB"},
        {"name": SETUP_NAME + ".minisig", "browser_download_url": DOWNLOADS + SETUP_NAME + ".minisig", "size": 300},
    ],
}
SETUP, SIGNATURE = RELEASE["assets"][2], RELEASE["assets"][3]


def _signed_name(signature: str) -> str:
    return re.search(r"file:(\S+)", signature).group(1)


NAME = _signed_name(SIGNED_V9)                  # the installer the test signatures name
OTHER_NAME = _signed_name(SIGNED_V8)            # another release's, signed too
# a key that signs nothing here, the made-up project's: RFC 8032's first public
# key under a key id of its own, refused for the tests' signatures
OTHER_KEY = base64.b64encode(b"Ed" + b"my-app-1" + bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")).decode()
PROJECT = updates.Project(repo=REPO, slug=SLUG, exe_name="MyApp", version="0.3.10", public_key=OTHER_KEY)
SIGNED = dataclasses.replace(PROJECT, public_key=PUBLIC_KEY)    # the same, trusting the tests' key


class VersionTest(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(updates.is_newer("0.4.0", "0.3.10"))
        self.assertTrue(updates.is_newer("v0.3.11", "0.3.10"))
        self.assertFalse(updates.is_newer("0.3.10", "0.3.10"))
        self.assertFalse(updates.is_newer("0.3.9", "0.3.10"))           # not as text: 9 < 10
        self.assertTrue(updates.is_newer("1.0", "0.9.9"))
        self.assertEqual(updates.parse_version("v1.2.0-rc1"), (1, 2, 0))


class CheckDueTest(unittest.TestCase):
    """When a check made by itself is due: every few hours, never in a burst."""

    NOW = datetime.datetime(2026, 11, 14, 15, 30)

    def test_the_morning_check_of_the_once_a_day_rule_does_not_hide_an_afternoon_release(self):
        # the date alone, as the once-a-day check wrote it: a release out after
        # the morning's check waited until the next day; now the date is its midnight
        self.assertTrue(updates.check_due("2026-11-14", self.NOW))
        self.assertFalse(updates.check_due("2026-11-14", datetime.datetime(2026, 11, 14, 3, 0)))

    def test_a_check_is_due_after_the_hours_and_not_before(self):
        hours = updates.CHECK_EVERY_HOURS
        just_before = self.NOW - datetime.timedelta(hours=hours) + datetime.timedelta(minutes=1)
        self.assertFalse(updates.check_due(updates.check_stamp(just_before), self.NOW))
        self.assertTrue(updates.check_due(updates.check_stamp(self.NOW - datetime.timedelta(hours=hours)), self.NOW))
        self.assertFalse(updates.check_due(updates.check_stamp(self.NOW), self.NOW))

    def test_nothing_readable_or_a_time_to_come_makes_it_due(self):
        for last in ("", "  ", "yesterday", "2026-13-40", "2026-11-14T17:00:00"):
            with self.subTest(last=last):
                self.assertTrue(updates.check_due(last, self.NOW))

    def test_the_stamp_reads_back_as_the_time_of_the_check(self):
        stamp = updates.check_stamp(datetime.datetime(2026, 11, 14, 10, 23, 5, 123456))
        self.assertEqual(stamp, "2026-11-14T10:23:05")
        self.assertEqual(datetime.datetime.fromisoformat(stamp), datetime.datetime(2026, 11, 14, 10, 23, 5))


class ReleaseTest(unittest.TestCase):
    def test_the_release_and_its_installer(self):
        release = updates.parse_release(PROJECT, RELEASE)
        self.assertEqual((release.version, release.notes), ("0.4.0", "What is new"))
        installer = updates.installer_of(PROJECT, release)
        self.assertEqual(installer.name, SETUP_NAME)
        self.assertEqual(installer.sha256, "BB")
        self.assertEqual(installer.signature_url, SIGNATURE["browser_download_url"])
        self.assertIsNone(updates.installer_of(PROJECT, updates.parse_release(PROJECT, {**RELEASE, "assets": RELEASE["assets"][:2]})))
        with self.assertRaises(updates.UpdateError):
            updates.parse_release(PROJECT, {"assets": []})

    def test_only_a_released_installer_of_the_project_with_its_checksum_and_signature(self):
        def installer(signature=SIGNATURE, **changes):
            assets = [{**SETUP, **changes}] + ([signature] if signature else [])
            return updates.installer_of(PROJECT, updates.parse_release(PROJECT, {**RELEASE, "assets": assets}))
        self.assertIsNotNone(installer())
        self.assertIsNone(installer(digest=None))                                     # nothing to check it with
        self.assertIsNone(installer(signature=None))                                  # not signed
        self.assertIsNone(installer(signature={**SIGNATURE, "browser_download_url": "https://example.com/s.minisig"}))
        self.assertIsNone(installer(browser_download_url="https://example.com/" + SETUP_NAME))
        self.assertIsNone(installer(name=f"{SLUG}-setup-vdev-windows-x64.exe"))        # a CI build
        # another version's installer, duly signed, offered under a newer tag: not this release's
        self.assertIsNone(installer(name=f"{SLUG}-setup-v0.3.9-windows-x64.exe"))
        self.assertEqual(PROJECT.installer_name("v0.4.0"), SETUP_NAME)
        self.assertIsNone(installer(name=f"{SLUG}-setup-v0.4.0-evil-windows-x64.exe"))


class DownloadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = self.tmp / "setup.exe"
        self.addCleanup(updates.release_held)          # the verified installer is kept open
        self.source.write_bytes(PAYLOAD)
        self.signature = self.tmp / "setup.exe.minisig"
        self.signature.write_text(SIGNED_V9)

    def asset(self, **changes) -> updates.Asset:
        fields = {"name": NAME, "url": self.source.as_uri(), "size": len(PAYLOAD),
                  "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "signature_url": self.signature.as_uri()}
        return updates.Asset(**{**fields, **changes})

    def refused(self, asset: updates.Asset, project=SIGNED) -> None:
        dest = self.tmp / "got.exe"
        with self.assertRaises(updates.UpdateError):
            updates.download(project, asset, dest)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("got.exe.part").exists())

    def test_download_checked_against_its_sha256_and_signature(self):
        seen = []
        dest = updates.download(SIGNED, self.asset(), self.tmp / "got.exe", lambda done, total: seen.append((done, total)))
        self.assertEqual(dest.read_bytes(), PAYLOAD)
        self.assertEqual(seen[-1], (9000, 9000))

    def test_a_download_cut_short_or_without_checksum_is_refused(self):
        self.refused(self.asset(size=len(PAYLOAD) + 10))
        self.refused(self.asset(sha256=""))
        self.refused(self.asset(sha256="0" * 64))

    def test_no_more_is_read_than_the_release_says(self):
        seen = []
        with mock.patch.object(updates, "CHUNK", 1000), self.assertRaises(updates.UpdateError):
            updates.download(SIGNED, self.asset(size=3000), self.tmp / "got.exe", lambda done, total: seen.append(done))
        self.assertEqual(seen, [1000, 2000, 3000])           # the fourth chunk, past the size, not taken
        self.assertFalse((self.tmp / "got.exe.part").exists())

    def test_an_answer_too_big_for_a_release_is_refused(self):
        answer = self.tmp / "release.json"
        answer.write_text(json.dumps({**RELEASE, "body": "x" * updates.RELEASE_MAX_BYTES}))
        with mock.patch.dict(os.environ, {PROJECT.dev_release_variable: answer.as_uri()}):
            with self.assertRaises(updates.UpdateError):
                updates.latest_release(PROJECT)
            answer.write_text(json.dumps(RELEASE))
            self.assertEqual(updates.latest_release(PROJECT).version, "0.4.0")

    def test_an_installer_not_signed_for_this_release_is_refused(self):
        self.refused(self.asset(signature_url=""))
        # signed, with the name of another release's installer
        self.refused(self.asset(name=OTHER_NAME))
        self.signature.write_text(SIGNED_V8)
        self.refused(self.asset())
        # signed by a key that is not the project's
        self.signature.write_text(SIGNED_V9)
        self.refused(self.asset(), project=PROJECT)
        self.signature.write_text("Not Found")
        self.refused(self.asset())


class NoReleaseTest(unittest.TestCase):
    def test_no_release_yet_is_its_own_answer_not_a_failure(self):
        """GitHub answers 404 for the latest release of a project that has
        none: NoRelease, which a caller may take as nothing to update to; one
        that takes every UpdateError as a failure still does, and says
        GitHub's answer as before."""
        missing = urllib.error.HTTPError(PROJECT.latest_release_api, 404, "Not Found", {}, None)
        self.addCleanup(missing.close)
        with mock.patch.object(updates, "_get", side_effect=missing):
            with self.assertRaises(updates.NoRelease) as caught:
                updates.latest_release(PROJECT)
        self.assertIsInstance(caught.exception, updates.UpdateError)
        self.assertEqual(str(caught.exception), "GitHub answered 404")
        failing = urllib.error.HTTPError(PROJECT.latest_release_api, 500, "Server Error", {}, None)
        self.addCleanup(failing.close)
        with mock.patch.object(updates, "_get", side_effect=failing):
            with self.assertRaises(updates.UpdateError) as caught:
                updates.latest_release(PROJECT)
            self.assertNotIsInstance(caught.exception, updates.NoRelease)


class ReleaseSourceTest(unittest.TestCase):
    """Where the latest release is asked, and where its files must come from."""

    def test_githubs_without_the_developers_variables(self):
        self.assertEqual(updates.release_source(PROJECT, {}, frozen=False),
                         (PROJECT.latest_release_api, PROJECT.downloads))
        self.assertEqual(updates.release_source(PROJECT, {}, frozen=True),
                         (PROJECT.latest_release_api, PROJECT.downloads))

    def test_the_variables_are_named_after_the_prefix(self):
        self.assertEqual(PROJECT.dev_release_variable, "MY_APP_DEV_RELEASE_API")          # from the slug
        self.assertEqual(PROJECT.dev_downloads_variable, "MY_APP_DEV_RELEASE_DOWNLOADS")
        other = dataclasses.replace(PROJECT, env_prefix="MYAPP")
        self.assertEqual(other.dev_release_variable, "MYAPP_DEV_RELEASE_API")

    def test_a_trial_from_the_sources_needs_the_answer_only(self):
        api = "file:///trial/release.json"
        environ = {PROJECT.dev_release_variable: api}
        self.assertEqual(updates.release_source(PROJECT, environ, frozen=False), (api, PROJECT.downloads))
        # the frozen build takes it only with the prefix of the files too
        self.assertEqual(updates.release_source(PROJECT, environ, frozen=True),
                         (PROJECT.latest_release_api, PROJECT.downloads))
        environ[PROJECT.dev_downloads_variable] = "http://127.0.0.1:8000/"
        self.assertEqual(updates.release_source(PROJECT, environ, frozen=True), (api, "http://127.0.0.1:8000/"))

    def test_the_installer_of_a_trial_comes_from_the_prefix_given(self):
        local = "http://127.0.0.1:8000/"
        assets = [{**SETUP, "browser_download_url": local + SETUP_NAME},
                  {**SIGNATURE, "browser_download_url": local + SETUP_NAME + ".minisig"}]
        release = updates.parse_release(PROJECT, {**RELEASE, "assets": assets})
        self.assertIsNone(updates.installer_of(PROJECT, release))       # not GitHub's: refused
        variables = {PROJECT.dev_release_variable: "file:///trial/release.json",
                     PROJECT.dev_downloads_variable: local}
        with mock.patch.dict(os.environ, variables):
            self.assertEqual(updates.installer_of(PROJECT, release).name, SETUP_NAME)


class WhenTest(unittest.TestCase):
    def test_by_itself_in_the_windows_build_only(self):
        self.assertTrue(updates.checks_by_itself("win32", True))
        self.assertFalse(updates.checks_by_itself("win32", False))         # the sources ask nobody unless asked
        self.assertFalse(updates.checks_by_itself("linux", True))          # the Flatpak: the software centre

    def test_setup_is_started_silent_and_for_this_user_when_installed_so(self):
        exe = Path("C:/tmp/my-app-setup-v1.0.0-windows-x64.exe")
        with mock.patch.object(updates, "per_user_install", return_value=True):
            self.assertEqual(updates.installer_command(exe),
                             [str(exe), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER"])
            self.assertEqual(updates.installer_command(exe, restart=True)[-1], "/RESTARTAPP=1")
        with mock.patch.object(updates, "per_user_install", return_value=False):
            self.assertNotIn("/CURRENTUSER", updates.installer_command(exe))
            self.assertNotIn("/RESTARTAPP=1", updates.installer_command(exe))


class ChannelTest(unittest.TestCase):
    def test_how_this_run_was_delivered(self):
        self.assertEqual(updates.distribution_channel({"FLATPAK_ID": "io.example.MyApp"}), "flatpak")
        with mock.patch.object(updates, "installed_build", return_value=False):
            if not Path("/.flatpak-info").is_file():
                self.assertEqual(updates.distribution_channel({}), "source")       # the tests run from the sources
                with mock.patch.object(sys, "frozen", True, create=True):
                    self.assertEqual(updates.distribution_channel({}), "portable")
        with mock.patch.object(updates, "installed_build", return_value=True):
            if not Path("/.flatpak-info").is_file():
                self.assertEqual(updates.distribution_channel({}), "installed")

    def test_an_automatic_install_only_in_setups_build_and_only_when_everything_allows_it(self):
        with mock.patch.object(updates, "distribution_channel", return_value="installed"):
            self.assertTrue(updates.automatic_install_supported())
        for channel in ("portable", "flatpak", "source"):
            with mock.patch.object(updates, "distribution_channel", return_value=channel):
                self.assertFalse(updates.automatic_install_supported(), channel)
        yes = {"enabled": True, "opted_in": True, "channel": "installed", "signed_installer": True, "dirty": False}
        self.assertTrue(updates.automatic_install_allowed(**yes))
        for key, value in (("enabled", False), ("opted_in", False), ("channel", "portable"),
                           ("signed_installer", False), ("dirty", True)):
            with self.subTest(key=key):
                self.assertFalse(updates.automatic_install_allowed(**{**yes, key: value}))


class InstalledTest(unittest.TestCase):

    @unittest.skipUnless(_setup.WINDOWS, "Windows only")
    def test_a_verified_installer_cannot_be_replaced_or_renamed_but_still_runs(self):
        import shutil
        import subprocess
        folder = Path(tempfile.mkdtemp(prefix=f"{SLUG}-hold-"))
        try:
            exe = folder / "held.exe"
            shutil.copy2(os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe"), exe)
            updates.hold(exe)
            with self.assertRaises(OSError):
                os.replace(exe, folder / "renamed.exe")                 # no delete sharing: no rename
            with self.assertRaises(OSError):
                open(exe, "r+b").close()                                # no write sharing
            with self.assertRaises(OSError):
                exe.unlink()
            self.assertTrue(exe.exists())
            done = subprocess.run([str(exe)], capture_output=True, timeout=30)   # Setup only reads it
            self.assertEqual(done.returncode, 0)
        finally:
            updates.release_held()
            shutil.rmtree(folder, ignore_errors=True)

    def test_installed_build_only_when_frozen(self):
        self.assertFalse(updates.installed_build())     # the tests run from the sources


class NotesTest(unittest.TestCase):
    """A release's notes, as the update window shows them."""

    def test_the_changelog_section_as_headings_and_items(self):
        notes = ("### Fixed\n- One thing that goes on\n  for two lines, with `code` and **bold**.\n- Another.\n\n"
                 "### Changed\n* A star item.\n\nA paragraph of its own.\n\n"
                 "**Full Changelog**: https://github.com/x/y/compare/v1...v2\n")
        self.assertEqual(updates.notes_sections(notes), [
            ("Fixed", ["One thing that goes on for two lines, with code and bold.", "Another."]),
            ("Changed", ["A star item.", "A paragraph of its own."])])

    def test_notes_without_headings_and_githubs_one_line(self):
        self.assertEqual(updates.notes_sections("Just a line.\n\n- and an item"), [("", ["Just a line.", "and an item"])])
        self.assertEqual(updates.notes_sections("**Full Changelog**: https://github.com/x/y/compare/v1...v2"), [])
        self.assertEqual(updates.notes_sections(""), [])

    def test_no_more_items_than_the_window_can_show(self):
        notes = "### Fixed\n" + "\n".join(f"- item {i}" for i in range(30)) + "\n### Added\n- late"
        self.assertEqual(updates.notes_sections(notes, limit=5), [("Fixed", [f"item {i}" for i in range(5)])])


if __name__ == "__main__":
    unittest.main()
