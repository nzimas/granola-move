#!/bin/bash
# One-shot deploy of everything Granola needs on the Move.
#   1. controller + SC engine + launch scripts   — deploy-controller.sh
#   2. Schwung overtake module (ui.js)           — deploy-module.sh
# Granola ships no SuperCollider bundle: run-engine.sh reuses one already on the device
# (PoundHard's, or any sibling takeover's). See README.
# Usage: ./deploy.sh [move-host]   (default: move.local)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-move.local}"

echo "=== 1/2 controller + engine ==="
"$HERE/deploy-controller.sh" "$HOST"
echo "=== 2/2 Schwung module ==="
"$HERE/deploy-module.sh" "$HOST"
echo
echo "All deployed. On the Move: open Schwung -> overtake -> Granola."
