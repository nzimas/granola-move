# Airwindows for the Move

`GranolaAirwindows.so` — the 154-effect Airwindows repertoire as a single SuperCollider
UGen plugin, built for **aarch64 Linux** (the Move) from the *unmodified* Airwindows
sources via Granola's own `Scripts/build-airwindows.sh`, which already had a Linux path.
The DSP, and therefore the sound, is exactly as published; only the wrapper differs — a
UGen instead of a VST.

Rebuild it with `move/build-airwindows.sh`, which drives that same script inside an
arm64 Debian container with the Granola checkout mounted **read-only**, so a Move build
can never disturb the macOS one.

`synthdefs/awfx_*.scsyndef` are copied from the macOS build unchanged. A compiled
SynthDef is a portable graph description — nothing in it is architecture-specific — so
only the UGen plugin ever needed rebuilding. That also guarantees both platforms run the
identical effect graph, gain-matching and LFO wiring.

`airwindows-manifest.json` is the repertoire the controller randomises over: each effect's
name, parameter count, parameter names and defaults.
