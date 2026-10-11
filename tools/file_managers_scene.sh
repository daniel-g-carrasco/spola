#!/usr/bin/env bash
# One file manager with Spola, inside the test container (see
# tools/file_managers_smoke.sh): FM=nautilus|nemo|caja|dolphin|thunar. Writes
# /out/$FM-folders.png (the home folder, icon view) and /out/$FM-files.png
# (inside Projects), or /out/thunar-menu.png for Thunar (menu only).
set -uo pipefail
export HOME=/home/me XDG_RUNTIME_DIR=/tmp/rt
mkdir -p "$HOME" && mkdir -m 700 -p "$XDG_RUNTIME_DIR"
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share" XDG_STATE_HOME="$HOME/.local/state"
export LANGUAGE="${LANGUAGE:-en}" LANG=C.UTF-8
export SPOLA_DEV_REMOTE=/tmp/remote SPOLA_DEV_USER=me@example.com SPOLA_DEV_BWLIMIT=40k
export SPOLA_NAUTILUS_LOG="/out/$FM-extension.log"
rm -f "$SPOLA_NAUTILUS_LOG" /out/"$FM"-*.png
SPOLA="python3 /src/bin/spola"

# the made-up account of tools/screenshots.sh, in short
mkdir -p "$HOME"/{Documents/Invoices,Photos/2026,Projects/Drawings,Downloads,Music} "$SPOLA_DEV_REMOTE"
for f in Documents/Invoices/2026-01.pdf Documents/contract.odt Photos/2026/IMG_0412.jpg \
         Projects/report.odt Projects/budget-2026.ods Projects/notes.md Projects/Drawings/site-plan.pdf; do
    head -c $((2000 + RANDOM)) /dev/urandom > "$HOME/$f"
done
touch -d '3 hours ago' $(find "$HOME" -type f)
for d in Documents Photos Projects; do cp -rp "$HOME/$d" "$SPOLA_DEV_REMOTE/"; done

Xvfb :5 -screen 0 2400x1400x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
export DISPLAY=:5 GDK_BACKEND=x11

dbus-run-session -- bash -c '
    set -u
    for d in Documents Projects Photos; do
        '"$SPOLA"' add "$HOME/$d" "/$d" >/dev/null
        echo s | '"$SPOLA"' align "$HOME/$d" >/dev/null
    done
    # the file manager was used before: its folders are there (Thunar writes uca.xml then)
    case "$FM" in
        nemo) mkdir -p "$HOME/.config/nemo" ;;
        caja) mkdir -p "$HOME/.config/caja" ;;
        dolphin) echo "[General]" > "$HOME/.config/dolphinrc"
                 # the overlay plugin, built and installed for this user as its README says
                 cmake -S /src/extensions/dolphin -B /tmp/build -DCMAKE_INSTALL_PREFIX="$HOME/.local" >/out/dolphin-cmake.log 2>&1 \
                   && cmake --build /tmp/build >>/out/dolphin-cmake.log 2>&1 \
                   && cmake --install /tmp/build >>/out/dolphin-cmake.log 2>&1
                 # <plugin dir>/kf6/overlayicon/spolaoverlayplugin.so
                 export QT_PLUGIN_PATH="$(dirname "$(dirname "$(dirname "$(find "$HOME/.local" -name spolaoverlayplugin.so)")")")"
                 echo "plugin path: $QT_PLUGIN_PATH" ;;
        thunar) timeout 5 thunar "$HOME" >/dev/null 2>&1 || true ;;
    esac
    python3 -c "import sys; sys.path.insert(0, \"/src/src\"); from spola import file_managers as f; print(\"installed:\", [str(p) for p in f.install() if \"emblem\" not in str(p) and \"locale\" not in str(p)])"
    # changes for Spola to find: a conflict, an Office lock file, a large file uploaded slowly
    echo "edited here" > "$HOME/Projects/report.odt"
    echo "edited elsewhere" > "$SPOLA_DEV_REMOTE/Projects/report.odt"
    echo lock > "$HOME/Projects/~\$budget-2026.ods"
    head -c 4000000 /dev/urandom > "$HOME/Photos/2026/panorama.tif"
    '"$SPOLA"' --background > /out/'"$FM"'-spola.log 2>&1 &
    sleep 12
    # the screen is black around the windows: a black border, then trim, leaves the window
    shot() { import -window root png:- 2>/dev/null | magick - -bordercolor black -border 1 -trim +repage "/out/$1.png"; }
    case "$FM" in
        nautilus)
            GDK_SCALE=2 nautilus --new-window "$HOME" >/out/nautilus-fm.log 2>&1 & sleep 8; shot nautilus-folders
            nautilus -q 2>/dev/null; sleep 1
            GDK_SCALE=2 nautilus --new-window "$HOME/Projects" >>/out/nautilus-fm.log 2>&1 & sleep 8; shot nautilus-files ;;
        nemo)
            GDK_SCALE=2 nemo --no-desktop "$HOME" >/out/nemo-fm.log 2>&1 & sleep 8; shot nemo-folders
            nemo -q 2>/dev/null; sleep 1
            GDK_SCALE=2 nemo --no-desktop "$HOME/Projects" >>/out/nemo-fm.log 2>&1 & sleep 8; shot nemo-files ;;
        caja)
            GDK_SCALE=2 caja --no-desktop "$HOME" >/out/caja-fm.log 2>&1 & sleep 8; shot caja-folders
            caja -q 2>/dev/null; sleep 1
            GDK_SCALE=2 caja --no-desktop "$HOME/Projects" >>/out/caja-fm.log 2>&1 & sleep 8; shot caja-files ;;
        dolphin)
            QT_SCALE_FACTOR=2 dolphin --new-window "$HOME" >/out/dolphin-fm.log 2>&1 & sleep 10; shot dolphin-folders
            pkill -x dolphin; sleep 1
            QT_SCALE_FACTOR=2 dolphin --new-window "$HOME/Projects" >>/out/dolphin-fm.log 2>&1 & sleep 10; shot dolphin-files
            # the context menu of the first item, with the Koofr submenu
            xdotool mousemove 476 168 click 3; sleep 2; shot dolphin-menu
            # the commands of the service menu, as Dolphin runs them (%f = a plain path)
            for action in SyncNow OpenOnKoofr ShowShareLink; do
                cmd="$(grep "^Exec=.*\.$action " "$HOME/.local/share/kio/servicemenus/"*.desktop | sed "s/^Exec=//")"
                ${cmd%\%f} "$HOME/Projects/notes.md" >/dev/null 2>&1
                echo "$action from the menu: exit $?" >> /out/dolphin-actions.log
            done
            ${cmd%\%f} "/tmp/not-synced.txt" >/dev/null 2>&1; echo "outside: exit $?" >> /out/dolphin-actions.log
            sleep 2 ;;
        thunar)
            GDK_SCALE=2 thunar "$HOME" >/out/thunar-fm.log 2>&1 & sleep 8; shot thunar-folders
            xdotool mousemove 476 216 click 3; sleep 2; shot thunar-menu ;;
    esac
    cp "$HOME/.config/Thunar/uca.xml" /out/thunar-uca.xml 2>/dev/null || true
    cp "$HOME/.local/share/kio/servicemenus/"*.desktop /out/dolphin-servicemenu.desktop 2>/dev/null || true
'
