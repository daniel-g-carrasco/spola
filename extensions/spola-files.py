#!/usr/bin/env python3
"""Spola in Files (Nautilus), Nemo and Caja: sync emblems on files and
folders, and a few context menu entries. The three share their extension
API (Nemo and Caja are forks of Nautilus), so this one file serves them all:
Spola installs a copy into each one's folder with FILE_MANAGER filled in
(~/.local/share/{nautilus,nemo,caja}-python/extensions/), the emblems into
~/.local/share/icons/hicolor/scalable/emblems/.

It never waits: Files asks about one file at a time on its main thread, so
every answer comes from a cache. On a miss the folder is queried once,
asynchronously, from Spola's D-Bus service, and the files are refreshed when
the answer lands. If Spola is not running, nothing is shown and nothing
slows Files down.
"""

import importlib
import os
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

FILE_MANAGER = "Nautilus"        # set by Spola in the copies for Nemo and Caja
VERSIONS = {"Nautilus": ("4.1", "4.0"), "Nemo": ("3.0",), "Caja": ("2.0",)}

try:
    for version in VERSIONS[FILE_MANAGER]:
        try:
            gi.require_version(FILE_MANAGER, version)
            break
        except ValueError:
            continue
    FM = importlib.import_module(f"gi.repository.{FILE_MANAGER}")
except (ImportError, ValueError):
    FM = None

APP_ID = "io.github.daniel_g_carrasco.spola"
OBJECT_PATH = "/io/github/daniel_g_carrasco/spola/Sync"
INTERFACE = APP_ID + ".Sync1"

# states.State in Spola; emblem names resolve to emblem-spola-<state>-symbolic
SYNCED, SYNCING, PENDING, ERROR, CONFLICT, EXCLUDED = 1, 2, 3, 4, 5, 6
EMBLEMS = {SYNCED: "spola-synced-symbolic", SYNCING: "spola-syncing-symbolic", PENDING: "spola-pending-symbolic",
           ERROR: "spola-error-symbolic", CONFLICT: "spola-conflict-symbolic", EXCLUDED: "spola-excluded-symbolic"}
MAX_FOLDERS = 400          # folders kept in the cache (oldest dropped first)
MAX_SEEN = 5000            # files Files asked about, to ask again when Spola comes back
CALL_TIMEOUT_MS = 10000
FOLDER_RETRY_S = 30     # a folder Spola could not answer for: asked again after this, not at every redraw
ROOTS_RETRY_S = (1, 2, 5, 10, 30)   # GetRoots failed while Spola is there: it may be starting

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
        # Spola 0.3.27 (D-Bus version 2): per folder, the names with kept
        # copies, for "Previous Versions…"; a folder not asked yet is absent
        self.versioned = {}
        # Files keeps the answer it got for a file until told otherwise: what
        # it asked while Spola was away (no roots, no emblem) must be asked
        # again when Spola comes back, or those files stay bare until Files
        # restarts. Most recent last.
        self.seen = {}

    def saw(self, path):
        self.seen.pop(path, None)
        self.seen[path] = True
        if len(self.seen) > MAX_SEEN:
            del self.seen[next(iter(self.seen))]

    def seen_in_roots(self):
        return [p for p in self.seen if self.root_of(p) is not None]

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

    def has_versions(self, path):
        names = self.versioned.get(os.path.dirname(path))
        return names is not None and os.path.basename(path) in names

    def drop_folder(self, folder):
        self.folders.pop(folder, None)
        self.versioned.pop(folder, None)
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
        for folder in [f for f in self.versioned if f == root or f.startswith(root + "/")]:
            del self.versioned[folder]
        return gone

    def clear(self):
        gone = list(self.states)
        self.states.clear()
        self.folders.clear()
        self.versioned.clear()
        self.in_flight.clear()
        return gone


def _providers():
    if FM is None:
        return []
    found = [FM.InfoProvider, FM.MenuProvider]
    if hasattr(FM, "NameAndDescProvider"):      # Nemo lists its extensions by name
        found.append(FM.NameAndDescProvider)
    return found


class SpolaProvider(GObject.GObject, *_providers()):
    spola_version = 0           # until Spola answers GetVersion (and for an older Spola)

    def __init__(self):
        super().__init__()
        self.cache = StateCache()
        self.proxy = None
        self._pending_folders = set()
        self._schedule = 0
        self._roots_retry = 0
        self.spola_version = 0      # Sync1: 2 brings previous versions, 3 explicit sharing
        Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION,
                                  Gio.DBusProxyFlags.DO_NOT_AUTO_START | Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES,
                                  None, APP_ID, OBJECT_PATH, INTERFACE, None, self._proxy_ready)

    # -- connection to Spola ---------------------------------------------------------

    def _proxy_ready(self, _source, result):
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as e:     # no emblems nor entries until the file manager restarts: said in its log
            print(f"spola-files: no connection to Spola: {e.message}", file=sys.stderr)
            return
        self.proxy.connect("g-signal", self._on_signal)
        self.proxy.connect("notify::g-name-owner", lambda *_: self._owner_changed())
        self._owner_changed()

    def _owner_changed(self):
        self._roots_retry = 0
        self.spola_version = 0
        if self.proxy and self.proxy.get_name_owner():
            self._call("GetVersion", None, self._version_ready)
            self._call("GetRoots", None, self._roots_ready)
        else:
            self._refresh(self.cache.clear())
            self.cache.set_roots([])

    def _version_ready(self, reply):
        if reply is not None:
            (self.spola_version,) = reply.unpack()

    def _roots_ready(self, reply):
        if reply is None:
            # Spola took its name but does not answer yet (still starting): ask again a little later
            if self.proxy and self.proxy.get_name_owner() and self._roots_retry < len(ROOTS_RETRY_S):
                delay = ROOTS_RETRY_S[self._roots_retry]
                self._roots_retry += 1
                log(f"roots: no answer, again in {delay} s")
                GLib.timeout_add_seconds(delay, lambda: self._call("GetRoots", None, self._roots_ready) and False)
            return
        self._roots_retry = 0
        (roots,) = reply.unpack()
        self.cache.set_roots([(path_of(uri) or "", pair_id) for uri, pair_id in roots])
        log(f"roots {len(roots)}")
        self._refresh(self.cache.clear() + self.cache.seen_in_roots())
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
        elif name == "VersionsChanged":
            (uri,) = params.unpack()
            root = path_of(uri) if uri else None
            for folder in [f for f in self.cache.versioned if root is None or f == root or f.startswith(root + "/")]:
                self._fetch_versions(folder)
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
        uri = file.get_uri()
        path = path_of(uri)
        if not path:
            return
        self.cache.saw(path)
        if self.cache.root_of(path) is None:
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
            self._fetch_versions(folder)
        return False

    def _fetch_versions(self, folder):
        """The names of `folder` with kept copies (Spola 0.3.27 on): the menu
        then knows at once whether "Previous Versions…" has anything."""
        if self.spola_version < 2:
            return

        def ready(reply):
            if reply is not None:
                (names,) = reply.unpack()
                self.cache.versioned[folder] = set(names)
                log(f"versions {folder}: {sorted(names)}")
        self._call("GetFolderVersions", GLib.Variant("(s)", (uri_of(folder),)), ready)

    def _folder_ready(self, folder, reply):
        self.cache.in_flight.discard(folder)
        if reply is None:
            # known for a while, with nothing to show: not asked again at
            # every redraw of the folder, but once more in FOLDER_RETRY_S
            self.cache.set_folder(folder, {})
            GLib.timeout_add_seconds(FOLDER_RETRY_S, lambda: self.cache.drop_folder(folder) or False)
            log(f"folder {folder}: no answer, again in {FOLDER_RETRY_S} s")
            return
        (states,) = reply.unpack()
        log(f"folder {folder}: {len(states)} entries")
        self.cache.set_folder(folder, states)
        self._refresh([os.path.join(folder, name) for name in states] + [folder])

    def _refresh(self, paths):
        """Ask Files to call update_file_info again, only for files it has."""
        if FM is None:
            return
        for path in paths:
            info = FM.FileInfo.lookup_for_uri(uri_of(path))
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
        sync = FM.MenuItem(name=f"{prefix}::SyncNow", label=_("Sync Now with Koofr"))
        sync.connect("activate", lambda *_: self._call("SyncNow", GLib.Variant("(s)", (uri_of(target),)), lambda r: None))
        items.append(sync)
        if len(paths) == 1:
            web = FM.MenuItem(name=f"{prefix}::OpenOnKoofr", label=_("Open on Koofr"))
            web.connect("activate", lambda *_: self._call("GetWebUrl", GLib.Variant("(s)", (uri_of(target),)), self._open_url))
            items.append(web)
            link = FM.MenuItem(name=f"{prefix}::ShareLink", label=_("Share on Koofr…"))
            link.connect("activate", lambda *_: self._share(target))
            items.append(link)
            if self.spola_version >= 2:
                items += self._version_items(prefix, target, background or os.path.isdir(target))
        return items

    def _share(self, target):
        if self.spola_version >= 3:
            self._call("Share", GLib.Variant("(s)", (uri_of(target),)), lambda r: None)
        else:
            # Older link methods publish immediately. Open the pair instead
            # until the running Spola has the explicit sharing dialog.
            root = self.cache.root_of(target)
            pair_id = next((pid for path, pid in self.cache.roots if path == root), "")
            self._call("ShowWindow", GLib.Variant("(s)", (pair_id,)), lambda r: None)

    def _version_items(self, prefix, target, folder):
        """"Previous Versions…" on a file with kept copies; "Recently Deleted
        Files…" on a folder (a deleted file has no icon to click on): Spola
        also looks in Koofr's trash there, which cannot be known beforehand."""
        if folder:
            item = FM.MenuItem(name=f"{prefix}::Deleted", label=_("Recently Deleted Files…"))
            item.connect("activate", lambda *_: self._call("ShowDeleted", GLib.Variant("(s)", (uri_of(target),)),
                                                           lambda r: None))
            return [item]
        if not self.cache.has_versions(target):
            return []
        item = FM.MenuItem(name=f"{prefix}::Versions", label=_("Previous Versions…"))
        item.connect("activate", lambda *_: self._call("ShowVersions", GLib.Variant("(s)", (uri_of(target),)),
                                                       lambda r: None))
        return [item]

    # Nautilus 4: (files) and (folder); Nemo and Caja put the window first
    def get_file_items(self, *args):
        return self._menu_items(list(args[-1]), False)

    def get_background_items(self, *args):
        return self._menu_items([args[-1]], True)

    def get_name_and_desc(self):
        desc = _("Sync status of the folders synced with Koofr")     # outside the f-string: every xgettext finds it
        return [f"Spola:::{desc}"]

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
            if FILE_MANAGER == "Nautilus":          # GTK 4
                gi.require_version("Gdk", "4.0")
                from gi.repository import Gdk
                Gdk.Display.get_default().get_clipboard().set(url)
            else:                                   # Nemo and Caja are GTK 3
                gi.require_version("Gtk", "3.0")
                gi.require_version("Gdk", "3.0")
                from gi.repository import Gdk, Gtk
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(url, -1)
        except (ImportError, ValueError, AttributeError) as e:
            print(f"spola-files: the link could not be copied: {e}", file=sys.stderr)


def _(text):
    """Translations of the menu entries, from Spola's catalogue when installed."""
    import gettext
    for directory in (os.path.expanduser("~/.local/share/locale"), "/usr/local/share/locale", "/usr/share/locale"):
        if gettext.find("spola", directory):
            return gettext.translation("spola", directory).gettext(text)
    return text
