#!/usr/bin/env bash
# Load the Files (Nautilus) extension inside a real Files, on a GTK Broadway
# display with a private D-Bus, a throw-away home and a local "Koofr". Spola
# runs in the background on the same bus; the extension logs what it does to
# $OUT/extension.log (SPOLA_NAUTILUS_LOG). Nothing touches the real session.
#
#   tools/nautilus_smoke.sh OUT_DIR
set -euo pipefail

OUT="$(realpath -m "${1:?usage: nautilus_smoke.sh OUT_DIR}")"
export ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$OUT"
mkdir -p "$OUT"/{config,state,data,xdg-data,remote/Sync,home/Sync/Progetti}
# A private runtime dir too: the portals the private bus starts (documents,
# gvfs) would otherwise mount over the real session's /run/user/<uid>.
mkdir -m 700 -p "$OUT/runtime"
export XDG_RUNTIME_DIR="$OUT/runtime"
DISPLAY_NUM="${BROADWAY_NUM:-11}"

gtk4-broadwayd ":$DISPLAY_NUM" >/dev/null 2>&1 &
broadway=$!
cleanup() {
    kill "$broadway" 2>/dev/null || true
    # the portals of the private session mount FUSE inside the private runtime dir
    for m in "$OUT/runtime/doc" "$OUT/runtime/gvfs"; do
        mountpoint -q "$m" 2>/dev/null && fusermount3 -uz "$m" 2>/dev/null || true
    done
}
trap cleanup EXIT
sleep 1

echo base > "$OUT/home/Sync/a.txt"
echo base > "$OUT/home/Sync/Progetti/b.txt"
echo "~\$lock" > "$OUT/home/Sync/~\$doc.docx"
cp -rp "$OUT/home/Sync/." "$OUT/remote/Sync/"

# nautilus-python 4.1.0 builds Python's path from the first python3 in PATH:
# a virtualenv there (uv, venv) breaks every extension, so use the system's
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME
export HOME="$OUT/home" XDG_CONFIG_HOME="$OUT/config" XDG_STATE_HOME="$OUT/state" XDG_DATA_HOME="$OUT/xdg-data"
export SPOLA_DEV_REMOTE="$OUT/remote" GDK_BACKEND=broadway BROADWAY_DISPLAY=":$DISPLAY_NUM" GSK_RENDERER=cairo
export OUT_NAUTILUS_LOG="$OUT/nautilus.log" SPOLA_NAUTILUS_LOG="$OUT/extension.log" LANGUAGE="${LANGUAGE:-it}" LANG="${LANG:-it_IT.UTF-8}"

dbus-run-session -- bash -c '
    set -e
    /usr/bin/python3 "$ROOT/bin/spola" add "$HOME/Sync" /Sync >/dev/null
    echo s | /usr/bin/python3 "$ROOT/bin/spola" align "$HOME/Sync" >/dev/null
    /usr/bin/python3 -c "import sys; sys.path.insert(0, \"$ROOT/src\"); from spola import nautilus_integration as n; print(\"installed:\", len(n.install()), \"files\")"
    /usr/bin/python3 "$ROOT/bin/spola" --background &
    spola=$!
    for i in $(seq 1 50); do
        gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
            --method org.freedesktop.DBus.NameHasOwner io.github.daniel_g_carrasco.spola 2>/dev/null | grep -q true && break
        sleep 0.2
    done
    echo "changed here" > "$HOME/Sync/Progetti/nuovo.txt"
    NAUTILUS_PYTHON_DEBUG=misc timeout 40 nautilus --new-window "$HOME/Sync" > "$OUT_NAUTILUS_LOG" 2>&1 &
    sleep 5
    nautilus --new-window "$HOME" 2>/dev/null || true     # the parent: the root itself gets an emblem there
    sleep 3
    # with Files open: a change here must turn the folder "pending", then "synced" again
    echo "changed while Files is open" > "$HOME/Sync/Progetti/dopo.txt"
    sleep 20
    nautilus -q 2>/dev/null || true
    sleep 1
    gdbus call --session --dest io.github.daniel_g_carrasco.spola --object-path /io/github/daniel_g_carrasco/spola \
        --method org.freedesktop.Application.ActivateAction quit "[]" "{}" >/dev/null 2>&1 || true
    wait $spola 2>/dev/null || true
' _
echo "--- extension log:"
cat "$SPOLA_NAUTILUS_LOG" 2>/dev/null || echo "(no log: the extension did not run)"
