"""Sharing is explicit, and the screen follows the state Koofr confirms."""

import importlib.util
import locale
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import _setup  # noqa: F401

_before = locale.setlocale(locale.LC_ALL)
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk
except (ImportError, ValueError):
    raise unittest.SkipTest("GTK 4 and libadwaita") from None
Adw.init()
locale.setlocale(locale.LC_ALL, _before)
if Gdk.Display.get_default() is None:
    raise unittest.SkipTest("no display")


def link(**changes):
    values = dict(id="link", mount_id="primary", path="/work", url="https://app.koofr.net/links/example",
                  has_password=False, password="", password_required=False, valid_from=None, valid_to=None,
                  downloadable=True, counter=0, active=True)
    return NS(**(values | changes))


def snapshot(**changes):
    values = dict(path="/work", name="work", is_dir=True, link=None, access=None, inherited=(),
                  inherited_links=(), can_create_link=True, can_invite=True)
    return NS(**(values | changes))


class SharingDialogTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("spola.sharing_dialog"), "sharing dialog is implemented")
        from spola import sharing_dialog
        self.ui = sharing_dialog
        self.jobs = []
        self.client = mock.Mock()
        self.client.load.return_value = snapshot()
        self.patch_client = mock.patch.object(sharing_dialog.sharing, "Client", return_value=self.client)
        self.patch_thread = mock.patch.object(sharing_dialog, "run_in_thread", side_effect=lambda w, d: self.jobs.append((w, d)))
        self.patch_client.start()
        self.patch_thread.start()
        self.addCleanup(self.patch_client.stop)
        self.addCleanup(self.patch_thread.stop)

    def dialog(self, windows=False):
        parent = (Gtk.Window if windows else Adw.Window)(default_width=520, default_height=680)
        parent.engine = NS(creds=NS(user="owner@example.test", password="secret"), cfg=NS(mount_id="primary"))
        self.addCleanup(parent.destroy)
        with mock.patch.object(self.ui, "WINDOWS", windows):
            dialog = self.ui.SharingDialog(parent, "/work")
            dialog.present(parent)
        self.addCleanup(dialog.force_close)
        return dialog

    def finish(self):
        work, done = self.jobs.pop(0)
        try:
            result, error = work(), None
        except Exception as exc:
            result, error = None, exc
        done(result, error)

    def test_opening_only_reads_and_copy_stays_unavailable_until_loaded(self):
        dialog = self.dialog()
        self.assertFalse(dialog.copy_button.get_sensitive())
        self.finish()
        self.assertEqual(self.client.method_calls, [mock.call.load("/work")])
        self.assertTrue(dialog.create_button.get_visible())
        self.assertFalse(dialog.copy_button.get_visible())

    def test_copy_existing_link_never_mutates_permissions_or_expiry(self):
        self.client.load.return_value = snapshot(link=link(valid_to=1900000000000))
        dialog = self.dialog()
        self.finish()
        with mock.patch.object(dialog, "_copy_text") as clipboard:
            dialog.copy_button.emit("clicked")
        clipboard.assert_called_once_with("https://app.koofr.net/links/example")
        self.assertEqual(self.client.method_calls, [mock.call.load("/work")])

    def test_existing_link_without_management_permission_can_only_be_copied(self):
        self.client.load.return_value = snapshot(link=link(), can_create_link=False)
        dialog = self.dialog()
        self.finish()
        self.assertTrue(dialog.copy_button.get_sensitive())
        self.assertFalse(dialog.link_actions.get_visible())
        with mock.patch.object(self.ui, "LinkSettingsDialog") as settings, \
                mock.patch.object(self.ui.Adw, "AlertDialog") as revoke:
            dialog._settings()
            dialog._revoke()
        settings.assert_not_called()
        revoke.assert_not_called()

    def test_protected_link_displays_password_and_correct_access_summary(self):
        self.client.load.return_value = snapshot(link=link(has_password=True, password="abc123", password_required=True))
        dialog = self.dialog()
        self.finish()
        self.assertIn("password", dialog.link_summary.get_subtitle().lower())
        self.assertEqual(dialog.password_entry.get_text(), "abc123")
        self.assertTrue(dialog.password_row.get_visible())

    def test_expired_link_cannot_be_copied(self):
        self.client.load.return_value = snapshot(link=link(valid_to=1000, active=False))
        dialog = self.dialog()
        self.finish()
        self.assertFalse(dialog.copy_button.get_sensitive())
        self.assertIn("expired", dialog.link_summary.get_subtitle().lower())

    def test_create_is_explicit_and_reloads_server_settings(self):
        dialog = self.dialog()
        self.finish()
        self.client.load.return_value = snapshot(link=link(has_password=True, password="abcdef"))
        dialog.create_button.emit("clicked")
        self.assertFalse(dialog.create_button.get_sensitive())
        self.finish()
        self.assertEqual(self.client.create_link.call_args, mock.call("/work"))
        self.assertEqual(dialog.password_entry.get_text(), "abcdef")
        self.assertTrue(dialog.copy_button.get_sensitive())

    def test_partial_write_failure_reloads_and_shows_actual_state(self):
        self.client.load.return_value = snapshot(link=link())
        dialog = self.dialog()
        self.finish()
        self.client.update_link.side_effect = RuntimeError("Koofr refused the expiry")
        self.client.load.return_value = snapshot(link=link(has_password=True, password="abcdef"))
        dialog._change(lambda: self.client.update_link(dialog.snapshot.link, password_mode="reset", valid_to=1000))
        self.assertFalse(dialog.copy_button.get_sensitive())
        self.finish()
        self.assertIn("refused", dialog.error_label.get_label())
        self.assertTrue(dialog.snapshot.link.has_password)

    def test_failed_reload_leaves_copy_disabled_and_offers_retry(self):
        dialog = self.dialog()
        self.client.load.side_effect = RuntimeError("Connection timed out")
        self.finish()
        self.assertFalse(dialog.copy_button.get_sensitive())
        self.assertTrue(dialog.retry_button.get_visible())
        self.client.load.side_effect = None
        dialog.retry_button.emit("clicked")
        self.finish()
        self.assertIsNotNone(dialog.snapshot)

    def test_late_reply_after_close_is_ignored(self):
        dialog = self.dialog()
        dialog.force_close()
        self.finish()
        self.assertIsNone(dialog.snapshot)

    def test_account_change_invalidates_the_dialog(self):
        dialog = self.dialog()
        dialog.engine.creds = None
        self.finish()
        self.assertIsNone(dialog.snapshot)
        self.assertFalse(dialog.create_button.get_sensitive())

    def test_inherited_access_is_labelled_and_has_no_member_edit_actions(self):
        member = NS(id="guest", name="Guest", email="guest@example.test", permissions={"READ": True}, owner=False, is_group=False)
        inherited = NS(mount_id="parent-share", path="/parent", is_dir=True, members=(member,), can_manage=False, inherited=True)
        self.client.load.return_value = snapshot(inherited=(inherited,))
        dialog = self.dialog()
        self.finish()
        self.assertEqual(len(dialog.member_rows), 1)
        self.assertIn("/parent", dialog.member_rows[0].get_subtitle())
        self.assertFalse(dialog.member_rows[0].get_activatable())

    def test_windows_dialog_is_bounded_resizable_and_scrollable(self):
        dialog = self.dialog(windows=True)
        root = dialog.get_root()
        self.assertIsInstance(root, Gtk.Window)
        self.assertTrue(root.get_resizable())
        width, height = root.get_default_size()
        self.assertLessEqual(height, 700)
        self.assertLess(root.measure(Gtk.Orientation.VERTICAL, width)[0], height)

    def test_single_file_permission_editor_cannot_grant_edit_access(self):
        editor = self.ui.PermissionEditor(False)
        self.assertFalse(editor.role.get_sensitive())
        self.assertFalse(editor.permissions().get("WRITE", False))

    def test_custom_permission_changes_preserve_unknown_grants(self):
        editor = self.ui.PermissionEditor(True, {"READ": True, "WRITE": False, "FUTURE_PERMISSION": True})
        editor.switches["COMMENT"].set_active(True)
        result = editor.permissions()
        self.assertTrue(result["FUTURE_PERMISSION"])
        self.assertTrue(result["COMMENT"])
        self.assertFalse(result["WRITE"])

    def test_link_settings_keep_existing_expiry_until_explicitly_changed(self):
        dialog = self.dialog()
        self.finish()
        settings = self.ui.LinkSettingsDialog(dialog, link(valid_to=int(time.time() * 1000) + 60000))
        self.assertNotIn("valid_to", settings.changes())
        settings.expiry.set_selected(2)  # one day
        with mock.patch.object(self.ui.time, "time", return_value=1000):
            self.assertEqual(settings.changes()["valid_to"], 87400000)

    def test_mandatory_password_cannot_be_removed(self):
        dialog = self.dialog()
        self.finish()
        settings = self.ui.LinkSettingsDialog(dialog, link(has_password=True, password_required=True))
        self.assertEqual(settings.password_mode.get_model().get_n_items(), 3)

    def test_custom_permissions_start_from_the_selected_preset(self):
        editor = self.ui.PermissionEditor(True)
        editor.role.set_selected(1)
        editor.role.set_selected(2)
        self.assertTrue(editor.permissions()["WRITE"])
        self.assertTrue(editor.permissions()["COMMENT"])

    def test_sending_an_invitation_is_explicit_and_uses_the_selected_role(self):
        dialog = self.dialog()
        self.finish()
        editor = self.ui.MemberDialog(dialog)
        self.assertEqual(self.client.method_calls, [mock.call.load("/work")])
        editor.present(dialog)
        editor.email.set_text(" guest@example.test ")
        editor.editor.role.set_selected(1)
        editor._save()
        self.finish()
        args = self.client.invite.call_args.args
        self.assertEqual(args[:2], ("/work", "guest@example.test"))
        self.assertTrue(args[2]["WRITE"])
        self.assertFalse(args[2]["MOUNT"])

    def test_invalid_invitation_and_password_do_not_start_requests(self):
        dialog = self.dialog()
        self.finish()
        editor = self.ui.MemberDialog(dialog)
        editor.email.set_text("no address")
        editor._save()
        self.assertTrue(editor.error.get_visible())
        settings = self.ui.LinkSettingsDialog(dialog, link())
        settings.password_mode.set_selected(2)
        settings.password.set_text("short")
        settings._save()
        self.assertTrue(settings.error.get_visible())
        self.assertEqual(self.jobs, [])

    def test_failed_verification_after_a_write_never_leaves_a_stale_copy_button(self):
        self.client.load.return_value = snapshot(link=link())
        dialog = self.dialog()
        self.finish()
        self.client.load.side_effect = RuntimeError("Connection timed out")
        dialog._change(lambda: self.client.revoke_link(dialog.snapshot.link))
        self.finish()
        self.assertIsNone(dialog.snapshot)
        self.assertFalse(dialog.copy_button.get_sensitive())
        self.assertTrue(dialog.retry_button.get_visible())

    def test_owner_and_group_access_cannot_be_edited_as_an_individual(self):
        owner = NS(id="owner", name="Owner", email="owner@example.test", permissions={"OWNER": True}, owner=True, is_group=False)
        group = NS(id="group", name="Team", email="", permissions={"READ": True}, owner=False, is_group=True)
        access = NS(mount_id="share", path="/work", is_dir=True, members=(owner, group), can_manage=True, inherited=False)
        self.client.load.return_value = snapshot(access=access)
        dialog = self.dialog()
        self.finish()
        self.assertTrue(all(not row.get_activatable() for row in dialog.member_rows))

    def test_expired_parent_link_is_not_described_as_providing_access(self):
        self.client.load.return_value = snapshot(inherited_links=(link(path="/", active=False, valid_to=1000),))
        dialog = self.dialog()
        self.finish()
        self.assertIn("inactive", dialog.inherited_link_rows[0].get_subtitle().lower())

    def test_custom_changes_do_not_grant_read_access_implicitly(self):
        for permissions in ({"READ": False, "WRITE": True}, {}):
            with self.subTest(permissions=permissions):
                editor = self.ui.PermissionEditor(True, permissions)
                editor.switches["COMMENT"].set_active(True)
                self.assertFalse(editor.permissions().get("READ", False))
        editor = self.ui.PermissionEditor(False, {"READ": False})
        self.assertFalse(editor.permissions()["READ"])

    def test_custom_expiry_uses_calendar_and_local_time(self):
        import datetime
        from gi.repository import GLib
        dialog = self.dialog()
        self.finish()
        settings = self.ui.LinkSettingsDialog(dialog, link())
        self.assertTrue(hasattr(settings, "calendar"), "custom expiry has a calendar picker")
        settings.expiry.set_selected(4)
        settings.calendar.select_day(GLib.DateTime.new_local(2030, 6, 15, 0, 0, 0))
        settings.hour.set_value(18)
        settings.minute.set_value(30)
        self.assertEqual(settings.changes()["valid_to"], int(datetime.datetime(2030, 6, 15, 18, 30).timestamp() * 1000))
        settings.calendar.select_day(GLib.DateTime.new_local(2000, 1, 1, 0, 0, 0))
        with self.assertRaises(ValueError):
            settings.changes()


if __name__ == "__main__":
    unittest.main()
