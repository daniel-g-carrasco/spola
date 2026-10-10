#!/usr/bin/env bash
# Start Spola on a GTK Broadway display with a private D-Bus and throw-away
# XDG folders, and save a screenshot of the window. Nothing touches the real
# session, the real configuration or Koofr.
#
#   tools/gui_smoke.sh OUT_DIR [setup-command...]
#
# With SPOLA_DEV_REMOTE set (the default here), the "Koofr" side is a local
# folder inside OUT_DIR. Extra arguments run first, in the same environment
# (e.g. "bin/spola add ..."), before the window starts.
set -euo pipefail

OUT="$(realpath -m "${1:?usage: gui_smoke.sh OUT_DIR [setup-command...]}")"
shift
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"/{config,state,data,remote,home/Sync}
# A private runtime dir too: the portals the private bus starts (documents,
# gvfs) would otherwise mount over the real session's /run/user/<uid>.
mkdir -m 700 -p "$OUT/runtime"
export XDG_RUNTIME_DIR="$OUT/runtime"
DISPLAY_NUM="${BROADWAY_NUM:-7}"

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

export OUT HOME="$OUT/home" XDG_CONFIG_HOME="$OUT/config" XDG_STATE_HOME="$OUT/state" XDG_DATA_HOME="$OUT/data"
export SPOLA_DEV_REMOTE="${SPOLA_DEV_REMOTE-$OUT/remote}"
export GDK_BACKEND=broadway BROADWAY_DISPLAY=":$DISPLAY_NUM" GSK_RENDERER=cairo
export SPOLA_SCREENSHOT="${SPOLA_SCREENSHOT:-$OUT/window.png}"
export LANGUAGE="${LANGUAGE:-it}" LANG="${LANG:-it_IT.UTF-8}"

dbus-run-session -- bash -c '
    if [ "$#" -gt 0 ]; then "$@"; fi
    exec /usr/bin/python3 "'"$ROOT"'/bin/spola"
' _ "$@"
echo "screenshot: $SPOLA_SCREENSHOT"
