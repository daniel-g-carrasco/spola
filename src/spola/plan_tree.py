"""One kind of change of a preview (copied to Koofr, deleted here...) as a
tree of folders, on a page of its own: each folder with how many files
and how much it holds, open or closed at will. A list view builds only
the rows on screen and a folder's rows only when it opens, so a review of
twenty thousand files stays quick, and none is left out."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import filetree  # noqa: E402
from .i18n import count, ngettext  # noqa: E402


class _Item(GObject.Object):
    __gtype_name__ = "SpolaPlanTreeItem"

    def __init__(self, node: filetree.Node):
        super().__init__()
        self.node = node


def amount(files: int, size: int) -> str:
    """"12 files · 3.4 MB"; the size only when the preview knew it."""
    text = ngettext("{n} file", "{n} files", files).format(n=count(files))
    return f"{text} · {GLib.format_size(size)}" if size else text


def _store(nodes: list[filetree.Node]) -> Gio.ListStore:
    store = Gio.ListStore(item_type=_Item)
    for node in nodes:
        store.append(_Item(node))
    return store


class PlanTreePage(Adw.NavigationPage):
    """`activate(node)` for a click on a file (a folder opens or closes)."""

    def __init__(self, title: str, nodes: list[filetree.Node], activate: Callable[[filetree.Node], None],
                 tooltip: str = ""):
        super().__init__(title=title)
        self.activate, self.tooltip = activate, tooltip
        files, size = filetree.totals(nodes)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=amount(files, size)))
        self.model = Gtk.TreeListModel.new(_store(nodes), False, False, self._children)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        view = Gtk.ListView(model=Gtk.NoSelection(model=self.model), factory=factory,
                            single_click_activate=True, css_classes=["navigation-sidebar"])
        view.connect("activate", self._activated)
        scrolled = Gtk.ScrolledWindow(child=view, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        toolbar = Adw.ToolbarView(content=scrolled)
        toolbar.add_top_bar(header)
        self.set_child(toolbar)
        if len(nodes) == 1 and nodes[0].folder:
            self.model.get_row(0).set_expanded(True)     # one folder holds it all: open it

    @staticmethod
    def _children(item: _Item):
        node = item.node
        return _store(node.children) if node.folder and node.children else None

    @staticmethod
    def _setup(_factory, list_item: Gtk.ListItem) -> None:
        icon = Gtk.Image(accessible_role=Gtk.AccessibleRole.PRESENTATION)
        name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        note = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["dim-label", "caption"])
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        texts.append(name)
        texts.append(note)
        amount_label = Gtk.Label(xalign=1, css_classes=["dim-label", "numeric"])
        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6)
        for widget in (icon, texts, amount_label):
            box.append(widget)
        expander = Gtk.TreeExpander(child=box)
        list_item.set_child(expander)
        list_item.parts = (expander, icon, name, note, amount_label)

    def _bind(self, _factory, list_item: Gtk.ListItem) -> None:
        expander, icon, name, note, amount_label = list_item.parts
        row = list_item.get_item()
        node = row.get_item().node
        expander.set_list_row(row)
        icon.set_from_icon_name("folder-symbolic" if node.folder else "text-x-generic-symbolic")
        name.set_label(node.name)
        note.set_label(node.note)
        note.set_visible(bool(node.note))
        amount_label.set_label(amount(node.files, node.size) if node.folder
                               else (GLib.format_size(node.size) if node.size else ""))
        list_item.get_child().set_tooltip_text(node.path if node.folder else
                                               "\n".join(t for t in (node.path, node.note, self.tooltip) if t))

    def _activated(self, _view, position: int) -> None:
        row = self.model.get_row(position)
        if row is None:
            return
        node = row.get_item().node
        if node.folder:
            row.set_expanded(not row.get_expanded())
        else:
            self.activate(node)
