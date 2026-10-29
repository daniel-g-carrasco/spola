"""Koofr sharing, independent of GTK. Reads never publish or invite.

The wire contracts follow Koofr's public OpenAPI 2.1 and java-koofr SDK.
A submount identifies a named share; an ordinary storage mount does not.
Link dates are Unix milliseconds. Writes are separate, never retried, and
may partially succeed: callers must reload after either success or failure.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import posixpath
import time
import urllib.error
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
import urllib.request

from . import KOOFR_WEB, __version__
from .i18n import _
from .koofr_api import _open

TIMEOUT = 20
UNCHANGED = object()
_ROLE_KEYS = ("READ", "WRITE", "COMMENT", "MOUNT", "CREATE_LINK", "CREATE_RECEIVER", "CREATE_ACTION")


class SharingError(Exception):
    """A localized message, safe to show without server data or secrets."""


@dataclass(frozen=True)
class Link:
    id: str = ""
    mount_id: str = ""
    path: str = "/"
    url: str = ""
    has_password: bool = False
    password: str = field(default="", repr=False)
    password_required: bool = False
    valid_from: int | None = None
    valid_to: int | None = None
    downloadable: bool = True
    counter: int = 0

    @property
    def active(self) -> bool:
        now = time.time() * 1000
        return (self.valid_from is None or self.valid_from <= now) and (self.valid_to is None or now < self.valid_to)


@dataclass(frozen=True)
class Member:
    id: str = ""
    name: str = ""
    email: str = ""
    permissions: Mapping[str, bool] = field(default_factory=dict)
    owner: bool = False
    is_group: bool = False

    def __post_init__(self):
        object.__setattr__(self, "permissions", MappingProxyType(dict(self.permissions)))


@dataclass(frozen=True)
class Access:
    mount_id: str = ""
    path: str = "/"
    is_dir: bool = True
    members: tuple[Member, ...] = ()
    can_manage: bool = False
    inherited: bool = False

    def __post_init__(self):
        object.__setattr__(self, "members", tuple(self.members))


@dataclass(frozen=True)
class Snapshot:
    path: str = "/"
    name: str = ""
    is_dir: bool = True
    link: Link | None = None
    access: Access | None = None
    inherited: tuple[Access, ...] = ()
    inherited_links: tuple[Link, ...] = ()
    can_create_link: bool = False
    can_invite: bool = False

    def __post_init__(self):
        object.__setattr__(self, "inherited", tuple(self.inherited))
        object.__setattr__(self, "inherited_links", tuple(self.inherited_links))


def permissions_for_role(role: str, existing: Mapping[str, bool] | None = None) -> dict[str, bool]:
    if role not in ("viewer", "editor", "custom"):
        raise SharingError(_("Choose a valid access role."))
    result = dict(existing or {})
    if role != "custom":
        result.update({key: key == "READ" or (role == "editor" and key in ("WRITE", "COMMENT"))
                       for key in _ROLE_KEYS})
    return result


def role_for_permissions(permissions: Mapping[str, bool]) -> str:
    if any(value for key, value in permissions.items() if key not in _ROLE_KEYS):
        return "custom"
    for role in ("viewer", "editor"):
        expected = permissions_for_role(role)
        if all(bool(permissions.get(key)) == value for key, value in expected.items()):
            return role
    return "custom"


def _path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or ".." in path.split("/"):
        raise SharingError(_("Choose a valid Koofr path."))
    return "/" + posixpath.normpath(path).lstrip("/")


def _mount_url(mount_id: str) -> str:
    return "/api/v2.1/mounts/" + urllib.parse.quote(mount_id, safe="")


def _bad_response() -> SharingError:
    return SharingError(_("Koofr returned incomplete sharing information. Please reload."))


def _link(data: dict, source: str | None = None) -> Link:
    try:
        if not all(isinstance(data[key], str) and data[key] for key in ("id", "mountId", "path", "url")):
            raise ValueError
        if any(type(data.get(key)) is not bool for key in ("hasPassword", "passwordRequired", "downloadable")):
            raise ValueError
        parts = urllib.parse.urlsplit(data["url"])
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise ValueError
        dates = [data.get("validFrom"), data.get("validTo")]
        if any(value is not None and (type(value) is not int or value < 0) for value in dates):
            raise ValueError
        for value in dates:
            if value is not None:
                # int64 milliseconds can exceed datetime's range. Validate
                # before a GTK callback tries to render the local date.
                dt.datetime.fromtimestamp(value / 1000)
        return Link(data["id"], data["mountId"], source or _path(data["path"]), data["url"],
                    bool(data.get("hasPassword")), str(data.get("password") or ""),
                    bool(data.get("passwordRequired")), *dates,
                    bool(data.get("downloadable", True)), int(data.get("counter") or 0))
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise _bad_response() from None


def _permission_data(data) -> dict[str, bool]:
    if not isinstance(data, dict) or any(not isinstance(k, str) or type(v) is not bool
                                        for k, v in data.items()):
        raise _bad_response()
    return data


def _members(mount: dict) -> tuple[Member, ...]:
    members = []
    owner = mount.get("owner")
    users, groups = mount.get("users"), mount.get("groups")
    if (not isinstance(owner, dict) or type(owner.get("isGroup")) is not bool or
            not isinstance(users, list) or not isinstance(groups, list)):
        raise _bad_response()
    for data, group, needs_email in [(owner, owner["isGroup"], False)] + [
            (u, False, True) for u in users] + [(g, True, False) for g in groups]:
        if (not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"] or
                not isinstance(data.get("name"), str) or
                not isinstance(data.get("email", None if needs_email else ""), str)):
            raise _bad_response()
        uid = data.get("id")
        permissions = _permission_data(data.get("permissions"))
        if any(member.id == uid for member in members):
            continue
        members.append(Member(uid, data.get("name") or "", data.get("email") or "", permissions,
                              uid == owner.get("id") or bool(permissions.get("OWNER")), group))
    return tuple(members)


def _validate_permissions(permissions: Mapping[str, bool], is_dir: bool) -> dict[str, bool]:
    if not isinstance(permissions, Mapping) or any(not isinstance(k, str) or type(v) is not bool
                                                  for k, v in permissions.items()):
        raise SharingError(_("Choose valid sharing permissions."))
    if permissions.get("OWNER"):
        raise SharingError(_("Owner access cannot be assigned or changed here."))
    if not is_dir and any(permissions.get(key) for key in _ROLE_KEYS if key != "READ"):
        raise SharingError(_("Files shared individually can only be viewed."))
    return dict(permissions)


class Client:
    def __init__(self, user: str, password: str, mount_id: str):
        self._user = user
        self._password = password
        self.mount_id = mount_id

    def _request(self, method: str, url: str, body: dict | None = None, params: dict | None = None) -> dict:
        token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        headers = {"Authorization": f"Basic {token}", "Accept": "application/json",
                   "User-Agent": f"Spola/{__version__}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(KOOFR_WEB + url + ("?" + urllib.parse.urlencode(params) if params else ""),
                                         data=json.dumps(body).encode() if body is not None else None,
                                         method=method, headers=headers)
        try:
            with _open(request, TIMEOUT) as response:
                raw = response.read()
                result = json.loads(raw) if raw else {}
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except urllib.error.HTTPError as error:
            # Server messages can echo submitted passwords or internal data.
            # Use only known codes to select our own localized messages.
            code = ""
            try:
                data = json.loads(error.read(16384))
                code = str((data.get("error") or {}).get("code", "")).lower()
            except (ValueError, AttributeError, TypeError, OSError):
                pass
            finally:
                error.close()
            if "password" in code:
                message = _("Koofr requires a password for this link or rejected the chosen password.")
            elif any(word in code for word in ("plan", "premium", "upgrade", "subscription")) or error.code == 402:
                message = _("Your Koofr plan does not allow this sharing option.")
            elif error.code == 401:
                message = _("Koofr could not authenticate this request. Check your connection settings.")
            elif error.code == 403:
                message = _("Koofr does not allow this sharing change. Check your permissions and plan.")
            elif error.code == 404:
                message = _("This item or share was not found on Koofr. It may not have synced yet.")
            elif error.code == 409:
                message = _("Koofr could not choose a unique recipient. Manage this invitation in Koofr.")
            elif error.code == 429:
                message = _("A Koofr sharing limit was reached. Check your plan or try again later.")
            elif error.code == 400:
                message = _("Koofr rejected these sharing settings. Check the recipient and link options.")
            else:
                message = _("Koofr could not complete the sharing request. Please reload and try again.")
            raise SharingError(message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SharingError(_("Could not reach Koofr. Check your connection and reload sharing.")) from None
        except (ValueError, TypeError):
            raise _bad_response() from None

    def _source(self, mount: dict, query_path: str, mount_path: str | None) -> str:
        if mount.get("id") == self.mount_id:
            return "/"
        root = mount.get("root") or {}
        if root.get("id") == self.mount_id and isinstance(root.get("path"), str):
            return _path(root["path"])
        if mount_path == "/":
            return query_path
        if mount_path and query_path.endswith(mount_path):
            return _path(query_path[:-len(mount_path)] or "/")
        raise _bad_response()

    def load(self, path: str) -> Snapshot:
        try:
            return self._load(path)
        except (KeyError, TypeError, AttributeError, ValueError):
            raise _bad_response() from None

    def _load(self, path: str) -> Snapshot:
        path = _path(path)
        base = self._request("GET", _mount_url(self.mount_id))
        if base.get("id") != self.mount_id or not isinstance(base.get("permissions"), dict):
            raise _bad_response()
        paths = [path]
        while paths[-1] != "/":
            paths.append(posixpath.dirname(paths[-1]))
        shares, links = {}, {}
        effective = base
        file = None
        for source_path in paths:
            data = self._request("GET", _mount_url(self.mount_id) + "/bundle", params={"path": source_path})
            current = data.get("file")
            if not isinstance(current, dict) or current.get("type") not in ("dir", "file"):
                raise _bad_response()
            current_mount = current.get("mount") or (base if source_path == "/" else None)
            if file is None:
                file = current
                if current_mount:
                    effective = current_mount
            if current_mount:
                source = self._source(current_mount, source_path, current.get("mountPath"))
                if source not in paths:
                    raise _bad_response()
                # Unshared mount metadata and its owner apply to all files;
                # their presence must never turn a child into a named share.
                if current_mount.get("isShared") or current_mount.get("id") != self.mount_id:
                    permissions = _permission_data(current_mount.get("permissions"))
                    inherited = source != path
                    shares[current_mount["id"]] = Access(
                        current_mount["id"], source, bool(current_mount.get("isDir", True)),
                        _members(current_mount), not inherited and bool(permissions.get("MOUNT")), inherited)
                # A bundle may omit a mount for the file; then the nearest
                # ancestor's authoritative permissions govern the child.
                if effective is base and source_path != path:
                    effective = current_mount
            if current.get("link"):
                raw_link = current["link"]
                if raw_link.get("mountId") == self.mount_id:
                    link_source = _path(raw_link.get("path", ""))
                elif current_mount and raw_link.get("mountId") == current_mount.get("id"):
                    link_source = _path(posixpath.join(source, raw_link.get("path", "").lstrip("/")))
                else:
                    raise _bad_response()
                if link_source not in paths:
                    raise _bad_response()
                links[raw_link["id"]] = _link(raw_link, link_source)
        permissions = _permission_data(effective.get("permissions"))
        return Snapshot(path, file.get("name") or posixpath.basename(path) or base.get("name") or "/",
                        file["type"] == "dir",
                        next((link for link in links.values() if link.path == path), None),
                        next((access for access in shares.values() if access.path == path), None),
                        tuple(access for access in shares.values() if access.path != path),
                        tuple(link for link in links.values() if link.path != path),
                        bool(permissions.get("CREATE_LINK")), bool(permissions.get("MOUNT")))

    def create_link(self, path: str) -> Link:
        snapshot = self.load(path)
        if not snapshot.can_create_link:
            raise SharingError(_("You do not have permission to create a link for this item."))
        if snapshot.link:
            return snapshot.link
        return _link(self._request("POST", _mount_url(self.mount_id) + "/links", {"path": snapshot.path}))

    def update_link(self, link: Link, *, password_mode: str = "keep", password: str = "",
                    valid_to=UNCHANGED, downloadable=UNCHANGED) -> Link:
        if password_mode not in ("keep", "set", "remove", "reset"):
            raise SharingError(_("Choose a valid password option."))
        if password_mode == "remove" and link.password_required:
            raise SharingError(_("Koofr requires a password for this link."))
        if password_mode == "set" and not password:
            raise SharingError(_("Enter a password for this link."))
        if valid_to is not UNCHANGED and valid_to is not None and (
                type(valid_to) is not int or valid_to < 0 or
                (link.valid_from is not None and valid_to <= link.valid_from)):
            raise SharingError(_("The expiry must be after the link's start time."))
        if downloadable is not UNCHANGED and type(downloadable) is not bool:
            raise SharingError(_("Choose a valid download setting."))
        url = _mount_url(link.mount_id) + "/links/" + urllib.parse.quote(link.id, safe="")
        changed = False
        if password_mode != "keep":
            suffix = "/password" if password_mode == "remove" else "/password/" + password_mode
            self._request("DELETE" if password_mode == "remove" else "PUT", url + suffix,
                          {"password": password} if password_mode == "set" else None)
            changed = True
        if valid_to is not UNCHANGED and valid_to != link.valid_to:
            body = {}
            if link.valid_from is not None:
                body["validFrom"] = link.valid_from
            if valid_to is not None:
                body["validTo"] = valid_to
            self._request("PUT", url + "/validity", body)
            changed = True
        if downloadable is not UNCHANGED and downloadable != link.downloadable:
            self._request("PUT", url + "/downloadable", {"downloadable": downloadable})
            changed = True
        return _link(self._request("GET", url), link.path) if changed else link

    def revoke_link(self, link: Link) -> None:
        self._request("DELETE", _mount_url(link.mount_id) + "/links/" + urllib.parse.quote(link.id, safe=""))

    def invite(self, path: str, email: str, permissions: Mapping[str, bool]) -> None:
        email = email.strip()
        if email.count("@") != 1 or any(char.isspace() for char in email) or not all(email.split("@")):
            raise SharingError(_("Enter a valid email address."))
        snapshot = self.load(path)
        permissions = _validate_permissions(permissions, snapshot.is_dir)
        if not snapshot.can_invite:
            raise SharingError(_("You do not have permission to invite people to this item."))
        if snapshot.access:
            if not snapshot.access.can_manage:
                raise SharingError(_("You do not have permission to change this shared access."))
            mount_id = snapshot.access.mount_id
        else:
            created = self._request("POST", _mount_url(self.mount_id) + "/submounts", {"path": snapshot.path})
            mount_id = created.get("id")
            if not isinstance(mount_id, str) or not mount_id:
                raise _bad_response()
        self._request("POST", _mount_url(mount_id) + "/users", {"email": email, "permissions": permissions})

    @staticmethod
    def _editable_member(access: Access, member_id: str) -> Member:
        if access.inherited:
            raise SharingError(_("This access is inherited. Open the parent folder to change it."))
        if not access.can_manage:
            raise SharingError(_("You do not have permission to change this shared access."))
        member = next((member for member in access.members if member.id == member_id), None)
        if member is None:
            raise SharingError(_("This member is no longer listed. Reload sharing."))
        if member.owner or member.permissions.get("OWNER"):
            raise SharingError(_("Owner access cannot be assigned or changed here."))
        if member.is_group:
            raise SharingError(_("Manage group access in Koofr."))
        return member

    def update_member(self, access: Access, member_id: str, permissions: Mapping[str, bool]) -> None:
        member = self._editable_member(access, member_id)
        merged = dict(member.permissions)
        merged.update(permissions)
        merged = _validate_permissions(merged, access.is_dir)
        self._request("PUT", _mount_url(access.mount_id) + "/users/" + urllib.parse.quote(member_id, safe=""),
                      {"permissions": merged})

    def remove_member(self, access: Access, member_id: str) -> None:
        self._editable_member(access, member_id)
        self._request("DELETE", _mount_url(access.mount_id) + "/users/" + urllib.parse.quote(member_id, safe=""))
