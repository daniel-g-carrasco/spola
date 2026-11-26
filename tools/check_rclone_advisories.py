#!/usr/bin/env python3
"""Report published OSV advisories for the rclone module shipped by Spola."""

import json
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MODULE = "github.com/rclone/rclone"
QUERY_URL = "https://api.osv.dev/v1/query"


def packaged_version():
    windows = ROOT / ".github/workflows/windows.yml"
    flatpak = ROOT / "build-aux/flatpak/io.github.daniel_g_carrasco.spola.yaml"
    win = re.findall(r'^  SPOLA_RCLONE_RELEASE: "([0-9]+\.[0-9]+\.[0-9]+)"$',
                     windows.read_text(encoding="utf-8"), re.MULTILINE)
    lin = re.findall(r'^ +url: https://github\.com/rclone/rclone/releases/download/'
                     r'v([0-9]+\.[0-9]+\.[0-9]+)/rclone-v\1\.tar\.gz$',
                     flatpak.read_text(encoding="utf-8"), re.MULTILINE)
    if len(win) != 1 or win != lin:
        raise ValueError("Windows and Flatpak must pin the same rclone release")
    return f"v{win[0]}"


def advisories(version):
    query = {"package": {"ecosystem": "Go", "name": MODULE}, "version": version}
    matches = set()
    seen_tokens = set()
    while True:
        request = Request(QUERY_URL, data=json.dumps(query).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read(10_000_000))
        if (
            not isinstance(result, dict)
            or result.keys() - {"vulns", "next_page_token"}
            or not isinstance(result.get("vulns", []), list)
        ):
            raise ValueError("unexpected OSV response")
        for entry in result.get("vulns", []):
            advisory_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(advisory_id, str) or not re.fullmatch(r"[A-Za-z0-9-]+", advisory_id):
                raise ValueError("unexpected OSV advisory identifier")
            matches.add(advisory_id)
        token = result.get("next_page_token")
        if not token:
            return sorted(matches)
        if not isinstance(token, str) or token in seen_tokens:
            raise ValueError("invalid OSV pagination")
        seen_tokens.add(token)
        query["page_token"] = token


def main():
    try:
        version = packaged_version()
        matches = advisories(version)
    except (OSError, ValueError) as error:
        print(f"OSV check unavailable: {error}", file=sys.stderr)
        return 2
    if not matches:
        print(f"No advisories returned by OSV for {MODULE} {version}.")
        return 0
    print(f"Advisories for {MODULE} {version}:")
    for advisory in matches:
        print(f"https://osv.dev/vulnerability/{advisory}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
