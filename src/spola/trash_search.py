"""Progress and pause/retry controls shared by both Koofr trash dialogs."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .i18n import _, count, ngettext  # noqa: E402
from .ui import escape  # noqa: E402


class TrashSearchRow(Adw.ActionRow):
    def __init__(self, engine, updated):
        super().__init__(subtitle_lines=0)
        self.engine, self.updated = engine, updated
        self.entries = []
        self.loading = self.complete = self.closed = False
        self.error = ""
        self._stop = lambda: None
        self.spinner = Adw.Spinner()
        self.add_prefix(self.spinner)
        self.action = Gtk.Button(valign=Gtk.Align.CENTER)
        self.action.connect("clicked", lambda *_: self.pause() if self.loading else self.start())
        self.add_suffix(self.action)

    def start(self):
        self._stop()
        self.loading, self.complete, self.error = True, False, ""
        self._render()
        self._stop = self.engine.trash_entries(self._updated)

    def pause(self):
        self._stop()
        self.loading, self.error = False, ""
        self._render()

    def close(self):
        self.closed = True
        self._stop()

    def _updated(self, entries, done, error):
        if self.closed:
            return
        self.entries, self.error = entries, error
        self.loading, self.complete = not done, done and not error
        self._render()
        self.updated(entries, done, error)

    def _render(self):
        self.set_visible(not self.complete)
        self.spinner.set_visible(self.loading)
        self.action.set_label(_("Stop") if self.loading else _("Continue Searching"))
        title = (_("Reading Koofr's trash…") if self.loading else
                 _("Koofr's trash could not be read entirely") if self.error else _("Search paused"))
        self.set_title(title)
        n = len(self.entries)
        progress = ngettext("{n} file of the account read so far", "{n} files of the account read so far",
                            n).format(n=count(n))
        self.set_subtitle(escape(progress + ("\n" + self.error if self.error else "")))
