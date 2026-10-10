"""The few Koofr REST calls Spola makes itself, all read-only, with the
same app password as rclone (HTTP Basic). Everything that changes files
goes through rclone.

  GET /api/v2/mounts                  the primary mount id (links to the web app)
  GET /api/v2.1/user/activity         what changed on Koofr, and who changed it

The activity feed is not documented by Koofr (it is used by the web app and
by third-party tools): Spola treats it as a doorbell only. When it fails,
the periodic full run catches every change anyway.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import KOOFR_WEB, __version__

TIMEOUT = 30
ACTIVITY_LIMIT = 100


class ApiError(Exception):
    pass


@dataclass(frozen=True)
class Activity:
    id: str
    timestamp: int          # ms since the epoch
    type: str               # file_uploaded, file_deleted, file_moved, file_dir_created...
    mount_id: str
    path: str
    new_path: str
    user_agent: str


def _get(user: str, password: str, path: str, params: dict | None = None) -> dict:
    url = KOOFR_WEB + path + ("?" + urllib.parse.urlencode(params) if params else "")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, headers={
        "Authorization": f"Basic {token}", "Accept": "application/json",
        "User-Agent": f"Spola/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code} for {path}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise ApiError(f"{path}: {e}") from None


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


def web_url(mount_id: str | None, path: str) -> str:
    """The folder in Koofr's web app (the form Koofr Desktop uses)."""
    if not mount_id:
        return f"{KOOFR_WEB}/app/storage"
    return f"{KOOFR_WEB}/app/storage/{mount_id}?" + urllib.parse.urlencode({"path": path})


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
