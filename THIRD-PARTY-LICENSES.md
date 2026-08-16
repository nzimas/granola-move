# Third-party licences

**Granola for Move**'s own code is MIT — see [`LICENSE`](LICENSE). Every third-party
component keeps its own licence, listed below. Full texts are in [`licenses/`](licenses/).

Granola for Move deliberately **ships no SuperCollider bundle**. It reuses the one already
installed on the device by another takeover, so `scsynth`, `sclang`, `libsndfile` and the
sc3-plugins UGens are *not* redistributed by this repository — they arrive with whichever
bundle you already have. They are still listed here, because they are what the instrument
runs on and their obligations apply to the device as a whole.

---

## Bundled in this repository

### Airwindows — the performance FX

- **Licence:** MIT — [`licenses/MIT-Airwindows.txt`](licenses/MIT-Airwindows.txt)
- **Author:** Chris Johnson
- **Upstream:** <https://github.com/airwindows/airwindows>
- **Here:** `vendor/airwindows/` — `GranolaAirwindows.so` (aarch64 Linux, 154 effects),
  a manifest, measured per-effect CPU costs, and 154 `.scsyndef` graph descriptions.
- **Modifications:** the DSP sources are compiled **unmodified**. The wrapper that presents
  each effect as a SuperCollider UGen comes from the macOS Granola
  (`Scripts/airwindows/`), including a shim standing in for the VST SDK headers, so no
  proprietary SDK is needed. `move/build-airwindows.sh` reproduces the build.

### yt-dlp — the harvester's route to YouTube

- **Licence:** The Unlicense (public domain dedication) —
  [`licenses/Unlicense.txt`](licenses/Unlicense.txt)
- **Upstream:** <https://github.com/yt-dlp/yt-dlp>
- **Here:** `controller/vendor/yt_dlp/` (version 2026.07.04), vendored whole because it is
  pure Python and the Move has no package manager, no JVM for NewPipeExtractor, and no
  ffmpeg.
- **Modifications:** two unused site extractors are **removed** — `shahid.py` and
  `scrippsnetworks.py`, with their entries in `_extractors.py` and `lazy_extractors.py`.
  Both hard-code a public API key from the site they scrape, which GitHub's secret scanner
  flags as an AWS credential and refuses to accept on push. Neither is reachable from this
  project: the harvester only ever talks to YouTube. Nothing else is changed, and the
  licence is declared in yt-dlp's own source as `__license__ = 'The Unlicense'`.

### python-osc — OSC transport

- **Licence:** The Unlicense (public domain dedication) —
  [`licenses/Unlicense.txt`](licenses/Unlicense.txt)
- **Upstream:** <https://github.com/attwad/python-osc>
- **Here:** `controller/vendor/pythonosc/`
- **Modifications:** none. The vendored copy does not carry the upstream licence file; the
  licence is as published upstream.

---

## Required on the device, not shipped here

### SuperCollider — `sclang`, `scsynth`, UGen plugins

- **Licence:** GNU General Public License v3 or later —
  [`licenses/GPL-3.0.txt`](licenses/GPL-3.0.txt)
- **Upstream:** <https://github.com/supercollider/supercollider>
- **Modifications:** none by this project.

`scsynth` and `sclang` run as **separate processes**, driven over UDP with Open Sound
Control. Nothing in this repository links against them.

### sc3-plugins — `JPverb` and the wider UGen set

- **Licence:** GNU General Public License (version varies by plugin: GPL-2.0-or-later or
  GPL-3.0 — see the individual plugin directories upstream)
- **Upstream:** <https://github.com/supercollider/sc3-plugins>
- **Modifications:** none by this project. Granola's master reverb is `JPverb`.

### libsndfile — audio file decoding

- **Licence:** GNU Lesser General Public License v2.1 or later —
  [`licenses/LGPL-2.1.txt`](licenses/LGPL-2.1.txt)
- **Upstream:** <https://github.com/libsndfile/libsndfile>
- **Modifications:** none by this project. Used through `scsynth` and `sclang`; the
  harvester's format choices are dictated by what the on-device build supports
  (Ogg Vorbis/Opus and FLAC, but not MP3 or AAC).

### JACK — the audio server

- **Licence:** `libjack` is LGPL-2.1-or-later; the `jackd` server is GPL-2.0-or-later
- **Upstream:** <https://github.com/jackaudio/jack2>
- **Modifications:** none by this project.

### Schwung / move-anything — the host framework

- **What it is:** the overtake host that loads `ui.js`, plus the shared constants and input
  filter it imports.
- **Licence:** as published by its author. This repository contains none of its code; it
  imports from `/data/UserData/move-anything/shared/` at runtime on the device.

### Ableton Move — the hardware and its firmware

- Ableton's own terms. Nothing from the Move firmware is included, modified or
  redistributed here. Granola runs as a guest process alongside the stock software.

---

## If you redistribute

The GPL and LGPL obligations travel with the binaries they cover — which, for this
repository, is none of them: the only third-party binary here is Airwindows, and that is
MIT. If you redistribute a **device image** containing SuperCollider, sc3-plugins,
libsndfile or JACK, keep this notice and the texts in [`licenses/`](licenses/) with it, and
be able to point recipients at the corresponding source. The upstream URLs above are
sufficient, since nothing is modified.
