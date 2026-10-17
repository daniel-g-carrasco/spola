"""The few Koofr REST calls Spola makes itself, with the same app password
as rclone (HTTP Basic). Everything that changes files goes through rclone;
the writes here are creating an empty folder, when the user asks for one
while choosing where a new folder syncs to, and restoring from the trash
what the user chose.

  GET  /api/v2/mounts                         the primary mount id (links to the web app)
  GET  /api/v2.1/user/activity                what changed on Koofr, and who changed it
  GET  /api/v2/mounts/{id}/files/list         the folders on Koofr, one level at a time
  POST /api/v2/mounts/{id}/files/folder       a new empty folder
  GET  /api/v2/trash                          the trash, newest first, a page at a time
  POST /api/v2/trash/undelete                 restore some of it (a job)
  GET  /api/v2/jobs/{id}                      how that job is going

The activity feed is not documented by Koofr (it is used by the web app and
by third-party tools): Spola treats it as a doorbell only. When it fails,
the periodic full run catches every change anyway.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import KOOFR_WEB, __version__

TIMEOUT = 30
ACTIVITY_LIMIT = 100


class ApiError(Exception):
    pass


class NotInTrash(ApiError):
    """Undelete answered 404: the files are not in the trash (restored already)."""


@dataclass(frozen=True)
class Activity:
    id: str
    timestamp: int          # ms since the epoch
    type: str               # file_uploaded, file_deleted, file_moved, file_dir_created...
    mount_id: str
    path: str
    new_path: str
    user_agent: str


def _ssl_context():
    """The frozen Windows build ships a CA bundle: MSYS2's OpenSSL does not
    always see the Windows certificate store (as in p7m-extractor)."""
    import ssl
    import sys
    from pathlib import Path
    context = ssl.create_default_context()
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "certs" / "ca-bundle.crt"
    if getattr(sys, "frozen", False) and bundled.is_file():
        try:
            context.load_verify_locations(cafile=str(bundled))
        except (ssl.SSLError, OSError):
            pass
    return context


def _get(user: str, password: str, path: str, params: dict | None = None) -> dict:
    url = KOOFR_WEB + path + ("?" + urllib.parse.urlencode(params) if params else "")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}", "Accept": "application/json",
        "User-Agent": f"Spola/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=_ssl_context()) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ApiError(f"{path}: {e}") from None


def _post_json(user: str, password: str, path: str, body: dict) -> dict:
    url = KOOFR_WEB + path
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Basic {token}", "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": f"Spola/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=120, context=_ssl_context()) as response:
            text = response.read()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        if e.code == 404 and path.endswith("/undelete"):
            raise NotInTrash(path) from None
        raise ApiError(f"HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ApiError(f"{path}: {e}") from None


def _post(user: str, password: str, path: str, params: dict, body: dict) -> None:
    url = KOOFR_WEB + path + "?" + urllib.parse.urlencode(params)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Basic {token}", "Content-Type": "application/json",
        "User-Agent": f"Spola/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=_ssl_context()):
            pass
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{path}: {e}") from None


def list_folders(user: str, password: str, mount_id: str, path: str) -> list[str]:
    """The names of the folders inside `path` on Koofr (one level)."""
    data = _get(user, password, f"/api/v2/mounts/{mount_id}/files/list", {"path": path or "/"})
    return sorted((f["name"] for f in data.get("files", []) if f.get("type") == "dir"), key=str.casefold)


def create_folder(user: str, password: str, mount_id: str, parent: str, name: str) -> None:
    _post(user, password, f"/api/v2/mounts/{mount_id}/files/folder", {"path": parent or "/"}, {"name": name})


def trash_page(user: str, password: str, cursor: str | None = None):
    """One page of the trash, newest first: (trash.Entry list, next cursor)."""
    from .trash import Entry
    params = {"pageSize": 1000, "sortField": "deleted", "sortDir": "desc"}
    if cursor:
        params["cursor"] = cursor
    data = _get(user, password, "/api/v2/trash", params)
    entries = []
    for f in data.get("files") or []:
        try:
            entries.append(Entry(str(f["mountId"]), str(f["path"]), int(f.get("size") or 0), int(f["deleted"])))
        except (KeyError, TypeError, ValueError):
            continue
    return entries, (data.get("pageInfo") or {}).get("cursor")


def trash_undelete(user: str, password: str, files: list[tuple[str, str]]) -> str | None:
    """Restore these (mount id, path): the id of the job doing it."""
    if not files:
        raise ValueError("an empty list would restore the whole trash")
    data = _post_json(user, password, "/api/v2/trash/undelete",
                      {"files": [{"mountId": m, "path": p} for m, p in files]})
    return data.get("id")


def job_done(user: str, password: str, job_id: str) -> bool | None:
    data = _get(user, password, f"/api/v2/jobs/{urllib.parse.quote(job_id)}")
    state = data.get("status") or data.get("state")
    if state in ("finished", "done"):
        return True
    if state in ("failed", "error"):
        return False
    return None


def primary_mount_id(user: str, password: str) -> str:
    mounts = _get(user, password, "/api/v2/mounts").get("mounts", [])
    for mount in mounts:
        if mount.get("isPrimary"):
            return mount["id"]
    raise ApiError("no primary mount")


def activity(user: str, password: str, mount_id: str, since_ms: int) -> list[Activity]:
    data = _get(user, password, "/api/v2.1/user/activity",
                {"limit": ACTIVITY_LIMIT, "sinceTime": since_ms, "mountId": mount_id})
    out = []
    for item in data.get("activity", []):
        try:
            out.append(Activity(str(item["id"]), int(item["timestamp"]), str(item.get("type", "")),
                                str(item.get("mountId", "")), str(item.get("path", "")),
                                str(item.get("newPath") or ""), str(item.get("user_agent") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return out


_LINK_CONTENT = re.compile(r"/content/links/([0-9A-Za-z-]+)/files/get\b")


def share_page(url: str) -> str:
    """The page of a share link, to give to someone: rclone link answers
    with the link's direct download (.../content/links/<id>/files/get),
    which Koofr refuses when opened from elsewhere ("Hotlinking not
    allowed"); the page is .../links/<id>. Any other URL as it is."""
    m = _LINK_CONTENT.search(url)
    return f"{KOOFR_WEB}/links/{m.group(1)}" if m else url


def web_url(mount_id: str | None, path: str) -> str:
    """The folder in Koofr's web app (the form Koofr Desktop uses)."""
    if not mount_id:
        return f"{KOOFR_WEB}/app/storage"
    return f"{KOOFR_WEB}/app/storage/{mount_id}?" + urllib.parse.urlencode({"path": path})


def folder_url(mount_id: str | None, remote_root: str, path: str) -> str:
    """The Koofr folder holding `path` (relative to a pair's root), in the
    web app: it cannot point at a single file."""
    return web_url(mount_id, posixpath.join(remote_root, posixpath.dirname(path)).rstrip("/") or "/")


def user_agent(instance_id: str) -> str:
    """What rclone sends when Spola runs it: the instance id lets Spola tell
    its own changes apart in the activity feed (Koofr Desktop does the same)."""
    return f"Spola/{__version__} ({instance_id})"


def touched_pairs(activities: list[Activity], pairs, mount_id: str, instance_id: str) -> tuple[set[str], bool]:
    """Which pairs a batch of activity concerns. Changes made by this Spola
    (its instance id in the user agent) are ignored. A full page may hide more: then every
    pair is reported, to be safe. Returns (pair ids, page_was_full)."""
    full = len(activities) >= ACTIVITY_LIMIT
    if full:
        return {p.id for p in pairs}, True
    touched = set()
    for act in activities:
        if (instance_id and instance_id in act.user_agent) or (act.mount_id and act.mount_id != mount_id):
            continue
        for pair in pairs:
            root = pair.remote.rstrip("/")
            for path in (act.path, act.new_path):
                if path and (root == "" or path == root or path.startswith(root + "/")
                             or root.startswith(path.rstrip("/") + "/")):
                    touched.add(pair.id)
    return touched, False
