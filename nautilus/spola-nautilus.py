#!/usr/bin/env python3
"""Spola in Files (Nautilus): sync emblems on files and folders, and a few
context menu entries. Installed by Spola into
~/.local/share/nautilus-python/extensions/ (the emblems next to it in
~/.local/share/icons/hicolor/scalable/emblems/).

It never waits: Files asks about one file at a time on its main thread, so
every answer comes from a cache. On a miss the folder is queried once,
asynchronously, from Spola's D-Bus service, and the files are refreshed when
the answer lands. If Spola is not running, nothing is shown and nothing
slows Files down.
"""

import os

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

try:
    for version in ("4.1", "4.0"):
        try:
            gi.require_version("Nautilus", version)
            break
        except ValueError:
            continue
    from gi.repository import Nautilus
except (ImportError, ValueError):
    Nautilus = None

APP_ID = "io.github.daniel_g_carrasco.spola"
OBJECT_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
INTERFACE = APP_ID + ".Sync1"

# states.State in Spola; emblem names resolve to emblem-spola-<state>-symbolic
SYNCED, SYNCING, PENDING, ERROR, CONFLICT, EXCLUDED = 1, 2, 3, 4, 5, 6
EMBLEMS = {SYNCED: "spola-synced-symbolic", SYNCING: "spola-syncing-symbolic", PENDING: "spola-pending-symbolic",
           ERROR: "spola-error-symbolic", CONFLICT: "spola-conflict-symbolic", EXCLUDED: "spola-excluded-symbolic"}
MAX_FOLDERS = 400          # folders kept in the cache (oldest dropped first)
CALL_TIMEOUT_MS = 10000

HOME_REAL = os.path.realpath("/home")
LOG = os.environ.get("SPOLA_NAUTILUS_LOG")     # tests inside a real Files: one line per event


def log(message):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(message + "\n")


def path_of(uri):
    """Local path of a file:// URI, with /home mapped to its real place
    (Spola names paths as the system does, e.g. /var/home/...)."""
    f = Gio.File.new_for_uri(uri)
    if f.get_uri_scheme() != "file":
        return None
    path = f.get_path()
    if path and HOME_REAL != "/home" and path.startswith("/home/"):
        path = HOME_REAL + path[len("/home"):]
    return path


def uri_of(path):
    return Gio.File.new_for_path(path).get_uri()


class StateCache:
    """Everything the provider knows, keyed by local path; no I/O, no D-Bus."""

    def __init__(self):
        self.roots = []                 # [(path, pair id)]
        self.states = {}                # path -> state
        self.folders = {}               # folder path -> True, insertion order = age
        self.in_flight = set()          # folders being asked about

    def set_roots(self, roots):
        self.roots = [(p.rstrip("/"), pair_id) for p, pair_id in roots]

    def root_of(self, path):
        for root, _pair_id in self.roots:
            if path == root or path.startswith(root + "/"):
                return root
        return None

    def state(self, path):
        return self.states.get(path)

    def folder_known(self, folder):
        return folder in self.folders

    def set_folder(self, folder, name_states):
        self.folders.pop(folder, None)
        self.folders[folder] = True
        for name, state in name_states.items():
            self.states[os.path.join(folder, name)] = state
        while len(self.folders) > MAX_FOLDERS:
            old = next(iter(self.folders))
            self.drop_folder(old)

    def drop_folder(self, folder):
        self.folders.pop(folder, None)
        prefix = folder + "/"
        for path in [p for p in self.states if p.startswith(prefix) and "/" not in p[len(prefix):]]:
            del self.states[path]

    def apply(self, path_states):
        """States pushed by Spola; only paths we already know get updated."""
        changed = []
        for path, state in path_states.items():
            if path in self.states or os.path.dirname(path) in self.folders or any(path == r for r, _ in self.roots):
                self.states[path] = state
                changed.append(path)
        return changed

    def drop_root(self, root):
        root = root.rstrip("/")
        gone = [p for p in self.states if p == root or p.startswith(root + "/")]
        for p in gone:
            del self.states[p]
        for folder in [f for f in self.folders if f == root or f.startswith(root + "/")]:
            del self.folders[folder]
        return gone

    def clear(self):
        gone = list(self.states)
        self.states.clear()
        self.folders.clear()
        self.in_flight.clear()
        return gone


class SpolaProvider(GObject.GObject, *([Nautilus.InfoProvider, Nautilus.MenuProvider] if Nautilus else [])):
    def __init__(self):
        super().__init__()
        self.cache = StateCache()
        self.proxy = None
        self._pending_folders = set()
        self._schedule = 0
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION,
                                  Gio.DBusProxyFlags.DO_NOT_AUTO_START | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                                  None, APP_ID, OBJECT_PATH, INTERFACE, None, self._proxy_ready)

    # -- connection to Spola ---------------------------------------------------------

    def _proxy_ready(self, _source, result):
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error:
            return
        self.proxy.connect("g-signal", self._on_signal)
        self.proxy.connect("notify::g-name-owner", lambda *_: self._owner_changed())
        self._owner_changed()

    def _owner_changed(self):
        if self.proxy and self.proxy.get_name_owner():
            self._call("GetRoots", None, self._roots_ready)
        else:
            self._refresh(self.cache.clear())
            self.cache.set_roots([])

    def _roots_ready(self, reply):
        if reply is None:
            return
        (roots,) = reply.unpack()
        self.cache.set_roots([(path_of(uri) or "", pair_id) for uri, pair_id in roots])
        log(f"roots {len(roots)}")
        self._refresh(self.cache.clear())
        self._fetch_root_states()

    def _fetch_root_states(self):
        """The roots themselves (the synced folders as seen from their
        parent) are nobody's children: their states come from GetStates."""
        if not self.cache.roots or "<roots>" in self.cache.in_flight:
            return
        self.cache.in_flight.add("<roots>")
        uris = [uri_of(root) for root, _pair_id in self.cache.roots]
        self._call("GetStates", GLib.Variant("(as)", (uris,)), self._root_states_ready)

    def _root_states_ready(self, reply):
        self.cache.in_flight.discard("<roots>")
        if reply is None:
            return
        (states,) = reply.unpack()
        changed = self.cache.apply({path_of(u) or "": s for u, s in states.items()})
        log(f"root states {len(changed)}")
        self._refresh(changed)

    def _call(self, method, params, done):
        if not self.proxy or not self.proxy.get_name_owner():
            done(None)
            return

        def finish(proxy, result):
            try:
                done(proxy.call_finish(result))
            except GLib.Error:
                done(None)

        self.proxy.call(method, params, Gio.DBusCallFlags.NO_AUTO_START, CALL_TIMEOUT_MS, None, finish)

    def _on_signal(self, _proxy, _sender, name, params):
        if name == "StatesChanged":
            (states,) = params.unpack()
            changed = self.cache.apply({path_of(u) or "": s for u, s in states.items()})
            self._refresh(changed)
        elif name == "RootChanged":
            (uri,) = params.unpack()
            if uri:
                root = path_of(uri)
                gone = self.cache.drop_root(root) if root else []
                self._fetch_root_states()
            else:
                gone = self.cache.clear()
                self._call("GetRoots", None, self._roots_ready)
            self._refresh(gone)

    # -- emblems ---------------------------------------------------------------------

    def update_file_info(self, file):
        """Called by Files for every file of a folder, on its main thread:
        answer from the cache and return at once."""
        if Nautilus is None:
            return
        uri = file.get_uri()
        path = path_of(uri)
        if not path or self.cache.root_of(path) is None:
            return
        state = self.cache.state(path)
        if state is None:
            if any(path == r for r, _ in self.cache.roots):
                self._fetch_root_states()
                return
            folder = os.path.dirname(path)
            if not self.cache.folder_known(folder) and folder not in self.cache.in_flight:
                self._pending_folders.add(folder)
                if not self._schedule:
                    self._schedule = GLib.idle_add(self._fetch_pending)
            return
        emblem = EMBLEMS.get(state)
        if emblem:
            file.add_emblem(emblem)
            log(f"emblem {emblem} {path}")

    def _fetch_pending(self):
        self._schedule = 0
        folders, self._pending_folders = self._pending_folders, set()
        for folder in folders:
            if folder in self.cache.in_flight:
                continue
            self.cache.in_flight.add(folder)
            self._call("GetFolderStates", GLib.Variant("(s)", (uri_of(folder),)),
                       lambda reply, folder=folder: self._folder_ready(folder, reply))
        return False

    def _folder_ready(self, folder, reply):
        self.cache.in_flight.discard(folder)
        if reply is None:
            return
        (states,) = reply.unpack()
        log(f"folder {folder}: {len(states)} entries")
        self.cache.set_folder(folder, states)
        self._refresh([os.path.join(folder, name) for name in states] + [folder])

    def _refresh(self, paths):
        """Ask Files to call update_file_info again, only for files it has."""
        if Nautilus is None:
            return
        for path in paths:
            info = Nautilus.FileInfo.lookup_for_uri(uri_of(path))
            if info is not None:
                info.invalidate_extension_info()

    # -- menus -------------------------------------------------------------------------

    def _menu_items(self, files, background):
        if not files or not self.proxy or not self.proxy.get_name_owner():
            return []
        paths = [path_of(f.get_uri()) for f in files]
        if any(p is None or self.cache.root_of(p) is None for p in paths):
            return []
        target = paths[0]
        prefix = "SpolaBackground" if background else "Spola"
        items = []
        sync = Nautilus.MenuItem(name=f"{prefix}::SyncNow", label=_("Sync Now with Koofr"))
        sync.connect("activate", lambda *_: self._call("SyncNow", GLib.Variant("(s)", (uri_of(target),)), lambda r: None))
        items.append(sync)
        if len(paths) == 1:
            web = Nautilus.MenuItem(name=f"{prefix}::OpenOnKoofr", label=_("Open on Koofr"))
            web.connect("activate", lambda *_: self._call("GetWebUrl", GLib.Variant("(s)", (uri_of(target),)), self._open_url))
            items.append(web)
            link = Nautilus.MenuItem(name=f"{prefix}::ShareLink", label=_("Copy Koofr Share Link"))
            link.connect("activate", lambda *_: self._call("CreateShareLink", GLib.Variant("(s)", (uri_of(target),)),
                                                           self._copy_url))
            items.append(link)
        return items

    def get_file_items(self, files):
        return self._menu_items(list(files), False)

    def get_background_items(self, folder):
        return self._menu_items([folder], True)

    @staticmethod
    def _open_url(reply):
        if reply is not None:
            (url,) = reply.unpack()
            Gio.AppInfo.launch_default_for_uri(url, None)

    @staticmethod
    def _copy_url(reply):
        if reply is None:
            return
        (url,) = reply.unpack()
        try:
            gi.require_version("Gdk", "4.0")
            from gi.repository import Gdk
            Gdk.Display.get_default().get_clipboard().set(url)
        except (ImportError, ValueError, AttributeError):
            pass


def _(text):
    """Translations of the menu entries, from Spola's catalogue when installed."""
    import gettext
    for directory in (os.path.expanduser("~/.local/share/locale"), "/usr/local/share/locale", "/usr/share/locale"):
        if gettext.find("spola", directory):
            return gettext.translation("spola", directory).gettext(text)
    return text
