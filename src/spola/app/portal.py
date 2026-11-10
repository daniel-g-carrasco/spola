"""org.freedesktop.portal.Background, called with Gio (libportal is not in
the GNOME runtime).

RequestBackground with autostart makes the portal write
~/.config/autostart/<app-id>.desktop itself, under GNOME and under niri.
SetStatus (the line under the app name in GNOME's background apps) fails
under niri, where the portal cannot ask GNOME Shell for the app state:
errors are ignored, it is cosmetic.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from gi.repository import Gio, GLib

from .paths import in_flatpak

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.Background"


def request_background(connection: Gio.DBusConnection, reason: str, autostart: bool,
                       commandline: list[str], done: Callable[[bool, bool], None]) -> None:
    """done(background_allowed, autostart_enabled). Outside the Flatpak the
    portal cannot identify the app, so it is not called at all."""
    if not in_flatpak():
        done(True, False)
        return
    token = "spola" + secrets.token_hex(4)
    sender = connection.get_unique_name().lstrip(":").replace(".", "_")
    handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
    sub = 0

    def on_response(_conn, _sender, _path, _iface, _signal, params):
        connection.signal_unsubscribe(sub)
        code, results = params.unpack()
        done(code == 0 and bool(results.get("background", False)),
             code == 0 and bool(results.get("autostart", False)))

    sub = connection.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response", handle, None,
                                      Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)
    options = {
        "handle_token": GLib.Variant("s", token),
        "reason": GLib.Variant("s", reason),
        "autostart": GLib.Variant("b", autostart),
        "commandline": GLib.Variant("as", commandline),
        "dbus-activatable": GLib.Variant("b", False),
    }

    def on_called(conn, result):
        try:
            conn.call_finish(result)
        except GLib.Error:
            connection.signal_unsubscribe(sub)
            done(False, False)

    connection.call(BUS, PATH, IFACE, "RequestBackground", GLib.Variant("(sa{sv})", ("", options)),
                    None, Gio.DBusCallFlags.NONE, -1, None, on_called)


def set_status(connection: Gio.DBusConnection, message: str) -> None:
    if not in_flatpak():
        return
    options = {"message": GLib.Variant("s", message[:96].replace("\n", " "))}
    connection.call(BUS, PATH, IFACE, "SetStatus", GLib.Variant("(a{sv})", (options,)),
                    None, Gio.DBusCallFlags.NONE, -1, None, _ignore)


def _ignore(conn, result) -> None:
    try:
        conn.call_finish(result)
    except GLib.Error:
        pass
