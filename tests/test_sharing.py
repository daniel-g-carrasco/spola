"""Sharing contracts against schema-shaped responses; no Koofr writes."""
import base64
import io
import json
import time
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from unittest.mock import patch

import _setup  # noqa: F401
from spola import sharing


VIEWER = {"READ": True, "WRITE": False, "COMMENT": False, "MOUNT": False,
          "CREATE_LINK": False, "CREATE_RECEIVER": False, "CREATE_ACTION": False}
EDITOR = {**VIEWER, "WRITE": True, "COMMENT": True}


def mount(mid="root", path=None, *, shared=False, permissions=None, users=()):
    result = {"id": mid, "name": "Koofr" if mid == "root" else "Team", "type": "device",
              "origin": "device", "online": True, "isShared": shared,
              "permissions": permissions if permissions is not None else {"OWNER": True, "READ": True,
                                                                          "MOUNT": True, "CREATE_LINK": True},
              "isPrimary": mid == "root", "isDir": True, "canWrite": True, "canUpload": True,
              "owner": {"id": "owner", "name": "Owner", "email": "owner@example.test",
                        "permissions": {"OWNER": True}, "isGroup": False, "added": 1},
              "users": list(users), "groups": [], "version": 1, "overQuota": False,
              "almostOverQuota": False, "userAdded": 1, "capabilities": {"externalLinks": True}}
    if path is not None:
        result["root"] = {"id": "root", "name": "Koofr", "path": path}
    return result


def bundle(path, *, directory=True, share=None, link=None, mount_path=None):
    file = {"name": path.rsplit("/", 1)[-1], "type": "dir" if directory else "file",
            "modified": 1, "size": 0, "contentType": "", "tags": {}}
    if share is not None:
        file["mount"] = share
    if link is not None:
        file["link"] = link
    if mount_path is not None:
        file["mountPath"] = mount_path
    return {"file": file, "files": []}


def link_data(**changes):
    result = {"id": "link-1", "mountId": "root", "path": "/Team/report.txt", "name": "report.txt",
              "counter": 3, "url": "https://app.koofr.net/links/abc", "shortUrl": "",
              "hash": "abc", "host": "app.koofr.net", "hasPassword": False,
              "passwordRequired": False, "downloadable": True, "created": 100}
    result.update(changes)
    return result


class HTTP:
    """Transport fake records real request objects and returns API JSON."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("Unexpected request: " + request.get_method() + " " + request.full_url)
        data = self.responses.pop(0)
        if isinstance(data, Exception):
            raise data
        return io.BytesIO(json.dumps(data).encode() if data is not None else b"")

    def methods(self):
        return [request.get_method() for request, _ in self.calls]

    def writes(self):
        return [(request.get_method(), urllib.parse.urlsplit(request.full_url).path,
                 json.loads(request.data) if request.data else None)
                for request, _ in self.calls if request.get_method() != "GET"]


def http_error(code, message="server-secret", error_code="Other"):
    return urllib.error.HTTPError("https://app.koofr.net/secret", code, message, {},
                                  io.BytesIO(json.dumps({"error": {"code": error_code,
                                                                  "message": message}}).encode()))


class RolesTest(unittest.TestCase):
    def test_roles_reset_known_grants_but_preserve_unknown_and_owner(self):
        old = {**EDITOR, "MOUNT": True, "OWNER": False, "FUTURE_FLAG": True}
        self.assertEqual(sharing.permissions_for_role("viewer", old),
                         {**VIEWER, "OWNER": False, "FUTURE_FLAG": True})
        self.assertEqual(sharing.permissions_for_role("editor"), EDITOR)
        self.assertTrue(old["MOUNT"])
        self.assertEqual(sharing.permissions_for_role("custom", old), old)

    def test_custom_grants_are_not_mislabelled_as_simple_roles(self):
        for value, expected in ((VIEWER, "viewer"), (EDITOR, "editor"),
                                ({**VIEWER, "FUTURE_FLAG": True}, "custom"),
                                ({**EDITOR, "MOUNT": True}, "custom"), ({"READ": False}, "custom")):
            with self.subTest(value=value):
                self.assertEqual(sharing.role_for_permissions(value), expected)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.client = sharing.Client("me@example.test", "app-password", "root")

    def http(self, *responses):
        fake = HTTP(*responses)
        self.addCleanup(patch.stopall)
        patch.object(sharing, "_open", fake).start()
        return fake

    def plain(self, *, directory=False):
        return [mount(), bundle("/Team/report.txt", directory=directory), bundle("/Team"),
                bundle("/", share=mount(), mount_path="/")]

    def test_load_is_read_only_and_root_ownership_is_not_explicit_sharing(self):
        fake = self.http(*self.plain())
        snapshot = self.client.load("/Team/report.txt")
        self.assertEqual(snapshot.name, "report.txt")
        self.assertFalse(snapshot.is_dir)
        self.assertIsNone(snapshot.link)
        self.assertIsNone(snapshot.access)
        self.assertEqual(snapshot.inherited, ())
        self.assertTrue(snapshot.can_create_link)
        self.assertTrue(snapshot.can_invite)
        self.assertTrue(all(method == "GET" for method in fake.methods()))
        for request, timeout in fake.calls:
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 30)
            self.assertEqual(request.get_header("Authorization"),
                             "Basic " + base64.b64encode(b"me@example.test:app-password").decode())
        paths = [urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get("path")
                 for req, _ in fake.calls]
        self.assertIn(["/Team/report.txt"], paths)
        self.assertIn(["/Team"], paths)
        self.assertIn(["/"], paths)

    def test_inherited_access_and_links_have_parent_sources_and_are_not_editable(self):
        shared = mount("shared", "/Team", shared=True,
                       users=[{"id": "guest", "name": "Guest", "email": "guest@example.test",
                               "permissions": VIEWER, "added": 1}])
        parent_link = link_data(path="/Team")
        self.http(mount(), bundle("/Team/report.txt", directory=False, share=shared,
                                  mount_path="/report.txt", link=parent_link),
                  bundle("/Team", share=shared, mount_path="/", link=parent_link),
                  bundle("/", share=mount(), mount_path="/"))
        snapshot = self.client.load("/Team/report.txt")
        self.assertIsNone(snapshot.access)
        self.assertIsNone(snapshot.link)
        self.assertEqual(len(snapshot.inherited), 1)
        self.assertEqual(snapshot.inherited[0].path, "/Team")
        self.assertFalse(snapshot.inherited[0].can_manage)
        self.assertTrue(snapshot.inherited[0].inherited)
        self.assertEqual(snapshot.inherited[0].members[0].id, "owner")
        self.assertTrue(snapshot.inherited[0].members[0].owner)
        self.assertEqual([link.path for link in snapshot.inherited_links], ["/Team"])

    def test_exact_share_is_distinct_from_an_ancestor_share(self):
        exact = mount("file-share", "/Team/report.txt", shared=True)
        exact["isDir"] = False
        parent = mount("parent-share", "/Team", shared=True)
        self.http(mount(), bundle("/Team/report.txt", directory=False, share=exact,
                                  link=link_data(), mount_path="/"),
                  bundle("/Team", share=parent, mount_path="/"), bundle("/", share=mount()))
        snapshot = self.client.load("/Team/report.txt")
        self.assertEqual(snapshot.access.mount_id, "file-share")
        self.assertFalse(snapshot.access.is_dir)
        self.assertTrue(snapshot.access.can_manage)
        self.assertEqual(snapshot.link.path, snapshot.path)
        self.assertEqual([a.mount_id for a in snapshot.inherited], ["parent-share"])

    def test_authoritative_permissions_do_not_infer_sharing_from_write_access(self):
        self.http(mount(permissions={"READ": True, "WRITE": True}),
                  bundle("/Team/report.txt", directory=False), bundle("/Team"), bundle("/"))
        snapshot = self.client.load("/Team/report.txt")
        self.assertFalse(snapshot.can_create_link)
        self.assertFalse(snapshot.can_invite)

    def test_explicit_permission_denials_are_not_overridden_by_owner_metadata(self):
        own = mount(permissions={"OWNER": True, "CREATE_LINK": False, "MOUNT": False})
        self.http(own, bundle("/Team/report.txt", directory=False, share=own, mount_path="/Team/report.txt"),
                  bundle("/Team", share=own, mount_path="/Team"), bundle("/", share=own, mount_path="/"))
        snapshot = self.client.load("/Team/report.txt")
        self.assertFalse(snapshot.can_create_link)
        self.assertFalse(snapshot.can_invite)
        self.assertIsNone(snapshot.access)
        self.assertEqual(snapshot.inherited, ())

    def test_missing_password_metadata_never_looks_like_an_unprotected_link(self):
        data = link_data()
        del data["hasPassword"]
        fake = self.http(mount(), bundle("/Team/report.txt", directory=False, link=data),
                         bundle("/Team"), bundle("/", share=mount()))
        with self.assertRaises(sharing.SharingError):
            self.client.load("/Team/report.txt")
        self.assertEqual(fake.writes(), [])

    def test_incomplete_shared_membership_fails_without_hiding_people(self):
        guest = {"id": "guest", "name": "Guest", "email": "guest@example.test",
                 "permissions": VIEWER, "added": 1}
        for field in ("owner", "users", "groups"):
            with self.subTest(missing=field):
                shared = mount("shared", "/Team", shared=True, users=[guest])
                del shared[field]
                fake = self.http(mount(), bundle("/Team", share=shared), bundle("/", share=mount()))
                with self.assertRaises(sharing.SharingError):
                    self.client.load("/Team")
                self.assertEqual(fake.writes(), [])
        for field, value in (("id", ""), ("id", None), ("permissions", None),
                             ("permissions", {"READ": "false"}), ("name", None)):
            with self.subTest(field=field, value=value):
                shared = mount("shared", "/Team", shared=True, users=[{**guest, field: value}])
                self.http(mount(), bundle("/Team", share=shared), bundle("/", share=mount()))
                with self.assertRaises(sharing.SharingError):
                    self.client.load("/Team")

    def test_unrepresentable_link_dates_fail_before_rendering(self):
        for field in ("validFrom", "validTo"):
            for value in (253402300800000, 2**63 - 1):
                with self.subTest(field=field, value=value):
                    fake = self.http(mount(), bundle("/Team/report.txt", link=link_data(**{field: value})),
                                     bundle("/Team"), bundle("/", share=mount()))
                    with self.assertRaises(sharing.SharingError):
                        self.client.load("/Team/report.txt")
                    self.assertEqual(fake.writes(), [])

    def test_existing_share_respects_explicit_manage_permission_denial(self):
        shared = mount("shared", "/Team", shared=True, permissions={"OWNER": True, "MOUNT": False})
        self.http(mount(), bundle("/Team", share=shared), bundle("/", share=mount()))
        snapshot = self.client.load("/Team")
        self.assertFalse(snapshot.can_invite)
        self.assertFalse(snapshot.access.can_manage)

    def test_not_uploaded_file_fails_without_writing(self):
        fake = self.http(mount(), http_error(404))
        with self.assertRaises(sharing.SharingError) as caught:
            self.client.create_link("/Team/report.txt")
        self.assertIn("Koofr", str(caught.exception))
        self.assertEqual(fake.writes(), [])

    def test_create_link_uses_defaults_and_returns_generated_password(self):
        fake = self.http(*self.plain(), link_data(hasPassword=True, password="generated-secret",
                                                 passwordRequired=True, validTo=1800000000000))
        link = self.client.create_link("/Team/report.txt")
        self.assertEqual(fake.writes(), [("POST", "/api/v2.1/mounts/root/links",
                                          {"path": "/Team/report.txt"})])
        self.assertTrue(link.password_required)
        self.assertEqual(link.password, "generated-secret")
        self.assertNotIn("generated-secret", repr(link))
        self.assertEqual(link.valid_to, 1800000000000)

    def test_update_expiry_preserves_start_and_password(self):
        old = sharing.Link("link-1", "root", "/Team/report.txt", "https://example.test/link",
                           has_password=True, password="keep-secret", valid_from=1000, valid_to=2000)
        fake = self.http(None, link_data(hasPassword=True, password="keep-secret", validFrom=1000,
                                         validTo=3000))
        updated = self.client.update_link(old, valid_to=3000)
        self.assertEqual(fake.writes(), [("PUT", "/api/v2.1/mounts/root/links/link-1/validity",
                                          {"validFrom": 1000, "validTo": 3000})])
        self.assertEqual(updated.password, "keep-secret")
        self.assertEqual(updated.valid_from, 1000)
        self.assertEqual(updated.valid_to, 3000)

    def test_unchanged_settings_do_not_write(self):
        old = sharing.Link("link-1", "root", "/a", "https://example.test/link")
        fake = self.http()
        self.assertEqual(self.client.update_link(old), old)
        self.assertEqual(fake.calls, [])

    def test_password_modes_and_downloadable_have_separate_schema_bodies(self):
        old = sharing.Link("link-1", "root", "/a", "https://example.test/link")
        for mode, method, suffix, payload in (("set", "PUT", "/password/set", {"password": "new-secret"}),
                                               ("reset", "PUT", "/password/reset", None),
                                               ("remove", "DELETE", "/password", None)):
            with self.subTest(mode=mode):
                fake = self.http(None, None, link_data())
                self.client.update_link(old, password_mode=mode, password="new-secret", downloadable=False)
                self.assertEqual(fake.writes(), [
                    (method, "/api/v2.1/mounts/root/links/link-1" + suffix, payload),
                    ("PUT", "/api/v2.1/mounts/root/links/link-1/downloadable", {"downloadable": False})])

    def test_clear_expiry_does_not_clear_valid_from(self):
        old = sharing.Link("link-1", "root", "/a", "https://example.test/link", valid_from=1000, valid_to=3000)
        fake = self.http(None, link_data(validFrom=1000))
        self.client.update_link(old, valid_to=None)
        self.assertEqual(fake.writes()[0][2], {"validFrom": 1000})

    def test_mandatory_password_and_invalid_expiry_rejected_before_any_write(self):
        old = sharing.Link("link-1", "root", "/a", "https://example.test/link", password_required=True,
                           valid_from=1000)
        fake = self.http()
        for kwargs in ({"password_mode": "remove"}, {"valid_to": 500},
                       {"password_mode": "set", "password": ""}, {"password_mode": "oops"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(sharing.SharingError):
                self.client.update_link(old, **kwargs)
        self.assertEqual(fake.calls, [])

    def test_partial_failure_stops_writes_and_never_claims_success_or_retries(self):
        old = sharing.Link("link-1", "root", "/a", "https://example.test/link")
        fake = self.http(None, http_error(403, "new-secret app-password", "PlanRequired"))
        with self.assertRaises(sharing.SharingError) as caught:
            self.client.update_link(old, password_mode="set", password="new-secret", valid_to=3000,
                                    downloadable=False)
        self.assertEqual(fake.methods(), ["PUT", "PUT"])
        self.assertNotIn("new-secret", str(caught.exception))
        self.assertNotIn("app-password", str(caught.exception))
        self.assertIn("plan", str(caught.exception).lower())

    def test_revoke_deletes_only_the_selected_link(self):
        fake = self.http(None)
        self.client.revoke_link(sharing.Link("l/1", "m/1", "/a", "https://example.test/link"))
        self.assertEqual(fake.writes(), [("DELETE", "/api/v2.1/mounts/m%2F1/links/l%2F1", None)])

    def test_invite_creates_only_an_exact_submount_and_then_adds_member(self):
        fake = self.http(*self.plain(), mount("new-share", "/Team/report.txt"),
                         {"id": "guest", "name": "Guest", "email": "guest@example.test",
                          "permissions": VIEWER, "added": 1})
        self.client.invite("/Team/report.txt", " guest@example.test ", VIEWER)
        self.assertEqual(fake.writes(), [
            ("POST", "/api/v2.1/mounts/root/submounts", {"path": "/Team/report.txt"}),
            ("POST", "/api/v2.1/mounts/new-share/users", {"email": "guest@example.test", "permissions": VIEWER})])

    def test_inviting_to_child_does_not_add_user_to_parent_share(self):
        parent = mount("parent-share", "/Team", shared=True)
        fake = self.http(mount(), bundle("/Team/report.txt", directory=False, share=parent,
                                        mount_path="/report.txt"),
                         bundle("/Team", share=parent, mount_path="/"), bundle("/", share=mount()),
                         mount("child-share", "/Team/report.txt"), {})
        self.client.invite("/Team/report.txt", "guest@example.test", VIEWER)
        self.assertEqual(fake.writes(), [
            ("POST", "/api/v2.1/mounts/root/submounts", {"path": "/Team/report.txt"}),
            ("POST", "/api/v2.1/mounts/child-share/users", {"email": "guest@example.test", "permissions": VIEWER})])

    def test_shared_mount_relative_link_has_original_mutation_mount_and_display_source(self):
        parent = mount("parent-share", "/Team", shared=True)
        self.http(mount(), bundle("/Team/report.txt", directory=False, share=parent, mount_path="/report.txt",
                                  link=link_data(mountId="parent-share", path="/report.txt")),
                  bundle("/Team", share=parent, mount_path="/"), bundle("/", share=mount()))
        snapshot = self.client.load("/Team/report.txt")
        self.assertEqual(snapshot.link.path, "/Team/report.txt")
        self.assertEqual(snapshot.link.mount_id, "parent-share")

    def test_invite_reuses_exact_share_without_creating_another(self):
        exact = mount("exact", "/Team/report.txt", shared=True)
        self.http(mount(), bundle("/Team/report.txt", share=exact), bundle("/Team"), bundle("/"), {})
        self.client.invite("/Team/report.txt", "guest@example.test", EDITOR)
        self.assertEqual(sharing._open.writes(), [("POST", "/api/v2.1/mounts/exact/users",
                                                   {"email": "guest@example.test", "permissions": EDITOR})])

    def test_individual_files_reject_write_permissions_before_creating_share(self):
        fake = self.http(*self.plain())
        with self.assertRaises(sharing.SharingError):
            self.client.invite("/Team/report.txt", "guest@example.test", EDITOR)
        self.assertEqual(fake.writes(), [])

    def test_members_update_preserves_unknown_flags_and_remove_targets_one_user(self):
        access = sharing.Access("shared", "/Team", True,
                                (sharing.Member("guest", "Guest", "g@example.test",
                                                {**VIEWER, "FUTURE_FLAG": True}),), True)
        fake = self.http(None, None)
        self.client.update_member(access, "guest", {"WRITE": True})
        self.client.remove_member(access, "guest")
        self.assertEqual(fake.writes(), [
            ("PUT", "/api/v2.1/mounts/shared/users/guest",
             {"permissions": {**VIEWER, "FUTURE_FLAG": True, "WRITE": True}}),
            ("DELETE", "/api/v2.1/mounts/shared/users/guest", None)])

    def test_owner_inherited_groups_and_unmanageable_access_are_protected(self):
        owner = sharing.Member("owner", "Owner", permissions={"OWNER": True}, owner=True)
        guest = sharing.Member("guest", "Guest", permissions=VIEWER)
        group = sharing.Member("group", "Group", permissions=VIEWER, is_group=True)
        access = sharing.Access("shared", "/Team", True, (owner, guest, group), True)
        fake = self.http()
        for source, uid in ((access, "owner"), (access, "group"),
                            (replace(access, inherited=True), "guest"),
                            (replace(access, can_manage=False), "guest"), (access, "missing")):
            for operation in (lambda source=source, uid=uid: self.client.remove_member(source, uid),
                              lambda source=source, uid=uid: self.client.update_member(source, uid, VIEWER)):
                with self.subTest(uid=uid, source=source), self.assertRaises(sharing.SharingError):
                    operation()
        self.assertEqual(fake.calls, [])

    def test_transport_errors_do_not_leak_server_or_connection_details(self):
        for error in (http_error(401), http_error(403), http_error(429),
                      urllib.error.URLError("app-password"), TimeoutError("app-password"),
                      ValueError("app-password")):
            fake = self.http(error)
            with self.subTest(error=type(error).__name__), self.assertRaises(sharing.SharingError) as caught:
                self.client.load("/a")
            self.assertNotIn("server-secret", str(caught.exception))
            self.assertNotIn("app-password", str(caught.exception))
            self.assertEqual(len(fake.calls), 1)

    def test_invalid_paths_and_email_cannot_make_network_requests(self):
        fake = self.http()
        for path in ("../a", "/a/../b", "/a\x00b"):
            with self.subTest(path=path), self.assertRaises(sharing.SharingError):
                self.client.load(path)
        for email in ("", "@example.test", "a@", "a@b@c", "a\nb@example.test"):
            with self.subTest(email=email), self.assertRaises(sharing.SharingError):
                self.client.invite("/a", email, VIEWER)
        self.assertEqual(fake.calls, [])

    def test_member_permissions_snapshot_cannot_be_changed_by_other_threads(self):
        permissions = dict(VIEWER)
        member = sharing.Member("guest", permissions=permissions)
        permissions["WRITE"] = True
        self.assertFalse(member.permissions["WRITE"])
        with self.assertRaises(TypeError):
            member.permissions["WRITE"] = True

    def test_malformed_mount_metadata_fails_with_a_safe_message(self):
        fake = self.http(mount(), bundle("/Team/report.txt", directory=False, share="server-secret"))
        with self.assertRaises(sharing.SharingError) as caught:
            self.client.load("/Team/report.txt")
        self.assertNotIn("server-secret", str(caught.exception))
        self.assertEqual(fake.writes(), [])

    def test_snapshot_copies_inherited_sequences(self):
        inherited = [sharing.Access("shared", "/Team")]
        snapshot = sharing.Snapshot(inherited=inherited)
        inherited.clear()
        self.assertEqual(len(snapshot.inherited), 1)
        self.assertIsInstance(snapshot.inherited, tuple)

    def test_active_links_use_milliseconds_and_exclusive_expiry(self):
        link = sharing.Link("l", "root", "/a", "https://example.test/link")
        with patch.object(time, "time", return_value=2):
            self.assertTrue(link.active)
            self.assertFalse(replace(link, valid_from=3000).active)
            self.assertFalse(replace(link, valid_to=2000).active)
            self.assertTrue(replace(link, valid_from=1000, valid_to=2001).active)


if __name__ == "__main__":
    unittest.main()
