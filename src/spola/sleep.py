"""The computer's sleep, through logind: an inhibitor while a sync runs,
and a word before the machine sleeps anyway.

GTK's own inhibit goes through the desktop's session manager (the Inhibit
portal, in the Flatpak): under GNOME it works, under other compositors
(niri, sway) the portal answers and nothing is inhibited, so the laptop
suspended in the middle of a 320 GB upload on 2026-10-15. logind is there
under every desktop: a "block" lock on "sleep" (the system bus,
--system-talk-name=org.freedesktop.login1 in the manifest) keeps the
machine awake, lid included, until the sync is done; PrepareForSleep says
when it sleeps regardless (a privileged or critical-battery suspend), so
that bisync gets its graceful stop first and the run starts again after.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from gi.repository import Gio, GLib

from .log import complain

LOGIND = "org.freedesktop.login1"
PATH = "/org/freedesktop/login1"
MANAGER = "org.freedesktop.login1.Manager"


class SleepWatch:
    def __init__(self, on_sleep: Callable[[], None], on_wake: Callable[[], None]):
        self.on_sleep, self.on_wake = on_sleep, on_wake
        self._bus: Gio.DBusConnection | None = None
        self._subscription = 0
        self._fds: dict[int, int] = {}
        self._next = 1

    def start(self) -> None:
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._subscription = self._bus.signal_subscribe(LOGIND, MANAGER, "PrepareForSleep", PATH, None,
                                                             Gio.DBusSignalFlags.NONE, self._prepare)
        except GLib.Error as e:
            complain("reaching logind", e)
            self._bus = None

    def stop(self) -> None:
        if self._bus is not None and self._subscription:
            self._bus.signal_unsubscribe(self._subscription)
            self._subscription = 0
        for cookie in list(self._fds):
            self.release(cookie)

    def _prepare(self, _bus, _sender, _path, _iface, _signal, params) -> None:
        (sleeping,) = params.unpack()
        (self.on_sleep if sleeping else self.on_wake)()

    def inhibit(self, reason: str) -> int:
        """A "block" lock on sleep, held until release(); 0 when logind would
        not give it (then the caller falls back to the desktop's inhibit)."""
        if self._bus is None:
            return 0
        try:
            _reply, fds = self._bus.call_with_unix_fd_list_sync(
                LOGIND, PATH, MANAGER, "Inhibit", GLib.Variant("(ssss)", ("sleep", "Spola", reason, "block")),
                GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 5000, None, None)
        except GLib.Error as e:
            complain("inhibiting sleep", e)
            return 0
        if fds is None or fds.get_length() < 1:
            return 0
        cookie, self._next = self._next, self._next + 1
        self._fds[cookie] = fds.get(0)      # logind holds the lock while this end stays open
        return cookie

    def release(self, cookie: int) -> None:
        fd = self._fds.pop(cookie, None)
        if fd is not None:
            os.close(fd)

    @property
    def holding(self) -> bool:
        return bool(self._fds)
