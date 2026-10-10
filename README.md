<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.daniel_g_carrasco.spola.svg" alt="Spola" width="128" height="128">
</p>

<h1 align="center">Spola</h1>

<p align="center">
  Keep your folders in sync with Koofr, on GNOME.
</p>

Spola keeps folders on your computer and folders on [Koofr](https://koofr.eu) identical, in both directions, in the background. It is a GTK 4 / libadwaita app built on [rclone](https://rclone.org)'s two-way sync (`rclone bisync`), with a tray icon and, later, Nautilus emblems.

The name comes from the weaving shuttle (*spola* in Italian) that carries the thread from one side of the loom to the other; in Italian *fare la spola* means going back and forth.

**Status: development, not released yet.** The design document (in Italian) is [docs/progetto.md](docs/progetto.md).

Spola is not affiliated with Koofr.

## Safety first

Spola never writes its own sync engine: rclone bisync does the work, and Spola puts barriers around it.

- **Preview before every big step.** The first alignment of a folder, a repair, and any run that stopped on a safety check are shown as a list of what would happen, and run only after you confirm.
- **No surprise deletions.** A run that would delete more than 10% of a side stops and waits (small folders may lose up to 5 files). Files deleted or replaced on this computer go to a local backup, kept 30 days.
- **Conflicts keep both versions** (`report.conflict1.odt`, `report.conflict2.odt`).
- **Unmounted drive, replaced folder:** the local folder must still be the one that was paired (same device and inode), otherwise the pair stops before rclone runs.
- **Folders that must never be synced** (the home folder itself, `~/.config`, `~/.var`, `~/.ssh`, system folders) are refused, and so is a folder that Koofr Desktop is still syncing.
- **Names Koofr refuses** (emoji, names that differ only in upper/lower case) are found before the first run.
- **The password stays in the keyring**, and reaches rclone only through the environment of the child process, never on a command line.

## Run from source

Needs Python 3.11 or newer, PyGObject, GTK 4, libadwaita 1.6 or newer, libsecret and rclone 1.71 or newer.

```bash
python3 tools/compile_po.py          # translations (optional)
bin/spola                            # the app
bin/spola --background               # start in the tray only
```

You sign in with your Koofr e-mail and an **app password** (Koofr account settings, *Passwords*): Koofr does not accept the main password from other apps.

## Command line

```bash
spola login
spola add ~/scratch/spola-test /spola-test
spola status
spola preview spola-test
spola align spola-test           # first alignment: preview, then asks
spola sync spola-test [--dry-run] [--force]
spola repair spola-test          # after a critical error: preview, then asks
```

## Development

```bash
python3 -m unittest discover -s tests     # all tests; the bisync ones need rclone
tools/gui_smoke.sh /tmp/spola-gui          # the window on a Broadway display, screenshot in /tmp/spola-gui
python3 tools/make_icons.py                # regenerate the icons
python3 tools/compile_po.py --update       # refresh po/spola.pot and po/*.po
```

`SPOLA_DEV_REMOTE=/some/folder` turns the Koofr side into a local folder, so every flow of the app can be tried without an account.

## License

MIT
