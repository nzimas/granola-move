# Granola for Move

**An eight-track granular synthesiser for the Ableton Move**, with a sample harvester that
goes out and finds its own material.

Granola for Move is a [Schwung](https://github.com/) *overtake* — it takes over the Move's
hardware while it runs, then hands it back. The DSP is a port of the macOS
[Granola](https://github.com/nzimas/granola): same engine, same parameter semantics, same
ranges and defaults. What is redesigned is everything above the DSP, because eight tracks
of granular synthesis have to fit on eight encoders, thirty-two pads and sixteen step
buttons.

**Version 20260816.**

---

## What makes it different

**It finds its own samples.** Hold Shift and Track 4 and the harvester goes out to YouTube,
pulls back a batch of excerpts, and puts them on pads ready to drop onto tracks. It is a
port of [SampleHarvester](https://github.com/nzimas/sampleharvester)'s
discovery layer — the same query generator, the same term filters, the same qualified-random
selection — running entirely on the device with no ffmpeg, no JVM and no package manager.
See [The sample harvester](#the-sample-harvester), including
[what you harvest is your responsibility](#what-you-harvest-is-your-responsibility).

**The playhead is an instrument.** The sixteen step buttons are a position bar for the
focused track *and* you can play them: tap one and the head cuts there, instantly, with no
glide. Hold **Rec** while you tap and the moves are recorded; release and they loop. Each
track keeps its own loop.

**154 Airwindows effects, properly ported.** Chris Johnson's effects, compiled unmodified
into SuperCollider UGens for aarch64, assembled into random chains by four slots — with a
measured CPU budget so a chain cannot overrun the device, and a **lock** so a chain you like
survives being switched off and on.

**Projects save the whole machine.** Thirty-two slots, each holding every parameter, every
sample, the transport, the exact FX chains, and the position of every playhead.

**Its own sample library.** Nothing is shared with other takeovers; the library lives in
`/data/UserData/granola/samples` and only holds what you put there.

**A web interface** on port 7135 for loading samples, managing projects and configuring the
harvester, laid out to mirror the hardware.

---

## Quick start

You will need:

- An **Ableton Move** with SSH access as `root` (this is the standard rooted-Move setup that
  Schwung requires).
- **Schwung** installed, with `move-anything` shared modules at
  `/data/UserData/move-anything/shared/`.
- **An existing SuperCollider bundle on the device.** Granola ships none. It reuses one
  already installed by another takeover — PoundHard, One Man Show, Wild Rider or Interwoven
  — because those bundles carry `sclang`, `scsynth` and the sc3-plugins UGens (Granola's
  reverb is `JPverb`). Any of them will do.
- A Mac or Linux machine with `ssh`, `scp` and `bash` to deploy from.

Then:

```bash
git clone https://github.com/nzimas/granola-move.git
cd granola-move
./move/deploy.sh move.local
```

On the Move: **Schwung → overtake → Granola**.

Open `http://move.local:7135` in a browser, drop a few audio files in, and assign them to
tracks. Press **Play**. Turn an encoder.

---

## Installation in detail

### 1. Confirm the prerequisites on the device

```bash
ssh root@move.local 'ls -d /data/UserData/schwung /data/UserData/move-anything/shared'
ssh root@move.local 'ls /data/UserData/*/bin/sclang'
```

The second command must print at least one path. That directory is the SuperCollider bundle
Granola will borrow. If it prints nothing, install a takeover that ships one (PoundHard is
the reference) before continuing.

### 2. Deploy

```bash
./move/deploy.sh move.local          # host defaults to move.local
```

This runs two steps, which you can also run separately:

| script | what it installs | where |
|---|---|---|
| `move/deploy-controller.sh` | Python controller, vendored `pythonosc` + `yt_dlp`, SC engine sources, Airwindows UGen + 154 synthdefs, launch scripts | `/data/UserData/granola/` |
| `move/deploy-module.sh` | the Schwung overtake module (`module.json`, `ui.js`, exit hook) | `/data/UserData/schwung/modules/overtake/granola/` |

The controller deploy runs a **syntax gate** over the SuperCollider sources first and
aborts rather than shipping a file that will not parse. It needs `sclang` on the deploying
machine; set `SCLANG=/path/to/sclang` if it is not at the macOS default. Without it the
gate is skipped with a warning.

It also creates the runtime directories and **chowns them to `ableton:users`**, because
Schwung launches the controller as uid 1000, not root.

### 3. Launch

Open Schwung on the Move and pick **Granola** from the overtake list. The module starts the
stack itself (`/data/UserData/granola/run-stack.sh`): shadow JACK, `sclang` with the
engine, and the Python controller.

First launch takes about 30 seconds — SuperCollider compiles its class library. The screen
says so.

### 4. Verify

```bash
ssh root@move.local 'tail -3 /data/UserData/granola/logs/controller.log'
```

Expect `controller up` and `engine ready`. The web UI at `http://move.local:7135` should
load and show eight empty tracks.

### Updating

Re-run `./move/deploy.sh`. To pick up changed SuperCollider or controller code you must
restart the stack — leave and re-enter the module, or:

```bash
ssh root@move.local 'sh /data/UserData/granola/stop-stack.sh'
```

### Uninstalling

```bash
ssh root@move.local 'sh /data/UserData/granola/stop-stack.sh; \
  rm -rf /data/UserData/granola /data/UserData/schwung/modules/overtake/granola'
```

Your samples and projects live under `/data/UserData/granola`, so that removes them too.
Copy them off first if you want to keep them.

---

## Cheat sheet

### Global

| gesture | what it does |
|---|---|
| **Play** | start / stop the granulators |
| **Shift + Play** | **panic** — free all voices, stop auditions, rebuild the master FX |
| **Back** | leave Granola (asks for confirmation first) |
| **Menu** | the projects view |
| **Master knob** | master volume |
| **Track 1 / 2 / 3 / 4** | the four views |
| **Shift + Track 4** | the sample harvester |

### The step buttons (all views)

| gesture | what it does |
|---|---|
| *display* | position bar for the focused track, in that track's colour |
| **tap a step** | jump the focused track's playhead there — an instant cut, no glide |
| **hold Rec + tap steps** | record those moves |
| **release Rec** | the recorded moves loop immediately |
| **Shift + Rec** | clear the focused track's loop |
| **Shift + volume-knob touch + Rec** | clear every loop on every track |

The **Rec LED** blinks red while recording, glows the focused track's colour when that track
has a loop, and glows white when some *other* track has one and this track does not.

### View 1 — granular performance (Track 1)

Eight columns, one per track: an encoder and four pads.

| gesture | what it does |
|---|---|
| **encoder** | scans the sample (playhead) when no pad is lit |
| **pads 1–4** | toggle **Size**, **Density**, **Jitter**, **Pitch** |
| **encoder** with one pad lit | move that parameter |
| **encoder** with several lit | move them together as a macro |
| **touch an encoder** | focus that track — the step bar takes its colour and position |

The pads are persistent, non-exclusive toggles: what is lit stays lit until you press it
again.

### View 2 — motion (Track 2)

| row | parameter |
|---|---|
| 1 | **Drift** — smears the grain clock toward irregularity |
| 2 | **Spread** — stereo scatter per grain |
| 3 | **Grain shape** — morphs continuously through Gaussian → Percussive → Plateau → Reverse |

### View 3 — mix and sends (Track 3)

| row | parameter |
|---|---|
| 1 | **Volume** |
| 2 | **Pan** |
| 3 | **Delay send** |
| 4 | **Reverb send** |

### View 4 — performance FX (Track 4)

| gesture | what it does |
|---|---|
| **row 1 pads** | select which tracks run through the chain |
| **row 4, pads 1–4** | FX slots — press to build a random chain, press again to free it |
| **short press a lit slot** | re-roll that slot's chain |
| **Shift + slot pad** | **lock** the slot — the same chain and parameters come back after off/on |
| **hold a slot + jog wheel** | that chain's dry/wet, across all its links |

A held slot glows white so you can see the hold is registered. Locked slots are cyan.

### The sample harvester (Shift + Track 4)

| control | |
|---|---|
| **row 1** | the eight tracks — momentary, a destination to tap |
| **rows 2–3** | the sixteen samples of the last batch |
| **hold a sample pad** | **audition** it — it plays for as long as you hold |
| **hold a sample pad, then tap a track** | assign it |
| **row 4, pad 1** | **generate** a new batch (blinks while working) |
| **jog wheel** | batch size, 1–16 |
| any **Track** button | leave |

### Projects (Menu)

| gesture | what it does |
|---|---|
| **short press a pad** | load that project |
| **Shift + pad** | save the live machine into that slot |
| **hold a pad** | save as well |

A slot blinks while saving. Occupied slots are visibly brighter.

---

## The sample harvester

The harvester searches **YouTube's music catalogue** — the songs listing rather than
general video search — because that catalogue contains catalogued music and nothing else.
It then filters and ranks what it finds before fetching a single byte of audio:

| layer | |
|---|---|
| 120 negative terms | wrong repertoire, matched on title **and** channel — podcast, lecture, tutorial, meditation, type beat, "10 hours of…", self-help, hobbyist electronics |
| 10 negative patterns | shapes words cannot catch — `Ep. 42`, `#1999`, `S02E14`, timestamps, `Dr. <name>`, `Change Your…` |
| 3 channel patterns | the uploader is often cleaner evidence than the title |
| 64 positive terms + `rank()` | order what survives, with duration bonuses and a repeat-channel penalty |
| qualified randomness | a weighted draw, so a batch is a spread rather than six variations of one idea |

Those rules are load-bearing, and text rules drift — every term added to catch one bad title
risks blocking real music. [`tools/test-filter.py`](tools/test-filter.py) asserts both
directions: 16 known-bad titles rejected, 20 known-good kept. Run it after touching the
lists.

```bash
python3 tools/test-filter.py
```

### What you harvest is your responsibility

The harvester retrieves audio that other people published. Granola does not, and cannot,
determine the rights attached to anything it fetches — a search result carries no licence
information, and the term filters select for *musical repertoire*, not for permission.

**Deciding what to do with harvested material is the user's responsibility.** Sampling it,
performing with it, recording it or releasing it may require clearance that this tool
neither obtains nor checks. Local law varies; so does what any given platform's terms
permit. Use it accordingly.

Excerpt length is **4–9 seconds** by default, settable anywhere from 3 to 20 seconds in the
web UI. A batch persists across view changes, restarts and crashes — every sample is written
to `state/harvest.json` as it lands.

The whole pipeline runs on the device: vendored yt-dlp fetches a bounded prefix, a
pure-Python WebM→Ogg-Opus remuxer converts it (no ffmpeg exists here), and the SC engine
cuts, normalises and fades the excerpt.

**There is no content filtering of any kind.** The term lists target musical repertoire —
they exist to avoid spoken word and beat packs, not to judge subject matter.

---

## How it fits together

```
Move hardware
    │  MIDI
    ▼
ui.js  (Schwung overtake — owns no synthesis state)
    │  ipc/control.json          ▲ ipc/status.json
    ▼                            │
Python controller  ── OSC ──►  sclang  ──►  scsynth
  model, projects,             engine       GrainBuf voices,
  FX slots, harvester,         graph        JPverb, delay,
  gesture loops                             154 Airwindows UGens
    │
    └── HTTP :7135  web interface
```

`ui.js` cannot open sockets, so the module and controller talk through two JSON files on
`/data`. The controller owns all state; the module renders it and sends gestures. That
split is why redrawing LEDs, switching views or relaunching the module cannot disturb a
running sound.

| path | |
|---|---|
| `controller/granola/` | the controller: model, projects, FX, harvester, web UI |
| `supercollider/` | `granola-synthdefs.scd`, `granola-engine.scd` |
| `move/` | deploy and launch scripts, the Schwung module |
| `vendor/airwindows/` | the aarch64 UGen plugin, manifest, costs, 154 synthdefs |
| `tools/` | the filter regression test, the SC syntax gate |
| `docs/ENGINEERING.md` | why everything is the way it is — measurements and deviations |

### Runtime layout on the device

| path | |
|---|---|
| `/data/UserData/granola/samples/` | the sample library |
| `/data/UserData/granola/projects/` | 32 project slots |
| `/data/UserData/granola/state/` | model, harvested batch |
| `/data/UserData/granola/logs/` | controller and engine logs |
| `/data/UserData/granola/ipc/` | `control.json`, `status.json` |

---

## Troubleshooting

**Nothing happens when I open the module.** Check `logs/controller.log` for `engine ready`.
If the engine never boots, the most likely cause is that no SuperCollider bundle was found —
`logs/stack_engine.log` says which paths were probed.

**Audio is stuck or runaway.** **Shift + Play.** The transport only gates the grain clocks,
so it cannot stop a master effect whose feedback state has run away; Shift + Play frees the
voices and rebuilds the reverb and delay, which is the only thing that clears it.

**The harvester returns nothing.** It needs network. `logs/controller.log` reports how many
candidates each discovery pass found and how many were filtered out.

**Samples sound wrong after an update.** Restart the stack — SuperCollider sources are only
re-read at boot.

---

## Licence

Granola for Move is **MIT** — see [`LICENSE`](LICENSE).

It stands on work under other licences. Airwindows (MIT), yt-dlp and python-osc (Unlicense)
are vendored here; SuperCollider and sc3-plugins (GPL), libsndfile (LGPL) and JACK
(LGPL/GPL) are required on the device but **not redistributed by this repository**, because
Granola borrows the bundle another takeover already installed. All of it is set out in
[`THIRD-PARTY-LICENSES.md`](THIRD-PARTY-LICENSES.md), with full texts in
[`licenses/`](licenses/).

## Credits

- **Granola** (macOS) — the original instrument this is a port of.
- **[SampleHarvester](https://github.com/nzimas/sampleharvester)** — the harvester's
  discovery layer.
- **Airwindows** by Chris Johnson — the performance effects.
- **Schwung** and **move-anything** — the overtake host this runs inside.
- **PoundHard** and the other Move takeovers — the SuperCollider bundle Granola borrows, and
  the conventions it follows.
