#!/usr/bin/env bash
# Take the screenshots used by the README, the site and the AppStream
# metadata, with fake data on a Broadway display (nothing real is touched):
#
#   tools/screenshots.sh            -> data/screenshots/*.png
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${TMPDIR:-/tmp}/spola-screenshots.$$"
DEST="$ROOT/data/screenshots"
mkdir -p "$DEST"
trap 'rm -rf "$OUT"' EXIT

scene() {   # name, screenshot delay, extra env...
    local name="$1" delay="$2"; shift 2
    rm -rf "$OUT" 2>/dev/null || true
    mkdir -p "$OUT/home/Documenti/Fatture" "$OUT/home/Foto/2026" "$OUT/home/Progetti/Relazione" "$OUT/remote"
    for f in "Documenti/Fatture/2026-01.pdf" "Documenti/Fatture/2026-02.pdf" "Documenti/contratto.odt" \
             "Foto/2026/IMG_0412.jpg" "Foto/2026/IMG_0413.jpg" "Progetti/Relazione/relazione città.odt" "Progetti/note.txt"; do
        echo "contenuto" > "$OUT/home/$f"
    done
    touch -d '2 hours ago' "$OUT"/home/*/* "$OUT"/home/*/*/* 2>/dev/null || true
    cp -rp "$OUT/home/Documenti" "$OUT/home/Foto" "$OUT/home/Progetti" "$OUT/remote/"
    env "$@" SPOLA_SCREENSHOT="$DEST/$name.png" SPOLA_SCREENSHOT_DELAY="$delay" LANGUAGE=it SPOLA_DEV_USER="nome@esempio.it" \
        "$ROOT/tools/gui_smoke.sh" "$OUT" bash -c '
            for d in Progetti Documenti Foto; do
                /usr/bin/python3 "'"$ROOT"'/bin/spola" add "$HOME/$d" "/$d" >/dev/null
                echo s | /usr/bin/python3 "'"$ROOT"'/bin/spola" align "$HOME/$d" >/dev/null
            done
            sleep 1
            # a conflict in Progetti: the same file changed here and "on Koofr"
            echo "versione di questo computer" > "$HOME/Progetti/Relazione/relazione città.odt"
            echo "versione cambiata su Koofr, più lunga" > "$OUT/remote/Progetti/Relazione/relazione città.odt"
        ' >/dev/null 2>&1
    echo "$DEST/$name.png"
}

scene main-window 14
scene conflicts 14 SPOLA_DEV_OPEN_REVIEW=11
scene preferences 6 SPOLA_DEV_OPEN_PREFERENCES=1
