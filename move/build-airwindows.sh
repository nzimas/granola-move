#!/usr/bin/env bash
# Build the Airwindows UGen plugin for the Move (aarch64 Linux).
#
# Granola's own Scripts/build-airwindows.sh already has a Linux path — it just needs an
# aarch64 toolchain, which the Move does not have (no compiler on the device) and macOS
# does not have natively. So it runs inside an arm64 Debian container; on Apple Silicon
# that is native, not emulated.
#
# The Granola checkout is mounted READ-ONLY and the build happens in a scratch tree, so
# building for the Move can never disturb the macOS artifacts (the same script writes
# vendor/airwindows/ and would otherwise overwrite them with the Linux results).
#
# Only the .so is architecture-specific. The awfx_*.scsyndef files are portable graph
# descriptions and are copied from the macOS build, which also guarantees both platforms
# run the identical effect graph.
#
# Usage: ./build-airwindows.sh [path-to-granola-checkout]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
GR="${1:-$(cd "$ROOT/../granola" && pwd)}"
SCRATCH="${SCRATCH:-/tmp/granola-aw-build}"

[[ -d "$GR/Scripts/airwindows" ]] || { echo "error: no Granola checkout at $GR" >&2; exit 1; }
[[ -d "$GR/build/deps/airwindows/plugins/MacVST" ]] || {
    echo "error: Airwindows sources missing — run $GR/Scripts/fetch-airwindows.sh" >&2; exit 1; }
[[ -d "$GR/build/deps/supercollider/include/plugin_interface" ]] || {
    echo "error: SuperCollider headers missing under $GR/build/deps/supercollider" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "error: Docker is not running" >&2; exit 1; }

rm -rf "$SCRATCH"; mkdir -p "$SCRATCH/build"
cp -R "$GR/Scripts" "$SCRATCH/"

cat > "$SCRATCH/go.sh" <<'EOS'
set -e
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq g++ python3 >/dev/null 2>&1
mkdir -p /src/build
# The deps live in the read-only mount; symlink rather than copy 681 MB of sources.
ln -sfn /granola/build/deps /src/build/deps
cd /src
bash Scripts/build-airwindows.sh
EOS

echo "==> building in an arm64 container ($(basename "$SCRATCH"))"
docker run --rm --platform linux/arm64 \
    -v "$GR":/granola:ro -v "$SCRATCH":/src -w /src debian:bookworm-slim \
    bash /src/go.sh

mkdir -p "$ROOT/vendor/airwindows/synthdefs"
cp "$SCRATCH/vendor/airwindows/GranolaAirwindows.so" "$ROOT/vendor/airwindows/"
cp "$SCRATCH/vendor/airwindows/airwindows-manifest.json" "$ROOT/vendor/airwindows/"
cp "$GR/Resources/synthdefs/"awfx_*.scsyndef "$ROOT/vendor/airwindows/synthdefs/"

echo "==> $(file -b "$ROOT/vendor/airwindows/GranolaAirwindows.so" | cut -d, -f1-2)"
echo "==> $(python3 -c "import json;print(len(json.load(open('$ROOT/vendor/airwindows/airwindows-manifest.json'))))") effects, \
$(ls "$ROOT/vendor/airwindows/synthdefs" | wc -l | tr -d ' ') synthdefs"
