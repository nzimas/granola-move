#!/bin/bash
# Deploy the Granola headless controller + SC engine + launch scripts to the Move.
#   controller/granola        -> /data/UserData/granola/controller/granola
#   controller/vendor/pythonosc -> .../controller/vendor/pythonosc
#   supercollider/*.scd + move/sc/gr-boot.scd -> .../sc
#   move/run-*.sh, stop-stack.sh -> /data/UserData/granola
#   samples/*                 -> .../samples   (whatever you dropped in the repo)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HOST="${1:-move.local}"
DEST="/data/UserData/granola"

ssh "root@$HOST" "mkdir -p $DEST/controller/vendor $DEST/sc $DEST/logs $DEST/ipc $DEST/samples $DEST/state $DEST/projects $DEST/plugins $DEST/synthdefs $DEST/harvest-tmp"

echo "-> controller (granola + pythonosc)"
COPYFILE_DISABLE=1 tar -C "$ROOT/controller" --exclude="._*" --exclude="__pycache__" -czf - granola \
    | ssh "root@$HOST" "tar -C $DEST/controller -xzf -"
COPYFILE_DISABLE=1 tar -C "$ROOT/controller/vendor" --exclude="._*" --exclude="__pycache__" -czf - pythonosc \
    | ssh "root@$HOST" "tar -C $DEST/controller/vendor -xzf -"
# yt-dlp, vendored whole (pure Python, no compiled deps). The harvester's only route to
# YouTube: the Move has no JVM for NewPipeExtractor and no ffmpeg.
if [ -d "$ROOT/controller/vendor/yt_dlp" ]; then
    echo "-> yt-dlp ($(du -sh "$ROOT/controller/vendor/yt_dlp" | cut -f1))"
    COPYFILE_DISABLE=1 tar -C "$ROOT/controller/vendor" --exclude="._*" --exclude="__pycache__" -czf - yt_dlp \
        | ssh "root@$HOST" "tar -C $DEST/controller/vendor -xzf -"
fi

# SYNTAX GATE. A .scd with a syntax error does not fail loudly on the device: `.load`
# prints the error and returns normally, so the engine simply stops constructing itself and
# every symptom shows up somewhere else — a silent boot, no scsynth, a stack that looks up
# but never becomes ready. That cost a device reboot to diagnose once. Never ship unparsed.
#
# JPverb comes from sc3-plugins and is not installed on the build host, and an unresolved
# class name is itself a compile error, so the synthdefs are checked against a substituted
# copy. Only the class name changes; the file's syntax is what is being checked.
SCLANG="${SCLANG:-/Applications/SuperCollider.app/Contents/MacOS/sclang}"
if [ -x "$SCLANG" ]; then
    echo "-> syntax check"
    SDTMP="$(mktemp -t gr-sd).scd"
    sed 's/JPverb\.ar/FreeVerb2.ar/' "$ROOT/supercollider/granola-synthdefs.scd" > "$SDTMP"
    OUT="$("$SCLANG" "$ROOT/tools/sc-syntax-check.scd" \
            "$ROOT/supercollider/granola-engine.scd" "$SDTMP" 2>&1)"
    rm -f "$SDTMP"
    if ! echo "$OUT" | grep -q "SYNTAX SUMMARY 0 bad"; then
        echo "$OUT" | grep -E "SYNTAX FAIL|syntax error"
        echo "DEPLOY ABORTED: fix the syntax error above." >&2
        exit 1
    fi
    echo "   all .scd parse"
else
    echo "!! sclang not found at $SCLANG — SKIPPING the syntax check" >&2
fi

echo "-> SC engine (.scd)"
scp -q "$ROOT/supercollider/granola-engine.scd" "$ROOT/supercollider/granola-synthdefs.scd" \
       "$HERE/sc/gr-boot.scd" "root@$HOST:$DEST/sc/"

# Airwindows: the aarch64 UGen plugin, the 154 compiled effect SynthDefs and the
# manifest the controller randomises over. Built by move/build-airwindows.sh.
if [ -f "$ROOT/vendor/airwindows/GranolaAirwindows.so" ]; then
    echo "-> airwindows (UGen plugin + $(ls "$ROOT/vendor/airwindows/synthdefs" | wc -l | tr -d ' ') synthdefs)"
    scp -q "$ROOT/vendor/airwindows/GranolaAirwindows.so" "root@$HOST:$DEST/plugins/"
    scp -q "$ROOT/vendor/airwindows/airwindows-manifest.json" "root@$HOST:$DEST/"
    scp -q "$ROOT/vendor/airwindows/fx-cost.json" "root@$HOST:$DEST/"
    COPYFILE_DISABLE=1 tar -C "$ROOT/vendor/airwindows/synthdefs" --exclude="._*" -czf - . \
        | ssh "root@$HOST" "tar -C $DEST/synthdefs -xzf -"
else
    echo "-> airwindows MISSING — run move/build-airwindows.sh (the FX view will be empty)"
fi

echo "-> launch scripts"
scp -q "$HERE/run-engine.sh" "$HERE/run-controller.sh" "$HERE/run-stack.sh" "$HERE/stop-stack.sh" \
       "root@$HOST:$DEST/"

# Samples are optional: the controller also scans the Move's own library and the
# sibling takeovers' audio, and falls back to cycling whatever it finds.
if compgen -G "$ROOT/samples/*" > /dev/null; then
    echo "-> samples"
    COPYFILE_DISABLE=1 tar -C "$ROOT/samples" --exclude="._*" --exclude=".DS_Store" -czf - . \
        | ssh "root@$HOST" "tar -C $DEST/samples -xzf -"
fi

# Chown ONLY what this script ships. A blanket `chown -R` over $DEST would clear the
# file capabilities on any SC bundle later placed there, silently dropping the audio
# chain out of realtime with nothing in the output to say so.
ssh "root@$HOST" "
  chmod +x $DEST/run-*.sh $DEST/stop-stack.sh
  chown ableton:users $DEST/run-*.sh $DEST/stop-stack.sh
  # projects/, samples/, ipc/ and state/ are WRITTEN AT RUNTIME, so their ownership is
  # not cosmetic: whichever user the Schwung host runs the controller as has to be able to
  # create files in them, or saving a project fails with nothing on screen to say why.
  chown -R ableton:users $DEST/controller $DEST/sc $DEST/samples $DEST/ipc $DEST/state $DEST/logs $DEST/projects $DEST/plugins $DEST/synthdefs $DEST/harvest-tmp
  # ...and the ROOT directory itself. Under Schwung the controller runs as uid 1000
  # (ableton), not root, so anything it has to CREATE at runtime — the harvester's scratch
  # dir — needs a writable parent. Every test here ran the stack as root over ssh, which
  # has more privilege than the real runtime and hid this completely: the harvest thread
  # died on mkdir with nothing on screen to say so.
  chown ableton:users $DEST
"
echo "Done."
