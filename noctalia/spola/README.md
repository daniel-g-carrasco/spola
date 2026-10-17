# Spola for Noctalia

A plugin for [Noctalia](https://noctalia.dev) 5 that shows what Spola is
doing: a cloud in the bar (lit while a sync runs, a warning colour when a
folder waits for you), a panel with every folder, its progress and the
buttons to sync it now or open it in Spola, a control-centre tile that
pauses and resumes everything, and launcher entries (`/spola`, or words
like "spola" and "koofr" in the global search).

How it works: Spola writes `status.json` in its state folder at every
change and every minute; the plugin's service reads it when its date
changes (no process is started) and runs the actions through Spola's
D-Bus interface with `gdbus` (from the glib2 package). With the plugin in
the bar, Spola's own icon in the notification area can be switched off
in its preferences.

## Install

Copy this folder to `~/.local/share/noctalia/plugins/spola/`, then add the
widget to a bar and, if you like, the tile to the control centre, in a
file of your Noctalia configuration (`~/.config/noctalia/60-spola.toml`):

```toml
[widget.spola]
type = "daniel-g-carrasco/spola:indicator"

[bar.default]
end = ["tray", "spola", "network", "volume", "battery", "notifications"]

[[control_center.shortcuts]]
type = "daniel-g-carrasco/spola:tile"
```

Noctalia loads the plugin and the configuration on its own; `noctalia msg
plugins list` shows it, `noctalia plugins lint ~/.local/share/noctalia/plugins/spola`
checks it.
