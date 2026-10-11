#!/usr/bin/env bash
# Spola in the other file managers (Nemo, Caja, Dolphin, Thunar), for real,
# inside a container: a throw-away home with three synced folders and a local
# "Koofr", Spola running from this checkout, the file manager on a virtual X
# screen at 2x, and a screenshot. Nothing on the host is touched.
#
#   tools/file_managers_smoke.sh OUT_DIR [nautilus|nemo|caja|dolphin|thunar ...]
#
# The image (Fedora with the five file managers and their Python bindings,
# Xvfb, ImageMagick and the KDE Frameworks headers for the Dolphin plugin):
#   podman build -t localhost/spola-fmtest -f tools/file-managers.Containerfile tools
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(realpath -m "${1:?usage: file_managers_smoke.sh OUT_DIR [file manager...]}")"
shift
FMS=("${@:-nautilus nemo caja dolphin thunar}")
IMAGE="${SPOLA_FMTEST_IMAGE:-localhost/spola-fmtest}"
mkdir -p "$OUT"

for fm in ${FMS[@]}; do
    echo "== $fm"
    # seccomp and SELinux relaxed for this throw-away container: the image loaders
    # (glycin) start their own bwrap sandbox, which the default profile blocks
    podman run --rm --userns=keep-id --user "$(id -u):$(id -g)" \
        --security-opt seccomp=unconfined --security-opt label=disable \
        -v "$ROOT:/src:ro,Z" -v "$OUT:/out:Z" -e FM="$fm" "$IMAGE" bash /src/tools/file_managers_scene.sh \
        > "$OUT/$fm.log" 2>&1 || echo "   (failed, see $OUT/$fm.log)"
    ls "$OUT/$fm"-*.png 2>/dev/null || true
done
