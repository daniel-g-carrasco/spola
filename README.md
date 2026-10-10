<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="data/icons/source/spola-darkbg.svg">
    <img src="data/icons/source/spola-lightbg.svg" alt="Spola" width="128" height="128">
  </picture>
</p>

<h1 align="center">Spola</h1>

<p align="center">
  Keep your folders in sync with Koofr, on GNOME.
</p>

Spola keeps folders on your computer and folders on [Koofr](https://koofr.eu) identical, in both directions, in the background. It is a GTK 4 / libadwaita app built on [rclone](https://rclone.org)'s two-way sync (`rclone bisync`), with a tray icon and, later, Nautilus emblems.

The name comes from the weaving shuttle (*spola* in Italian) that carries the thread from one side of the loom to the other; in Italian *fare la spola* means going back and forth.

<p align="center">
  <img src="data/screenshots/main-window.png" alt="Spola's window: three folder pairs, one with conflicts to check" width="560">
</p>

The design document (in Italian) is [docs/progetto.md](docs/progetto.md). Spola is not affiliated with Koofr.

## Install

### Flatpak

From [our own Flatpak repository](https://daniel-g-carrasco.github.io/spola/); updates arrive through GNOME Software:

```bash
flatpak install https://daniel-g-carrasco.github.io/spola/spola.flatpakref
```

The GNOME runtime comes from Flathub. The repository is signed; the key's fingerprint is `DFE7 82B9 7EA0 ED33 B6A2  C0F2 BFB2 EC99 7FDB C5B1`. A `.flatpak` bundle is attached to every [release](https://github.com/daniel-g-carrasco/spola/releases) too. The emblems in Files need `nautilus-python` on the host (Fedora: `sudo dnf install nautilus-python`; Margine OS ships it).

On [Margine OS](https://margine.dev): `ujust install-spola`, and `ujust install-spola migrate` to take over the folders of Koofr Desktop.

## Safety first

Spola never writes its own sync engine: rclone bisync does the work, and Spola puts barriers around it.

- **Preview before every big step.** The first alignment of a folder, a repair, and any run that stopped on a safety check are shown as a list of what would happen, and run only after you confirm.
- **No surprise deletions.** A run that would delete more than 10% of a side stops and waits (small folders may lose up to 5 files). Files deleted or replaced on this computer go to a local backup, kept 30 days.
- **Conflicts keep both versions** (`report.conflict1.odt`, `report.conflict2.odt`).
- **Unmounted drive, replaced folder:** the local folder must still be the one that was paired (same device and inode), otherwise the pair stops before rclone runs.
- **Folders that must never be synced** (the home folder itself, `~/.config`, `~/.var`, `~/.ssh`, system folders) are refused, and so is a folder that Koofr Desktop is still syncing.
- **Names Koofr refuses** (emoji, names that differ only in upper/lower case) are found before the first run.
- **The password stays in the keyring**, and reaches rclone only through the environment of the child process, never on a command line.

## In the window

Each folder pair is one row: its status, what the last sync did, a progress bar while it runs. A pair that needs you (a preview to confirm, conflicts, files Koofr refuses) says so and opens the right view with one click, as does its notification. Conflicts are resolved from there: keep this computer's version, Koofr's, or both under readable names; the discarded one goes to the wastebasket. Each pair has its own exclusions (rclone filter syntax, seeded with sensible defaults). Preferences: pause on metered networks and in power saver mode, start at login, backup retention, language of the conflict copies' names.

## In Files (Nautilus)

Preferences has a switch, *Show sync status in Files*: Spola installs a small nautilus-python extension and its emblems for your user (`~/.local/share/nautilus-python/extensions/spola-nautilus.py`, `~/.local/share/icons/hicolor/scalable/emblems/`), and Files shows an emblem on every synced file and folder: synced, syncing, changed here and waiting, error, conflict, left out. Right-click gives *Sync Now with Koofr*, *Open on Koofr* and *Copy Koofr Share Link*. The extension only ever reads from a cache and asks Spola, over D-Bus, once per folder, so Files stays as fast as it is; with Spola not running it does nothing. Files needs nautilus-python (Fedora: `nautilus-python`) and a restart (`nautilus -q`) after the switch.

The D-Bus interface (`io.github.daniel_g_carrasco.spola.Sync1` at `/io/github/daniel_g_carrasco/spola/Sync`) is also there for other file managers: states per folder, states per file, sync now, web link, share link, and signals when states change.

### From source

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

`SPOLA_DEV_REMOTE=/some/folder` turns the Koofr side into a local folder, so every flow of the app and of the command line can be tried without an account; `SPOLA_DEV_BWLIMIT=300k` slows runs down to watch them.

## License

MIT
