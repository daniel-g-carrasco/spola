"""What tests/test_strati.py must know of Spola, the one part of the
layers' check that is the app's own (no fingerprint). test_strati.py is the
model's file, the same in every app, and reads these names. The shared
modules are not listed here: they are the manifest's (impronte/comune.json).
"""

TOOLKIT = ("gi",)

# The parts of the toolkit's library that show nothing, which app/ may
# import (and the bare package with them). Spola's services live on them
# (Daniel, 10/11/2026): the engine on GLib's main loop and GObject's signals;
# the file managers' service, the Background portal and logind over Gio's
# D-Bus; Gio's file monitors; libsecret for the app password. Rewriting the
# engine that decides about the user's files to be rid of them would buy
# nothing. Never Gtk, Adw or Gdk outside gui/, never any of it in core/ or
# in a shared module.
WINDOWLESS: tuple[str, ...] = ("gi.repository.GLib", "gi.repository.Gio", "gi.repository.GObject",
                               "gi.repository.Secret")

# every file of app/ may use them
WINDOWLESS_FILES: tuple[str, ...] = ()

# "file -> what it imports": "why it is still there, and when it goes".
# Two are left, one call each, where what shows nothing is still not
# allowed: core/ and the command line. The list can only get shorter.
DEBTS: dict[str, str] = {
    "core/progress.py -> gi": "GLib.format_size for the sizes: a function of core's own, with the same words",
    "cli.py -> gi": "one D-Bus call, inside a function, to know whether the app is running: a service in app/",
}

# gui modules that do not import the toolkit, "file": "why it stays in gui".
# Frozen: none may be added (plain logic belongs in core, data or app).
GUI_WITHOUT_TOOLKIT: dict[str, str] = {}

# gui modules that are values and nothing else: none in Spola.
GUI_VALUES: set[str] = set()
