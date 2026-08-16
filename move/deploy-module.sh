#!/bin/bash
# Deploy the Granola Schwung overtake module (module.json + ui.js + exit-hook) to the
# Move. Re-open the Schwung menu (or rescan) to see the runner.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
HOST="${1:-move.local}"
DEST="/data/UserData/schwung/modules/overtake/granola"

ssh "root@$HOST" "mkdir -p $DEST"
# COPYFILE_DISABLE + --exclude strips macOS AppleDouble junk.
COPYFILE_DISABLE=1 tar -C "$HERE/schwung-module/granola" --exclude="._*" -czf - . \
    | ssh "root@$HOST" "tar -C $DEST -xzf -"
ssh "root@$HOST" "chmod +x $DEST/exit-hook.sh; chown -R ableton:users $DEST; ls -la $DEST"
