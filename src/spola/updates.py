"""A newer Spola, from the GitHub releases (Windows; on Linux the Flatpak
brings it, from GNOME Software).

As in p7m-extractor: the latest release from GitHub's API, standard library
only, at most once a day at start and whenever asked. The installed build
downloads the release's installer, checks it against the SHA-256 GitHub
publishes for it, and starts it: Setup asks the running Spola to quit
(rclone stopped with its state saved), replaces the files and starts it
again. The portable build and a run from the sources open the release page.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .i18n import _
from .koofr_api import _ssl_context

GITHUB_REPO = "daniel-g-carrasco/spola"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
DOWNLOADS = f"https://github.com/{GITHUB_REPO}/releases/download/"
# a released version only: never the "dev" builds of the CI
INSTALLER = re.compile(r"^spola-setup-v\d+(\.\d+){1,3}-windows-x64\.exe$", re.IGNORECASE)
PORTABLE = re.compile(r"^spola-v\d+(\.\d+){1,3}-windows-x64-portable\.zip$", re.IGNORECASE)
DOWNLOAD_FOLDER_PREFIX = "spola-update-"


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str = ""            # from GitHub's "digest", when it gives one


@dataclass
class Release:
    version: str
    url: str                    # the release's page
    notes: str
    assets: list[Asset] = field(default_factory=list)


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.0' -> (1, 2, 0); what is not a number counts as 0."""
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def installed_build() -> bool:
    """The Inno Setup installation (its uninstaller next to Spola.exe), not
    the portable zip nor the sources."""
    return (sys.platform == "win32" and bool(getattr(sys, "frozen", False))
            and (Path(sys.executable).parent / "unins000.exe").is_file())


def _get(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"User-Agent": f"Spola/{__version__}",
                                                   "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


def parse_release(data: dict) -> Release:
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError(_("unexpected answer from GitHub"))
    assets = []
    for a in data.get("assets") or []:
        digest = str(a.get("digest") or "")
        assets.append(Asset(str(a.get("name") or ""), str(a.get("browser_download_url") or ""),
                            int(a.get("size") or 0), digest[7:] if digest.startswith("sha256:") else ""))
    return Release(tag.lstrip("vV"), str(data.get("html_url") or RELEASES_URL), str(data.get("body") or ""), assets)


def latest_release(timeout: float = 15.0) -> Release:
    # developer aid, as SPOLA_DEV_REMOTE: a release described in a local file,
    # to try the dialog without publishing one; never in the built app
    url = LATEST_RELEASE_API
    if not getattr(sys, "frozen", False):
        url = os.environ.get("SPOLA_DEV_RELEASE_API") or url
    try:
        with _get(url, timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        raise UpdateError(_("GitHub answered {code}").format(code=e.code)) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(str(getattr(e, "reason", e))) from None
    return parse_release(data)


def installer_of(release: Release) -> Asset | None:
    """The release's installer: a released version, from Spola's own
    releases, with the SHA-256 GitHub gives for it. Else None: the dialog
    then opens the release page."""
    return next((a for a in release.assets
                 if INSTALLER.match(a.name) and a.url.startswith(DOWNLOADS) and a.sha256), None)


def download_folder() -> Path:
    """A new folder of its own for an installer, in the temporary folder."""
    return Path(tempfile.mkdtemp(prefix=DOWNLOAD_FOLDER_PREFIX))


def clean_downloads() -> None:
    """The installers of earlier updates, gone (Setup has finished with them
    by the time Spola starts again)."""
    for folder in Path(tempfile.gettempdir()).glob(DOWNLOAD_FOLDER_PREFIX + "*"):
        shutil.rmtree(folder, ignore_errors=True)


def download(asset: Asset, dest: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: threading.Event | None = None) -> Path:
    """The asset, in dest, checked against its size and its SHA-256: a file
    that does not match is removed; without a SHA-256 nothing is downloaded."""
    if not asset.sha256:
        raise UpdateError(_("the release gives no checksum for the installer: not downloaded"))
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with _get(asset.url, 30) as response, open(part, "wb") as out:
            total = int(response.headers.get("Content-Length") or asset.size or 0)
            done = 0
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise UpdateError(_("cancelled"))
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        # a connection cut short ends the reading without an error
        if (asset.size and done != asset.size) or digest.hexdigest().lower() != asset.sha256.lower():
            raise UpdateError(_("the file downloaded does not match the release: not started"))
        part.replace(dest)
        return dest
    except urllib.error.HTTPError as e:
        raise UpdateError(_("download failed ({code})").format(code=e.code)) from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(str(getattr(e, "reason", e))) from None
    finally:
        try:
            part.unlink()
        except OSError:
            pass
