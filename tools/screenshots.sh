#!/usr/bin/env bash
# Take the screenshots used by the README, the site and the AppStream
# metadata: Spola's windows and Files (Nautilus) with the emblems, in
# English, drawn at 2x, with made-up folders on a Broadway display and a
# private D-Bus (nothing real is touched; "Koofr" is a local folder).
#
#   tools/screenshots.sh            -> data/screenshots/*.png
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/data/screenshots"
WORK="${TMPDIR:-/tmp}/spola-screenshots.$$"
mkdir -p "$DEST"
export LANGUAGE=en LANG=en_US.UTF-8 SPOLA_DEV_USER=me@example.com SPOLA_SCREENSHOT_SCALE=2
export PATH=/usr/bin:/bin:/usr/sbin:/sbin       # nautilus-python wants the system python3 first
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME

# The made-up account: three folders on both sides, aligned, then a few
# changes so that each pair has something to show once Spola starts.
#   Documents: two new files (a quick upload, a summary in its row)
#   Projects:  report.odt changed on both sides (a conflict), an Office
#              lock file (excluded), a subfolder
#   Photos:    a large new file uploaded slowly (syncing, progress bar)
populate() {
    local home="$1" remote="$2"
    mkdir -p "$home"/{Documents/Invoices,Photos/2026,Projects/Drawings,Downloads,Music} "$remote"
    for f in Documents/Invoices/2026-01.pdf Documents/Invoices/2026-02.pdf Documents/contract.odt \
             Photos/2026/IMG_0412.jpg Photos/2026/IMG_0413.jpg Photos/2026/IMG_0414.jpg \
             Projects/report.odt Projects/budget-2026.ods Projects/notes.md \
             Projects/Drawings/site-plan.pdf Projects/Drawings/sections.pdf; do
        head -c $((2000 + RANDOM)) /dev/urandom > "$home/$f"
    done
    touch -d '3 hours ago' $(find "$home" -type f)
    for d in Documents Photos Projects; do cp -rp "$home/$d" "$remote/"; done
}

changes() {
    local home="$1" remote="$2"
    head -c 3000 /dev/urandom > "$home/Documents/Invoices/2026-03.pdf"
    head -c 3000 /dev/urandom > "$home/Documents/offer.odt"
    echo "edited on this computer" > "$home/Projects/report.odt"
    echo "edited on another computer, through Koofr" > "$remote/Projects/report.odt"
    echo "lock" > "$home/Projects/~\$budget-2026.ods"
    head -c 6000000 /dev/urandom > "$home/Photos/2026/panorama.tif"
}

pairs() {   # the same order as the rows: quick ones first, the slow one last
    local bin="$1" home="$2"
    for d in Documents Projects Photos; do
        /usr/bin/python3 "$bin" add "$home/$d" "/$d" >/dev/null
        echo s | /usr/bin/python3 "$bin" align "$home/$d" >/dev/null
    done
}

# --- Spola's windows, through tools/gui_smoke.sh --------------------------------
SCENE_DISPLAY=20
window_scene() {   # name, screenshot delay, extra env...
    local name="$1" delay="$2"; shift 2
    SCENE_DISPLAY=$((SCENE_DISPLAY + 1))      # a display of its own: the last one may still be closing
    local out="$WORK/$name"
    mkdir -p "$out"
    populate "$out/home" "$out/remote"
    # a shorter window than the default: no empty space under the rows
    mkdir -p "$out/state/spola"
    echo "{\"width\": 600, \"height\": ${WINDOW_HEIGHT:-520}}" > "$out/state/spola/window.json"
    env "$@" BROADWAY_NUM="$SCENE_DISPLAY" SPOLA_SCREENSHOT="$DEST/$name.png" SPOLA_SCREENSHOT_DELAY="$delay" SPOLA_DEV_BWLIMIT=60k \
        "$ROOT/tools/gui_smoke.sh" "$out" bash -c "
            $(declare -f pairs changes)
            pairs '$ROOT/bin/spola' \"\$HOME\"
            changes \"\$HOME\" '$out/remote'
        " > "$WORK/$name.log" 2>&1 || true
    echo "$DEST/$name.png"
}

if [ -z "${ONLY_FILES:-}" ]; then
    window_scene main-window 14
    window_scene conflicts 18 SPOLA_DEV_OPEN_REVIEW=1
    WINDOW_HEIGHT=1120 window_scene preferences 6 SPOLA_DEV_OPEN_PREFERENCES=1
fi

# --- Files (Nautilus) with the emblems --------------------------------------------
out="$WORK/files"
mkdir -p "$out"/{config,state,data,xdg-data/nautilus-python/extensions,runtime}
chmod 700 "$out/runtime"
populate "$out/home" "$out/remote"
cp "$ROOT/tools/nautilus_screenshot_helper.py" "$out/xdg-data/nautilus-python/extensions/zz-screenshot-helper.py"
DISPLAY_NUM=13
XDG_RUNTIME_DIR="$out/runtime" gtk4-broadwayd ":$DISPLAY_NUM" >/dev/null 2>&1 &     # its socket lives there
broadway=$!
cleanup() {
    kill "$broadway" 2>/dev/null || true
    for m in "$WORK"/*/runtime/doc "$WORK"/*/runtime/gvfs; do
        mountpoint -q "$m" 2>/dev/null && fusermount3 -uz "$m" 2>/dev/null || true
    done
    [ -n "${KEEP:-}" ] || rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT
sleep 1
env HOME="$out/home" XDG_CONFIG_HOME="$out/config" XDG_STATE_HOME="$out/state" XDG_DATA_HOME="$out/xdg-data" \
    XDG_RUNTIME_DIR="$out/runtime" SPOLA_DEV_REMOTE="$out/remote" SPOLA_DEV_BWLIMIT=40k \
    GDK_BACKEND=broadway BROADWAY_DISPLAY=":$DISPLAY_NUM" GSK_RENDERER=cairo \
    dbus-run-session -- bash -c "
        $(declare -f pairs changes)
        pairs '$ROOT/bin/spola' \"\$HOME\"
        /usr/bin/python3 -c \"import sys; sys.path.insert(0, '$ROOT/src'); from spola import nautilus_integration as n; n.install()\"
        changes \"\$HOME\" '$out/remote'
        /usr/bin/python3 '$ROOT/bin/spola' --background >/dev/null 2>&1 &
        sleep 14                      # Spola starts, syncs Documents and Projects, starts on Photos
        gsettings set org.gnome.nautilus.window-state initial-size '(980, 390)'
        gsettings set org.gnome.nautilus.preferences default-folder-viewer 'icon-view'
        gsettings set org.gnome.nautilus.icon-view default-zoom-level 'medium'
        SPOLA_NAUTILUS_SHOT='$DEST/files-folders.png' SPOLA_NAUTILUS_SHOT_DELAY=6 \
            NAUTILUS_PYTHON_DEBUG=misc timeout 30 nautilus --new-window \"\$HOME\" > '$WORK/nautilus-1.log' 2>&1 || true
        nautilus -q >/dev/null 2>&1 || true
        gsettings set org.gnome.nautilus.preferences default-folder-viewer 'list-view'
        gsettings set org.gnome.nautilus.window-state initial-size '(980, 520)'
        SPOLA_NAUTILUS_SHOT='$DEST/files-emblems.png' SPOLA_NAUTILUS_SHOT_DELAY=6 \
            NAUTILUS_PYTHON_DEBUG=misc timeout 30 nautilus --new-window \"\$HOME/Projects\" > '$WORK/nautilus-2.log' 2>&1 || true
        nautilus -q >/dev/null 2>&1 || true
        gdbus call --session --dest io.github.daniel_g_carrasco.spola --object-path /io/github/daniel_g_carrasco/spola \
            --method org.freedesktop.Application.ActivateAction quit '[]' '{}' >/dev/null 2>&1 || true
        sleep 2
    " > "$WORK/files.log" 2>&1 || true
ls "$DEST"/files-*.png
