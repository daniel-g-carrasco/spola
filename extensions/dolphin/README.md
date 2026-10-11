# Spola's overlay icons in Dolphin

Spola gives Dolphin a *Koofr* submenu in the context menu by itself (a service menu: turn on *Show sync status in the file manager* in Spola's preferences). The overlay icons on files and folders (synced, syncing, waiting, error, conflict, left out) need this small plugin. It has to be built against the KDE Frameworks of your system, so an app, and a Flatpak even less, cannot install it for you. It talks to Spola over D-Bus exactly like the Files extension does, and with Spola not running it does nothing.

## What you need

- Fedora: `sudo dnf install cmake extra-cmake-modules gcc-c++ kf6-kio-devel qt6-qtbase-devel`
- Debian, Ubuntu: `sudo apt install cmake extra-cmake-modules g++ libkf6kio-dev qt6-base-dev`
- Arch: `sudo pacman -S cmake extra-cmake-modules kio qt6-base`

On Fedora Kinoite and other atomic systems, build inside a toolbox or distrobox of the same release and install it for your user (below): the home folder is shared.

## For your user only

```bash
cmake -S extensions/dolphin -B build-dolphin -DCMAKE_INSTALL_PREFIX="$HOME/.local"
cmake --build build-dolphin
cmake --install build-dolphin
```

`cmake --install` prints where the plugin went, for example `~/.local/lib64/plugins/kf6/overlayicon/spolaoverlayplugin.so` on Fedora. Qt has to look in that `plugins` folder; Plasma reads the scripts in `~/.config/plasma-workspace/env/` at login:

```bash
mkdir -p ~/.config/plasma-workspace/env
echo 'export QT_PLUGIN_PATH="$HOME/.local/lib64/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"' \
    > ~/.config/plasma-workspace/env/spola-dolphin.sh
```

(use the folder `cmake --install` printed, without `kf6/overlayicon`), then log out and back in.

## For every user of the computer

```bash
cmake -S extensions/dolphin -B build-dolphin -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build-dolphin
sudo cmake --install build-dolphin
```

Then restart Dolphin. After a major update of KDE Frameworks, build it again.

The plugin is tested in the container of `tools/file_managers_smoke.sh` with Dolphin 26.08 and KDE Frameworks 6.
