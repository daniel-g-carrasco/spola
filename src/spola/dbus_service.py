"""The D-Bus interface the file manager extension talks to.

    io.github.daniel_g_carrasco.spola          (the app's own name)
    /io/github/daniel_g_carrasco/spola/Sync
    io.github.daniel_g_carrasco.spola.Sync1

States are integers (states.State). Paths travel as file:// URIs; the
actions also take a plain absolute path, for the file managers whose menus
can only run a command (Dolphin's service menu, Thunar's custom actions).
The extension lives in the file manager's process on the host and must never
wait: everything here answers from memory, except CreateShareLink, which
reads an existing Koofr link in a thread and answers when it is done.

OpenOnKoofr and Share do the whole job in Spola (open the browser or sharing
dialog): a command run from a menu cannot read an
answer, and on Wayland an app in the background cannot set the clipboard.

Version 2 adds the previous versions (versions.py), answered from the index
in memory: GetFolderVersions (the names of a folder with kept copies, for
the menus), ListVersions and ListDeleted (a{sv} per copy: id, path, time,
size, reason, expires), RestoreVersion (mode "copy" or "replace"; returns
the URI written), ShowVersions and ShowDeleted (the window), and the
VersionsChanged signal (the root of a pair whose copies were read again).
Version 3 adds Share. ShowShareLink opens the same dialog; CreateShareLink
only retrieves an existing usable unprotected link. Creating access always
requires an explicit action in the dialog, including from older extensions.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

from gi.repository import Gio, GLib

from . import APP_ID, explorer, koofr_api, places
from .engine import Engine
from .i18n import _
from .rclone import default_excluded
from .states import State, ancestors_and_self

OBJECT_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
INTERFACE = f"{APP_ID}.Sync1"
VERSION = 3
BATCH_MS = 300

XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="GetVersion"><arg type="u" name="version" direction="out"/></method>
    <method name="GetRoots"><arg type="a(ss)" name="roots" direction="out"/></method>
    <method name="GetFolderStates">
      <arg type="s" name="folder" direction="in"/><arg type="a{{su}}" name="states" direction="out"/>
    </method>
    <method name="GetStates">
      <arg type="as" name="uris" direction="in"/><arg type="a{{su}}" name="states" direction="out"/>
    </method>
    <method name="SyncNow"><arg type="s" name="uri" direction="in"/></method>
    <method name="GetWebUrl"><arg type="s" name="uri" direction="in"/><arg type="s" name="url" direction="out"/></method>
    <method name="CreateShareLink"><arg type="s" name="uri" direction="in"/><arg type="s" name="url" direction="out"/></method>
    <method name="ShowWindow"><arg type="s" name="what" direction="in"/></method>
    <method name="OpenOnKoofr"><arg type="s" name="uri" direction="in"/></method>
    <method name="ShowShareLink"><arg type="s" name="uri" direction="in"/></method>
    <method name="Share"><arg type="s" name="uri" direction="in"/></method>
    <method name="GetFolderVersions">
      <arg type="s" name="folder" direction="in"/><arg type="as" name="names" direction="out"/>
    </method>
    <method name="ListVersions"><arg type="s" name="uri" direction="in"/><arg type="aa{{sv}}" name="versions" direction="out"/></method>
    <method name="ListDeleted"><arg type="s" name="folder" direction="in"/><arg type="aa{{sv}}" name="versions" direction="out"/></method>
    <method name="RestoreVersion">
      <arg type="s" name="uri" direction="in"/><arg type="s" name="id" direction="in"/>
      <arg type="s" name="mode" direction="in"/><arg type="s" name="restored" direction="out"/>
    </method>
    <method name="ShowVersions"><arg type="s" name="uri" direction="in"/></method>
    <method name="ShowDeleted"><arg type="s" name="uri" direction="in"/></method>
    <signal name="StatesChanged"><arg type="a{{su}}" name="states"/></signal>
    <signal name="VersionsChanged"><arg type="s" name="root"/></signal>
    <signal name="RootChanged"><arg type="s" name="root"/></signal>
  </interface>
</node>
"""


def uri_for(path: str) -> str:
    return Gio.File.new_for_path(path).get_uri()


def sendable(name: str) -> bool:
    """Whether a name can go over D-Bus: one that is not UTF-8 on the disk
    comes from os.scandir with surrogates, which no "s" can hold, and the
    whole answer would fail (the emblems of the whole folder gone). Such
    names are left out: the file managers cannot show them right anyway."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def path_for(uri: str) -> str | None:
    """The local path of a file:// URI, or of an absolute path as it is."""
    if uri.startswith("/"):
        path = os.path.normpath(uri)
    else:
        f = Gio.File.new_for_uri(uri)
        path = f.get_path() if f.get_uri_scheme() == "file" else None
    if path and path.startswith("/home/"):
        real_home = os.path.realpath("/home")
        if real_home != "/home":
            path = real_home + path[len("/home"):]
    return path


class SyncService:
    def __init__(self, engine: Engine, show_window: Callable[[str], None],
                 open_url: Callable[[str], None] = lambda url: None,
                 tell: Callable[[str], None] = lambda message: None,
                 show_versions: Callable[[str, str, str], None] = lambda pair_id, rel, kind: None,
                 show_share: Callable[[str], None] | None = None):
        self.engine = engine
        self.show_window = show_window
        self.open_url = open_url          # the browser, from Spola
        self.tell = tell                  # a notification: what went wrong
        self.show_versions = show_versions   # (pair id, relative path, "file" or "deleted"): the window
        self.show_share = show_share or (lambda path: self.show_window(""))  # local path: sharing dialog
        self.connection: Gio.DBusConnection | None = None
        self._reg = 0
        self._pending: dict[str, int] = {}
        self._flush_source = 0
        self._handlers = [
            engine.connect("paths-changed", self._on_paths_changed),
            engine.connect("root-changed", self._on_root_changed),
            engine.connect("pairs-changed", lambda _e: self._emit("RootChanged", GLib.Variant("(s)", ("",)))),
            engine.connect("versions-changed", self._on_versions_changed),
        ]

    def start(self, connection: Gio.DBusConnection) -> None:
        self.connection = connection
        info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        self._reg = connection.register_object(OBJECT_PATH, info, self._call, None, None)
        # The application owns its bus name a moment before this object
        # exists: a Files extension that asked in that moment got an error.
        # "Every root changed" makes it ask again, now that we answer.
        GLib.idle_add(lambda: self._emit("RootChanged", GLib.Variant("(s)", ("",))) and False)

    def stop(self) -> None:
        for handler in self._handlers:
            self.engine.disconnect(handler)
        if self.connection and self._reg:
            self.connection.unregister_object(self._reg)
            self._reg = 0

    # -- lookups ----------------------------------------------------------------

    def locate(self, uri: str):
        """(pair, relative path) for a URI inside a pair, else None."""
        path = path_for(uri)
        return places.locate(self.engine.cfg.pairs, path) if path else None

    def state_of(self, uri: str) -> int:
        found = self.locate(uri)
        if found is None:
            return int(State.NONE)
        pair, rel = found
        rt = self.engine.runtime.get(pair.id)
        if rt is None or not pair.initialized:
            return int(State.NONE)
        path = os.path.join(pair.local, rel) if rel else pair.local
        return int(rt.states.state_of(rel, os.path.isdir(path)))

    def folder_states(self, uri: str) -> dict[str, int]:
        found = self.locate(uri)
        if found is None:
            return {}
        pair, rel = found
        rt = self.engine.runtime.get(pair.id)
        if rt is None or not pair.initialized:
            return {}
        folder = os.path.join(pair.local, rel) if rel else pair.local
        try:
            children = [(e.name, e.is_dir(follow_symlinks=False)) for e in os.scandir(folder)]
        except OSError:
            return {}
        return {name: int(state) for name, state in rt.states.folder_states(rel, children).items() if sendable(name)}

    # -- D-Bus ------------------------------------------------------------------

    def _call(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            if method == "GetVersion":
                invocation.return_value(GLib.Variant("(u)", (VERSION,)))
            elif method == "GetRoots":
                roots = [(uri_for(p.local), p.id) for p in self.engine.cfg.pairs if p.initialized]
                invocation.return_value(GLib.Variant("(a(ss))", (roots,)))
            elif method == "GetFolderStates":
                (uri,) = params.unpack()
                invocation.return_value(GLib.Variant("(a{su})", (self.folder_states(uri),)))
            elif method == "GetStates":
                (uris,) = params.unpack()
                invocation.return_value(GLib.Variant("(a{su})", ({u: self.state_of(u) for u in uris},)))
            elif method == "SyncNow":
                (uri,) = params.unpack()
                found = self.locate(uri)
                if found:
                    self.engine.request_sync(found[0].id, "manual")
                else:
                    self._not_synced(uri)
                invocation.return_value(None)
            elif method == "GetWebUrl":
                (uri,) = params.unpack()
                url = self.web_url(uri)
                if not url:
                    invocation.return_dbus_error(f"{INTERFACE}.NotSynced", "not inside a synced folder")
                    return
                invocation.return_value(GLib.Variant("(s)", (url,)))
            elif method == "OpenOnKoofr":
                (uri,) = params.unpack()
                url = self.web_url(uri)
                if url:
                    self.open_url(url)
                else:
                    self._not_synced(uri)
                invocation.return_value(None)
            elif method == "CreateShareLink":
                (uri,) = params.unpack()
                self._share_link(uri, invocation)
            elif method in ("Share", "ShowShareLink"):
                (uri,) = params.unpack()
                _found, refusal = self._sharing_target(uri)
                if refusal:
                    self.tell(refusal)
                else:
                    self.show_share(path_for(uri))
                invocation.return_value(None)
            elif method == "GetFolderVersions":
                (uri,) = params.unpack()
                invocation.return_value(GLib.Variant("(as)", (self.folder_versions(uri),)))
            elif method == "ListVersions":
                (uri,) = params.unpack()
                found = self.locate(uri)
                listed = self._as_dicts(found[0], self._index(found[0]).of(found[1])) if found and found[1] else []
                invocation.return_value(GLib.Variant("(aa{sv})", (listed,)))
            elif method == "ListDeleted":
                (uri,) = params.unpack()
                found = self.locate(uri)
                listed = self._as_dicts(found[0], self._index(found[0]).deleted_under(found[1])) if found else []
                invocation.return_value(GLib.Variant("(aa{sv})", (listed,)))
            elif method == "RestoreVersion":
                uri, vid, mode = params.unpack()
                found = self.locate(uri)
                if not found:
                    invocation.return_dbus_error(f"{INTERFACE}.NotSynced", "not inside a synced folder")
                    return
                local = found[0].local

                def restored(rel: str, error: str) -> None:      # the answer once the copy is made
                    if error:
                        invocation.return_dbus_error(f"{INTERFACE}.Failed", error)
                    else:
                        invocation.return_value(GLib.Variant("(s)", (uri_for(os.path.join(local, rel)),)))
                self.engine.restore_version(found[0].id, vid, mode, restored)
            elif method in ("ShowVersions", "ShowDeleted"):
                (uri,) = params.unpack()
                found = self.locate(uri)
                if not found:
                    self._not_synced(uri)
                else:
                    pair, rel = found
                    folder = not rel or os.path.isdir(os.path.join(pair.local, rel))
                    if method == "ShowDeleted" or folder:
                        # a file given to ShowDeleted: the folder it is in
                        rel = rel if folder else os.path.dirname(rel)
                        self.show_versions(pair.id, rel, "deleted")
                    else:
                        self.show_versions(pair.id, rel, "file")
                invocation.return_value(None)
            elif method == "ShowWindow":
                (what,) = params.unpack()
                self.show_window(what)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as e:  # never let the file manager hang on us
            invocation.return_dbus_error(f"{INTERFACE}.Failed", str(e))

    # -- previous versions ---------------------------------------------------------

    def _index(self, pair):
        return self.engine.versions_index(pair.id)

    def folder_versions(self, uri: str) -> list[str]:
        """The names in this folder with at least one kept copy (the file
        managers show "Previous Versions…" on those only)."""
        found = self.locate(uri)
        if not found:
            return []
        return sorted(name for name in self._index(found[0]).names_in(found[1]) if sendable(name))

    def _as_dicts(self, pair, found) -> list[dict]:
        days = self.engine.cfg.backup_days
        return [{"id": GLib.Variant("s", v.id), "path": GLib.Variant("s", uri_for(os.path.join(pair.local, v.rel))),
                 "time": GLib.Variant("x", int(v.time)), "size": GLib.Variant("t", v.size),
                 "reason": GLib.Variant("s", v.reason), "expires": GLib.Variant("x", int(v.expires(days)))}
                for v in found if sendable(v.id)]       # (the path goes as a URI: always sendable)

    def _on_versions_changed(self, _engine, pair_id: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        if pair is not None:
            self._emit("VersionsChanged", GLib.Variant("(s)", (uri_for(pair.local),)))

    def web_url(self, uri: str) -> str:
        """Koofr's web page of the item: the folder itself, or the folder
        that holds the file. '' outside the synced folders."""
        found = self.locate(uri)
        if not found:
            return ""
        pair, rel = found
        remote = pair.remote.rstrip("/") + ("/" + rel if rel else "")
        if rel and not os.path.isdir(os.path.join(pair.local, rel)):
            remote = os.path.dirname(remote) or "/"
        return koofr_api.web_url(self.engine.cfg.mount_id, remote)

    def _not_synced(self, uri: str) -> None:
        name = os.path.basename(path_for(uri) or uri) or uri
        self.tell(_("“{name}” is not in a folder synced with Koofr.").format(name=name))

    def _sharing_target(self, uri: str):
        """Shared validation for dialog opening and legacy link retrieval."""
        found = self.locate(uri)
        if found is None:
            name = os.path.basename(path_for(uri) or uri) or uri
            return None, _("“{name}” is not in a folder synced with Koofr.").format(name=name)
        if self.engine.creds is None:
            return None, _("Not signed in to Koofr")
        pair, rel = found
        refusal = explorer.link_refusal(pair, rel, os.path.isdir(path_for(uri)), default_excluded, self.engine.paths)
        return found, refusal

    def _share_link(self, uri: str, invocation) -> None:
        """Compatibility retrieval: read existing public access, never create it."""
        found, refusal = self._sharing_target(uri)
        if refusal:
            invocation.return_dbus_error(f"{INTERFACE}.NotSynced", refusal)
            return
        from . import sharing
        pair, rel = found
        remote = explorer.remote_path(pair, rel)
        creds = self.engine.creds
        mount_id = self.engine.cfg.mount_id

        def work():
            try:
                link = sharing.Client(creds.user, creds.password, mount_id).load(remote).link
                GLib.idle_add(done, link, "")
            except Exception:
                # Never expose arbitrary HTTP exception text (which can contain credentials).
                GLib.idle_add(done, None, _("Sharing could not be checked. Open Share… to try again."))

        def done(link, error):
            if self.engine.creds is not creds or self.engine.cfg.mount_id != mount_id:
                invocation.return_dbus_error(f"{INTERFACE}.NotSynced", _("The Koofr account changed. Open Share… again."))
                return False
            if link and link.url and link.active and not link.has_password and not link.password_required:
                invocation.return_value(GLib.Variant("(s)", (link.url,)))
            else:
                name = "Failed" if error else "ExplicitSharingRequired"
                invocation.return_dbus_error(f"{INTERFACE}.{name}", error or _(
                    "Open Share… to explicitly create or manage a link. No usable unprotected link exists."))
                self.show_share(path_for(uri))
            return False

        threading.Thread(target=work, name="share-link", daemon=True).start()

    # -- signals -------------------------------------------------------------------

    def _emit(self, name: str, params: GLib.Variant) -> None:
        if self.connection:
            self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, name, params)

    def _on_paths_changed(self, _engine, pair_id: str, rels) -> None:
        pair = self.engine.cfg.pair(pair_id)
        rt = self.engine.runtime.get(pair_id)
        if pair is None or rt is None:
            return
        for rel in rels:
            path = os.path.join(pair.local, rel) if rel else pair.local
            self._pending[uri_for(path)] = int(rt.states.state_of(rel, os.path.isdir(path)))
        if not self._flush_source:
            self._flush_source = GLib.timeout_add(BATCH_MS, self._flush)

    def _flush(self) -> bool:
        self._flush_source = 0
        if self._pending:
            self._emit("StatesChanged", GLib.Variant("(a{su})", (self._pending,)))
            self._pending = {}
        return False

    def _on_root_changed(self, _engine, pair_id: str) -> None:
        pair = self.engine.cfg.pair(pair_id)
        if pair is not None:
            self._pending = {}
            self._emit("RootChanged", GLib.Variant("(s)", (uri_for(pair.local),)))


__all__ = ["SyncService", "OBJECT_PATH", "INTERFACE", "uri_for", "path_for", "ancestors_and_self"]
