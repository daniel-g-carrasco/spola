#!/usr/bin/env python3
"""A release is published only when every file is there: the installed app
asks GitHub for the latest release, and 0.3.41, out with the Flatpak alone
because its Windows build had failed, offered a version nobody on Windows
could install. The step that publishes is the same in both workflows and
is run here against a stand-in for gh."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _setup

ROOT = _setup.ROOT
FILES = ("spola.flatpak", "spola-setup-v1.2.3-windows-x64.exe", "spola-setup-v1.2.3-windows-x64.exe.minisig",
         "spola-v1.2.3-windows-x64-portable.zip", "spola-v1.2.3-windows-x64-portable.zip.minisig")


def step(workflow: str) -> str:
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    found = re.search(r"- name: Published when every file is there\n(.*?)(?=\n      - name: |\n  \w|\Z)", text, re.S)
    assert found is not None, f"{workflow} has no step that publishes the release when it is complete"
    return found.group(1)


def script() -> str:
    lines = step("build.yml").split("        run: |\n", 1)[1].splitlines()
    return "\n".join(line[10:] for line in lines) + "\n"


class CompleteReleaseTest(unittest.TestCase):
    def test_the_step_is_the_same_in_both_workflows(self):
        self.assertEqual(step("build.yml").rstrip(), step("windows.yml").rstrip())

    def test_the_release_is_made_as_a_draft(self):
        build = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        create = re.search(r"gh release create .*?(?=\n          gh release upload)", build, re.S).group()
        self.assertIn("--draft", create)
        self.assertNotIn("--draft=false", create)

    def run_with(self, names) -> tuple[str, str]:
        """(what the step printed, what it asked gh to do) with those files in the release."""
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("no bash to run the workflow's step with")
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "names.txt").write_text("".join(name + "\n" for name in names), encoding="utf-8", newline="\n")
        # the stand-in: "release view" gives the names, anything else is written down
        (tmp / "gh").write_text('#!/bin/sh\n'
                                'if [ "$2" = view ]; then cat "$GH_STAND_IN/names.txt"; '
                                'else echo "$@" >> "$GH_STAND_IN/asked.txt"; fi\n',
                                encoding="utf-8", newline="\n")
        # relative names, and the folder as the working one: the bash found
        # may be Git's, MSYS2's or WSL's, and each spells a Windows path its
        # own way; the tag and the repository are set in the script (WSL's
        # bash does not take Windows' variables)
        (tmp / "step.sh").write_text('export GH_STAND_IN="$PWD" GITHUB_REF_NAME=v1.2.3 GITHUB_REPOSITORY=me/app\n'
                                     'PATH="$PWD:$PATH"\nchmod +x gh\n' + script(),
                                     encoding="utf-8", newline="\n")
        done = subprocess.run([bash, "step.sh"], capture_output=True, text=True, cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        asked = (tmp / "asked.txt").read_text(encoding="utf-8") if (tmp / "asked.txt").exists() else ""
        return done.stdout, asked

    def test_every_file_there_and_it_is_published(self):
        out, asked = self.run_with(FILES)
        self.assertIn("published", out)
        self.assertEqual(asked.split(), ["release", "edit", "v1.2.3", "--repo", "me/app", "--draft=false", "--latest"])

    def test_one_file_missing_and_it_stays_a_draft(self):
        for missing in FILES:
            with self.subTest(missing):
                out, asked = self.run_with([name for name in FILES if name != missing])
                self.assertIn("stays a draft", out)
                self.assertEqual(asked, "")

    def test_another_versions_installer_does_not_count(self):
        out, asked = self.run_with([name.replace("v1.2.3-windows-x64.exe", "v1.2.2-windows-x64.exe") for name in FILES])
        self.assertIn("stays a draft", out)
        self.assertEqual(asked, "")


if __name__ == "__main__":
    unittest.main()
