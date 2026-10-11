"""The D-Bus interface the file manager extension talks to.

    io.github.daniel_g_carrasco.spola          (the app's own name)
    /io/github/daniel_g_carrasco/spola/Sync
    io.github.daniel_g_carrasco.spola.Sync1

States are integers (states.State). Paths travel as file:// URIs; the
actions also take a plain absolute path, for the file managers whose menus
can only run a command (Dolphin's service menu, Thunar's custom actions).
The extension lives in the file manager's process on the host and must never
wait: everything here answers from memory, except CreateShareLink, which
asks rclone in a thread and answers when it is done.

OpenOnKoofr and ShowShareLink do the whole job in Spola (open the browser,
show the link with a Copy button): a command run from a menu cannot read an
answer, and on Wayland an app in the background cannot set the clipboard.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable

from gi.repository import Gio, GLib

from . import APP_ID, koofr_api
from .engine import Engine
from .i18n import _
from .paths import Paths
from .rclone import koofr_env, remote_spec
from .states import State, ancestors_and_self

OBJECT_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
INTERFACE = f"{APP_ID}.Sync1"
VERSION = 1
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
    <signal name="StatesChanged"><arg type="a{{su}}" name="states"/></signal>
    <signal name="RootChanged"><arg type="s" name="root"/></signal>
  </interface>
</node>
"""


def uri_for(path: str) -> str:
    return Gio.File.new_for_path(path).get_uri()


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
                 show_link: Callable[[str, str], None] = lambda name, url: None,
                 tell: Callable[[str], None] = lambda message: None):
        self.engine = engine
        self.show_window = show_window
        self.open_url = open_url          # the browser, from Spola
        self.show_link = show_link        # (file name, link): a window with a Copy button
        self.tell = tell                  # a notification: what went wrong
        self.connection: Gio.DBusConnection | None = None
        self._reg = 0
        self._pending: dict[str, int] = {}
        self._flush_source = 0
        self._handlers = [
            engine.connect("paths-changed", self._on_paths_changed),
            engine.connect("root-changed", self._on_root_changed),
            engine.connect("pairs-changed", lambda _e: self._emit("RootChanged", GLib.Variant("(s)", ("",)))),
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
        if not path:
            return None
        for pair in self.engine.cfg.pairs:
            root = pair.local.rstrip("/")
            if path == root or path.startswith(root + "/"):
                return pair, path[len(root):].strip("/")
        return None

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
        return {name: int(state) for name, state in rt.states.folder_states(rel, children).items()}

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
            elif method == "ShowShareLink":
                (uri,) = params.unpack()
                if self.locate(uri) is None:
                    self._not_synced(uri)
                else:
                    name = os.path.basename(path_for(uri) or uri)
                    self._share_link(uri, None, lambda url, error: self.show_link(name, url) if url
                                     else self.tell(error))
                invocation.return_value(None)
            elif method == "ShowWindow":
                (what,) = params.unpack()
                self.show_window(what)
                invocation.return_value(None)
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as e:  # never let the file manager hang on us
            invocation.return_dbus_error(f"{INTERFACE}.Failed", str(e))

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

    def _share_link(self, uri: str, invocation, then=None) -> None:
        """rclone link in a thread; the link goes back over D-Bus
        (invocation) or to then(url, error)."""
        found = self.locate(uri)
        creds, rclone = self.engine.creds, self.engine.rclone
        if not found or creds is None or rclone is None:
            if invocation is not None:
                invocation.return_dbus_error(f"{INTERFACE}.NotSynced", "not inside a synced folder, or not signed in")
            elif then is not None:
                then("", _("Not signed in to Koofr"))
            return
        pair, rel = found
        remote = pair.remote.rstrip("/") + ("/" + rel if rel else "")
        paths: Paths = self.engine.paths

        def work():
            try:
                proc = subprocess.run([rclone, "link", remote_spec(remote), "--config", str(paths.rclone_config)],
                                      env=koofr_env(creds.user, creds.obscured), capture_output=True, text=True,
                                      timeout=60)
                url = proc.stdout.strip() if proc.returncode == 0 else ""
                error = "" if url else (proc.stderr.strip().splitlines() or ["rclone link failed"])[-1]
            except (OSError, subprocess.SubprocessError) as e:
                url, error = "", str(e)
            GLib.idle_add(done, url, error)

        def done(url, error):
            if then is not None:
                then(url, error)
            elif url:
                invocation.return_value(GLib.Variant("(s)", (url,)))
            else:
                invocation.return_dbus_error(f"{INTERFACE}.Failed", error)
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
