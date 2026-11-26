"""Check the rclone shipped by Spola against OSV."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_rclone_advisories as check  # noqa: E402


class RcloneAdvisoriesTest(unittest.TestCase):
    def test_new_advisory_is_reported_without_changing_the_packaged_version(self):
        version = check.packaged_version()
        output = io.StringIO()
        with (
            patch.object(check, "urlopen", side_effect=[
                io.BytesIO(b"{}"),
                io.BytesIO(b'{"vulns":[{"id":"TEST-RCLONE-1"}]}'),
            ]) as request,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(check.main(), 0)
            self.assertEqual(check.main(), 1)

        queries = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual(queries, [{
            "package": {"ecosystem": "Go", "name": "github.com/rclone/rclone"},
            "version": version,
        }] * 2)
        self.assertIn("https://osv.dev/vulnerability/TEST-RCLONE-1", output.getvalue())

    def test_network_and_invalid_response_exit_without_claiming_a_clean_scan(self):
        for response in (URLError("offline fixture"), io.BytesIO(b'{"vulns":[{"id":null}]}')):
            with self.subTest(response=response):
                output = io.StringIO()
                with (
                    patch.object(check, "urlopen", side_effect=[response]),
                    contextlib.redirect_stderr(output),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(check.main(), 2)
                self.assertIn("OSV check unavailable", output.getvalue())
                self.assertNotIn("No advisories", output.getvalue())


if __name__ == "__main__":
    unittest.main()
