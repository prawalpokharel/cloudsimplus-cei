#!/bin/sh
# Runs every sweep on Modal with the jar built as tag v1, writing results/ and the app IDs.
set -e
cd "$(dirname "$0")/.."
MODAL="${MODAL_BIN:-modal}"   # set MODAL_PROFILE yourself to pick a Modal workspace
for name in ${SWEEPS:-main replicas frontier scale}; do
    "$MODAL" run experiments/modal_app.py::sweep --config "experiments/sweep_${name}.json" \
        --out "results/sweep_${name}.csv" --tag "${TAG:-v1}" > "evidence/sweep_${name}_stdout.txt" 2>&1
    app=$(grep -oE "ap-[A-Za-z0-9]{10,}" "evidence/sweep_${name}_stdout.txt" | head -1)
    echo "$(date -u +%FT%TZ) sweep_${name} ${app}" >> evidence/modal_apps.txt
    echo "done ${name} ${app}"
done
