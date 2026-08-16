# Granola for Move — engineering notes

Everything below is the *why*: the measurements, the wrong turns, and the deviations from
the macOS original. The user-facing documentation is in [the README](../README.md); this
file exists so the reasoning behind each decision survives, because most of it was expensive
to learn and none of it is obvious from the code.

Findings here were measured on the device unless they say otherwise.

---

# Granola for Ableton Move

A port of the macOS **Granola** multitrack granular synthesiser to the Ableton Move, as a
Schwung *overtake* takeover.

This is a port, not a re-design. The DSP, the parameter table (ranges, defaults, curves)
and the macro semantics come from the existing Granola sources and are preserved; what is
new is the user interface, which is rebuilt around Move's encoders, pads, step buttons,
Track buttons and display.

```
Hardware input  ->  ui.js view state  -> [control.json] ->  Granola control model  ->  SC engine / DSP
DSP + model state  ->  [status.json]  ->  ui.js view state  ->  Move display + LEDs
```

---

## 1. What was carried over unchanged

| From the macOS app | Here |
|---|---|
| `Scripts/synthdefs.scd` | `supercollider/granola-synthdefs.scd` — the same `granolaVoice` (GrainBuf), `granolaStrip`, `granolaReverb` (JPverb), `granolaDelay`, `granolaPerfIn/Out`, `granolaMaster` |
| `Engine/GranolaEngine.swift` | `supercollider/granola-engine.scd` — the same fixed group / bus / buffer map, the same voice lifecycle |
| `Model/Parameters.swift` | `controller/granola/params.py` — identical ranges, defaults, curves and OSC key names |
| `Model/TrackModel.swift` | `controller/granola/tracks.py` — the same four macro slots and multi-select macro behaviour |
| `Model/AirwindowsCatalog.swift` + `assembleChain` | `controller/granola/airwindows.py` — the same randomiser: 0.15…0.9 parameters, dry/wet forced wet, one link in three modulated, 1–4 links, no repeats |
| the Airwindows UGen plugin | `vendor/airwindows/GranolaAirwindows.so`, rebuilt for aarch64 from the same unmodified sources |
| `GranolaModel.encoderDelta` / `nudgeMacro` | `params.encoder_delta` / `Model.nudge_encoder` — verbatim |

Granola already had exactly the model this hardware wants: eight tracks, four
non-exclusive macro selectors per track (Size / Density / Jitter / Pitch), and an encoder
that drives whichever of them are lit. On macOS those selectors were the SMC-Mixer's four
per-strip buttons; here they are the four pads under each encoder.

## 2. View 1 — the main granular performance view

The hardware is eight vertical control strips: one encoder, the four pads beneath it, one
Granola track per column. The same model applies to all eight columns.

| Control | Behaviour |
|---|---|
| **Encoder 1–8** | With **no pad lit in that column**: the track's sample scan / playhead. This is the default. With pads lit: a macro over exactly those parameters. |
| **Pads** (top → bottom) | `SIZE` · `DENSITY` · `JITTER` · `PITCH`. Persistent toggles, deliberately **not** mutually exclusive. |
| **16 step buttons** | A 16-segment position bar for the focused track — step 1 = start of sample, 16 = end. |
| **Track buttons 1–4** | View selectors — 1 is this view and the default; 2, 3 and 4 are below. |
| **Play** | Starts / stops every granulator together. Green while running. Works from any view, including Projects — stopping the sound should never require navigating somewhere first. |
| **Master knob** | Output level. |
| **Jog wheel** | Next / previous sample from the library, on the focused track. **Jog push** re-scans the library. |
| **Menu** | Opens the **Projects** view (below). |
| **Back** | **Arms** the exit confirmation — it never exits on its own. |

The selector state is **per column**: another track's pads never change this encoder's role.

**Macro scaling.** Multiple lit pads move together by the same *normalized* delta, and each
parameter maps that back through its own curve into its own domain — Size is exponential
over 2 ms…2 s, Density exponential over 0.2…200 /s, Pitch linear over ±24 st. They are
never handed the same raw number. Verified on the device:

```
pads: SIZE + DENSITY + PITCH  (JITTER off)
one encoder gesture ->  size 0.5927 -> 0.7727   density 0.6667 -> 0.8467   pitch 0.50 -> 0.68
                        jitter 0.6348 -> 0.6348 (untouched)   playhead unchanged
```

**Focus and the step bar.** The bar follows the encoder currently being manipulated — set
both by *touching* an encoder (the Move reports knob touch separately from turn) and by
turning it. Touching is enough: no need to move anything to ask where a track is.

The bar is drawn **in that track's own hue**, both halves of it — filled segments in the
lit index, the remainder in the same hue's dark index. The colour is half the information:
a plain bar says where the playhead is, a coloured one also says *whose*. So one touch
tells you which track you are about to move, which sample it holds (named on the screen)
and where it currently sits, without looking away from the pads.

Switching focus by touch also **drops any local estimate** for the newly selected track and
**forces a status read on the next frame**, so the position shown is the engine's at that
moment rather than a cached one or one up to ~170 ms old.

**The bar is always the playhead**, whatever the encoder happens to be driving. That
distinction is worth stating because it caused a false bug report: a track with a selector
lit (Reverb Send, in that case) has an encoder that moves *that parameter*, so the bar
correctly does not follow the hand, and it looked as though the wrong encoder owned the
track. The screen now names what the bar is showing — `BAR: T3 @ 42%` — alongside the
parameter the encoder is on.

**Track colours.** Move's LEDs take a palette index, not RGB, so each track's identity is a
pair of indices from the hardware's own ramps: a lit index for a pad that is ON and the dark
index of the *same hue* for OFF. Eight hues run left to right through the spectrum, so
column order is legible, a column's four pads read as one family, and toggle state is a
brightness difference within that family.

Both indices of every pair were chosen by reading the palette's **actual hex values**, not
by their names. Two earlier picks failed that test: index 84 is not in the palette at all
(it would have rendered as fallback grey), and "lime" 31 is `#4A8700` — near enough to
green 8 (`#56BF13`) that two adjacent columns read alike.

| | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 |
|---|---|---|---|---|---|---|---|---|
| lit | 1 `#FF2424` | 3 `#FF9900` | 7 `#FFFF00` | 8 `#56BF13` | 12 `#159573` | 16 `#274FCC` | 20 `#8700FF` | 23 `#FF0099` |
| dark | 66 | 75 | 78 | 79 | 87 | 95 | 104 | 110 |
| hue | 0° | 36° | 60° | 97° | 164° | 225° | 272° | 324° |

### View 2 — further parameters

Track button 2. The columns are the same eight tracks with the same eight hues; what
changes is which parameters the pads reach.

| Row | Parameter |
|---|---|
| 1 | **Drift** |
| 2 | **Spread** — the classical granular stereo spread |
| 3 | **Grain Shape** — a continuous morph of the grain window |
| 4 | *empty*, and left unlit rather than given something invented |

**Spread was already in the ported engine**, so it needed exposing rather than adding:
`granolaVoice` scatters every grain's pan between `-spread` and `+spread`, and `Rotate2`
maps that onto ±90°. At 0 the cloud stacks at the centre; at 1 it is thrown across the
whole field. The default is the desktop app's own **0.6** — mid-to-wide, which is where a
granulator wants to sit before anyone touches it.

Measured on the device with a mono source (where spread 0 *must* produce identical
channels, so any divergence is the control working), every other track silenced:

```
spread 0.0  ->  mean |L-R| = 0.00000      spread 0.5  ->  0.02485
spread 1.0  ->  mean |L-R| = 0.02502      spread 0.0  ->  0.00000
```

#### Grain shape as a continuum

The encoder morphs the grain window continuously along
**Gaussian → Percussive → Plateau → Reverse**, crossfading each pair (the ends do not
wrap). The readout names where you are: `GAUSS`, `GAUS>PERC 45%`, `PERC`, and so on.

This is the one place real DSP work was needed, because `GrainBuf` takes a *buffer* for
its window and reads it at grain onset — a morph cannot be computed in the SynthDef, the
interpolated windows have to exist. So the engine pre-renders **64 windows** into a table
of buffers at boot (256 kB, once) and the encoder simply picks one; a grain starting a
moment later picks up the new window, which is what makes it sound continuous rather than
switched. The alternative — rewriting one buffer in place — would mean pushing 1024 floats
per encoder detent *and* mutating a buffer while the audio thread reads it.

The blends are **not** normalised, which was a corrected mistake rather than an oversight.
Normalising each blend to unit peak was tried first, on the theory that a crossfade dips.
It is wrong twice: the four shapes have genuinely different energies in the app (a
percussive window carries far less than a plateau) and rescaling breaks that relationship;
and because a crossfade of two differently-placed peaks has a *lower* peak than either,
dividing by it pushed the in-between windows **above** both neighbours. Measured, the sweep
bulged instead of travelling. Removing it, with a fixed cloud so only the window varies:

```
morph   0.000 GAUSS  0.167 gaus>perc  0.333 PERC   0.500 perc>plat  0.667 PLATEAU  0.833 plat>reve  1.000 REVERSE
level   0.0624       0.0389           0.0272       0.0637           0.0934        0.0608           0.0272
        \____________ descending _____/\___________ ascending _____/\____________ descending _____/
```

Every blend lies between its neighbours, and Percussive and Reverse measure identically —
they are time-mirrors of one another, so equal energy is the right answer.

The four discrete `GrainShape` cases the desktop app switched between are gone as a
*control*; their maths is unchanged and now sits at morph 0, ⅓, ⅔ and 1.

**The selector set grows across views, it does not swap.** A track lit for Size in View 1
and Shape in View 2 and Reverb Send in View 3 has an encoder that macros over *all* of them — the views decide what is
reachable, not what is on. Leaving View 2 does not quietly hand the encoder back to the
playhead. Verified on the device:

```
drift pad on           -> label "Drift",  encoder moves drift 0.10 -> 0.22, size untouched
+ size pad (View 1)    -> label "MACRO x2", one gesture moves drift 0.22 -> 0.30
                                                        and size  0.12s -> 0.20s
```

### View 3 — mixer and master sends

Track button 3. Same eight columns, same hues, same logic.

| Row | Parameter |
|---|---|
| 1 | **Volume** — the track's level (0…1.4, default 0.7) |
| 2 | **Pan** — centre-detented |
| 3 | **Delay send** |
| 4 | **Reverb send** |

All four were already in the ported parameter table, and `granolaStrip` already carried
post-fader sends to the reverb and delay buses — so this view is the first thing to
actually exercise the master-effects path. Nothing in the signal flow changed; it was
built at boot from the start and had simply never been fed.

`level` and the two sends belong to `granolaStrip` while `pan` belongs to `granolaVoice`,
but nothing upstream needs to know: `ParamSpec.is_strip` routes them and the engine splits
one message into the right nodes.

**Pan catches centre**, ported from the desktop's `nudgeSingle(centreDetent:)` — and, as
there, only when the move passes *through* centre (a detent defined as "within X of
centre" is wider than one encoder tick, so it swallows every tick and the control appears
dead), and only when pan is the sole selected parameter, so a macro sweep never sticks.
Pan reads as `L45` / `C` / `R30` rather than a signed decimal — on a 128×64 screen the
former is read at a glance.

Measured on the device, with a probe track and the transport stopped so only the tail is
left — the dry path stops instantly and a raised send does not:

```
sends OFF        tail 0.00s
REVERB send 1.0  tail 2.37s     (JPverb, t60 3.5s)
DELAY  send 1.0  tail 0.81s     (0.375/0.5s, feedback 0.45)
```

The two master effects keep their SynthDef defaults for now; a view or gesture for their
own parameters comes later.

### View 4 — performance FX (Airwindows)

Track button 4. Not a parameter view: it is the routing surface for the performance
chain, exactly as in the desktop app.

| Row | |
|---|---|
| 1 | which **tracks** are routed through the chain, each in its own hue |
| 2–3 | *empty* |
| 4 (first four pads) | four **chain slots** |

A short press toggles a slot, and **turning one on is what assembles its chain** — one to
four Airwindows effects at random, never the same effect twice. There is no separate
"generate" button because switching a slot on *is* the roll, which is the app's own
gesture. **Hold** a running slot to re-roll it without disturbing the others.

Slots stack in the order they were switched **on**, and that activation order is the
signal order — so the composite chain reflects the order you pressed the pads and closes
up when one is removed. The Move's screen shows that composite chain, since it is the one
thing slot order cannot tell you. A `~` marks a link whose parameter is being moved by an
LFO (roughly one in three, as in the app).

#### Riding a chain's dry/wet — hold a slot + jog

**Hold a chain slot and turn the jog wheel** to move that slot's dry/wet, every Airwindows
link in its chain at once. The held pad turns **white** for as long as your finger is on
it: a modifier that leaves no mark is one you cannot tell you have engaged, and this one
needs to be obvious *before* the other hand reaches the jog, not after turning it.

The hold is purely a modifier and does nothing on its own — re-rolling is a short press
(toggling a slot on is what assembles a fresh chain), so nothing had to move aside for
this gesture.

It **scales** the links rather than flattening them to one number. Each was rolled with its
own blend around `FX_WET_MIX`, and that relative balance is part of the chain's character;
the control moves them together. At 0 the chain is fully dry. Roughly 2% of the range per
detent, so a full sweep is about a turn and a half.

A slot brought back up carries the wet level it was left at, not the one it was rolled
with. Measured on the device with a steady source:

```
rolled blend, wet 0.40      level 0.0079
jog to        wet 0.90      level 0.0016     (a chain that attenuates when fully wet)
jog to        wet 0.00      level 0.0078     — identical to dry, so the blend reaches every link
```

The web page gives each slot the same control as a slider.

#### The CPU budget — why chains are rolled against a cost

Chains caused audible xruns, so all 154 effects were **profiled on the Move itself**: each
one inserted alone into the chain over a fixed source, measuring the rise in scsynth's CPU
and any xruns it caused. The answer to "is it CPU, or is it a few bad plugins, or is it a
software fault" turned out to be measurable rather than arguable.

| | |
|---|---|
| per-effect cost | median **4.6%** of one core, mean 5.0%, range **0.7% – 15.2%** |
| effects ≥3% | **149 of 154** — it is not a handful of outliers, the whole repertoire is dear |
| worst family | the amp simulations. MidAmp + BigAmp + LeadAmp + FireAmp alone took the box **47% → 97%** |
| xruns from any single effect | **zero**, across all 154 |

So no plugin is pathological, and nothing is broken: it is arithmetic. Four slots × four
links is **sixteen links** — ~74% at the median on top of a ~37% eight-voice baseline, and
far worse if the roll happens to favour amp sims. The dropouts were simply the rack being
allowed to ask for more than the machine has.

The fix is therefore not fewer slots but a **budget**. `vendor/airwindows/fx-cost.json`
carries the measured cost of every effect, and a roll spends against it: cheap effects buy
length, expensive ones cost it. The candidate pool is filtered to what still fits *before*
each draw, which is what keeps late slots varied — rejecting after the draw would make a
nearly-full rack keep picking expensive effects, fail, and hand every late slot a single
link.

That keeps all four slots — the app's own layout — while bounding what they can ask for:

```
                                        peak CPU   xruns
8 voices granulating, no rack              36.5%      0
8 voices + FULL rack (all 4 slots)         63.6%      0     rack estimate 31.9% of 32%
```

**Would supernova help?** Not with this. Supernova parallelises across *ParGroups*, and an
FX chain is inherently serial — every link reads what the previous one wrote on the same
bus, so it cannot be spread across cores no matter which server runs it. It could
parallelise the eight granular *voices*, which are independent, and that would free
headroom for the (still single-threaded) chain. But on this device `JPverb_supernova.so`
does not register its UGen — measured — so supernova currently costs the master reverb.
That trade is worth revisiting only if the voices, not the chain, become the ceiling.

#### Locked slots — a Move addition

**Shift + a slot pad locks it**, and shift + that pad again frees it. A locked slot keeps
its chain *and the exact parameters it was rolled with*, so switching it off and on again
brings back the same sound instead of a new one. That is what turns a lucky roll into
something you can perform with — the desktop app has no equivalent, because every
activation there is a fresh roll.

Locked slots move to a different **hue family** rather than a different brightness: amber
is free, **cyan is locked**, and within each family the lit index means the chain is
running and the dark one means it is not. So "is it locked" and "is it on" are two
independent readings, not one crowded one.

Three details that make the lock actually hold:

* **Off keeps the chain.** For an unlocked slot, switching off clears it — that clearing
  is precisely what makes the next activation roll something new. A locked slot keeps it.
* **A re-roll is refused, not obeyed.** Holding a locked slot says `S1 LOCKED` and changes
  nothing. A lock that another gesture can overwrite is not a lock.
* **Unlocking an inactive slot drops its chain**, so the slot genuinely returns to normal
  behaviour instead of replaying the old chain once more.

Locking an empty slot is allowed: it arms the lock, and the first chain rolled into it
becomes the one it holds.

Verified on the device:

```
unlocked   roll A  ChromeOxide(0.444)                 off -> (empty)
           roll B  Coils(0.371) > Zoom(0.443)         -> changed
locked     off/on x3, chain kept while off and identical on return, all three cycles
           re-roll -> {ok: false, reason: locked}, chain unchanged
unlocked   off -> (empty);  fresh roll  Fracture > Beam > Melt > Pop2   -> different
```

Locked chains live in memory: they survive off/on, view changes and an engine restart
(`_restore_fx` rebuilds them without re-rolling), but they are **not** written into
projects yet — project files carry tracks and parameters, not the FX rack.

#### The rack only exists while something is routed into it

`granolaReverb` and `granolaDelay` both output to the **performance bus** — that is the
desktop app's design, so the chain mangles the effect returns as well as the routed tracks.
The consequence here was that a raised reverb send made the Airwindows chain audible with
**no track routed at all**: measured, one chain took the master from 0.0501 to 0.0722 with
nothing selected. That is not what "these tracks go through the effects" should mean.

The links are therefore simply **not in the graph** while nothing is routed. Arming a slot
still rolls and holds its chain; routing the first track in brings the rack up, routing the
last one out takes it down, both over the existing 5-second fade.

Bypassing each link was the obvious alternative and is worse: the compiled SynthDefs have
no bypass control, and stepping `mix` to zero would click. Not existing costs nothing and
cannot leak.

```
                                        master level   awfx links in the graph
reverb send up, nothing routed, no chain    0.0087                 0
chain ARMED, nothing routed                 0.0082                 0     (-6%, i.e. noise)
track routed -> chain LIVE                  0.0097                 2     (+11%)
```

The node count is the decisive part: an armed rack with nothing routed has no links at all,
so it cannot be processing anything. (A first attempt to prove this by level alone was
inconclusive — a random grain cloud is not a steady enough source to compare two readings,
and two *identical* conditions differed by as much as the effect being measured.)

An armed slot with nothing routed **breathes** between its lit and dark shade rather than
sitting steady, and the screen says `(SILENT)`: the slot is on, the chain is held, nothing
is being processed. That is a real state, and it needs to look different from one that is
making sound.

Links crossfade in and out over **5 seconds** — `fxFadeTime`, carried over: a performance
control should swell rather than snap. That is also why a re-roll flashes the pad; without
it the gesture would appear to do nothing for several seconds.

The encoders keep doing what they do in every other view (the selector set is global), so
a hand on an encoder behaves the same whichever view is open.

Verified on the device — chains rolling, re-rolling, stacking, and the resulting graph:

```
slot 1 rolled     PaulWide > IronOxideClassic2 > Tube
re-rolled         TapeHack
slot 2 added      Remap > StereoDoubler > Capacitor > Creature
composite         TapeHack > Remap~ > StereoDoubler~ > Capacitor > Creature~

NODE TREE Group 14        (the performance group, in signal order)
   1490 granolaPerfIn
   3004 awfx_TapeHack
   3005 awfx_Remap
   3006 awfx_StereoDoubler
   3007 awfx_Capacitor
   3008 awfx_Creature
   1500 granolaPerfOut
```

Switching the slots off leaves the group back at head + terminator, with no orphans.

#### Airwindows on the Move

The **154-effect repertoire is fully ported**, not approximated. `move/build-airwindows.sh`
drives Granola's own `Scripts/build-airwindows.sh` — which already had a Linux path — inside
an **arm64 Debian container**, so the Airwindows sources compile *unmodified* against the
same minimal `audioeffectx.h` shim. The DSP, and therefore the sound, is exactly as
published; only the wrapper differs, a UGen instead of a VST. The Linux build drops nothing:
154 effects, the same set as the macOS build.

Only the `.so` is architecture-specific. The `awfx_*.scsyndef` files are copied from the
macOS build unchanged — a compiled SynthDef is a portable graph description — which also
guarantees both platforms run the identical effect graph, gain-matching and LFO wiring.

The Granola checkout is mounted **read-only** during the build: the same script writes
`vendor/airwindows/`, so a Move build would otherwise overwrite the macOS artifacts.

Schwung ships an `Airwindows.clap` of its own, but a CLAP plugin is not reachable from a
SuperCollider graph without a host to load it — hence Granola running its own.

### Transport

Play starts and stops all eight granulators. It stops the grain **clock** rather than
muting the output, so grains already in flight finish their envelopes instead of being
chopped; an 80 ms fade then catches anything long enough to outlast a musical stop.

Granola comes up **stopped**. Eight grain clouds at full tilt the moment the module opens
is not a neutral state to hand someone, and it would make the Play button meaningless on
launch. The screen says `RUN`/`STOP` as well as the button LED, because a stopped
granulator looks exactly like one with no sample loaded. Measured on the device: master
level `0.0 → 0.029 → 0.0` across stop/start/stop.

### Leaving

Back **arms** a confirmation rather than exiting: the screen shows `EXIT YES?`, and a
**jog-wheel push** commits while **Back** cancels. The prompt is modal and has no timeout —
it stays up until you decide, and everything else is swallowed while it shows, so a stray
pad can neither dismiss it nor trigger an edit behind it. Same gesture as PoundHard and
OneManShow.

### A project is the whole machine

Saving a project captures everything, including **the playhead of every track**. What goes
in a slot:

| | |
|---|---|
| per track | sample, every parameter, the four pad toggles, mute, **playhead position** |
| global | master level, transport running state |
| FX rack | each slot's active/locked state, dry/wet, activation order, and the **exact chain** — every link's effect, parameters, mix and LFO settings |

The playhead saved is the one the **engine reports** — the model's `position` plus any
free-running scan — because that is the true head at that instant. The model's own
`position` only tracks the encoder, and would restore the wrong place on any scanning
track. On load it is re-asserted as a jump once the sample has finished loading, since the
voice that carries it does not exist until then.

`Track.apply` used to zero the head deliberately, so that a model restored at launch agreed
with the position bar. Granola no longer restores a model at launch — it starts empty — so
that rule only survived as a bug that threw away part of every project.

The FX chain is **stored, not re-rolled**: a slot's chain is random when created, so
re-rolling on load would give a different sound under the same project name. If the
Airwindows catalogue ever changes under a saved project, an unknown effect drops that one
link and says so, rather than losing the chain.

Verified end to end — build a machine, save it, wreck it, reload:

```
BEFORE  T1 0.6875  T4 0.3125 | run True  | fx0 BeziComp > Texturize > Swell | routed [0,2]
WRECKED T1 0.0000  T4 0.9375 | run False | fx0 —                            | routed []
AFTER   T1 0.6875  T4 0.3125 | run True  | fx0 BeziComp > Texturize > Swell | routed [0,2]
```

**Not yet in a project:** the master reverb and delay parameters — the controller has no UI
for them yet and never sends them, so they sit at their SynthDef defaults for every
project. When that view exists, they belong in the `machine` block too.

### Playhead gesture loops — hold Rec

Hold **Rec** and tap step buttons: the playhead moves as it always does, and the taps are
captured. Release Rec and the sequence loops immediately, on that track.

| gesture | |
|---|---|
| hold **Rec** + tap steps | record playhead moves on the focused track |
| release **Rec** | the loop starts at once |
| **Shift + Rec** | clear the focused track's loop |
| **Shift + volume-knob touch + Rec** | clear every loop on every track |

**The loop length is the whole held window**, not the span between first and last tap. How
long you hold Rec sets the bar — a tap at the start and nothing else gives a long sparse
loop rather than a one-shot, and the silence at the end is part of the phrase.

The same press both moves the head and gets recorded; there is no separate arm-and-record
to learn. It rides on the existing `jump` command, so a recorded cut is bit-for-bit the
same event as a played one.

**The Rec LED says three things:**

| | |
|---|---|
| blinking red | recording right now |
| the focused track's colour | that track has a loop running |
| white | some *other* track has a loop and this one does not |

so a glance tells you both that loops exist and whether the track under your hands is one
of them.

Playback runs on **its own 200 Hz thread**, not the 30 Hz control loop: 33 ms of jitter on
a jump cut is audible as sloppy timing, and the point of the gesture is that the cuts land
where you put them.

Clearing everything is deliberately a three-finger chord — shift, the volume knob's touch
sensor, and Rec — because it is the one action here that destroys work and cannot be
undone.

Verified on the device: taps at 0, 8, 4, 12 held for 2.0 s retrace as
`0 → .5 → .25 → .75 → 0 → .5 → .25 → .75`, two restarts in 4.2 s; loops on tracks 1 and 3
give track colour on 1 and 3 and white on 6; both clear gestures empty what they should.

### The 16 step buttons — tap to jump the playhead

The strip shows the focused track's playhead in that track's colour; pressing a segment
moves the head there. A step is a POSITION, not an offset, so tapping the same button
repeatedly cuts back to the same place — that repetition is the effect, and it works in
every view because the strip is global. An empty track says so rather than jumping
silently.

**The jump is a cut, not a scrub.** The head is normally de-zippered by `posLag` (50 ms) so
an encoder sweep does not zipper — but that same lag turns a jump into a 50 ms glide
*through* everything between the two positions, which is a zip. `/gr/jump` therefore drops
`posLag` to zero, sets the position, and restores the lag 60 ms later, leaving the
encoder's smoothing untouched. Grains already in flight are not disturbed; they finish
where they started, which is what keeps the cut from clicking.

Verified on the device through the same `control.json` path the buttons use — steps 0, 4,
8, 12, 15 and 3 landing on 0.000, 0.250, 0.500, 0.750, 0.938 and 0.188, and `posLag`
reading 0 during the jump and 0.05 again afterwards.

### Pitch 0 is the sample's own pitch

`granolaVoice` does **not** multiply its rate by `BufRateScale`, even though the macOS app
does. `GrainBuf` already compensates for the buffer's sample rate internally; `PlayBuf`
does not. Applying it to `GrainBuf` applies the correction twice.

Measured with [tools/test-grainbuf-rate.scd](tools/test-grainbuf-rate.scd) — a 1000 Hz tone
in a 48 kHz buffer, rendered on a 44.1 kHz server:

| GrainBuf rate | output |
|---|---|
| `1` | 1000 Hz — correct |
| `BufRateScale` | 1088 Hz — **+1.46 semitones** |

The app is not wrong to carry the multiply: it runs its server at the same rate as its
material, where `BufRateScale` is 1.0 and the error cannot show. Here the server is
44.1 kHz and every harvested sample is 48 kHz (Opus always decodes at 48 kHz), so every
harvested sample arrived a semitone and a half sharp. The audition path keeps
`BufRateScale` because `PlayBuf` needs it — confirmed independently by playback duration
(6.105 s measured against 6.121 s of audio).

### Panic — Shift + Play

Frees every voice, stops any audition, and **rebuilds the master reverb and delay**.

The rebuild is the point. Play only gates the grain clocks, so it cannot stop a master
effect whose internal feedback state has run away: that state survives a stop, a view
change, an empty project and a `/gr/panic` that only frees voices, because it sits
downstream of every voice and needs no input to sustain itself. Observed on the device as
audio escalating to full scale with the voice and strip groups already empty. Recreating
the two synths is the only thing that clears it, so panic does that, then pushes the live
reverb and delay values back — the engine remembers them for exactly this reason.

Measured: a reverb fed and left ringing at 0.484 with its input removed drops to **0.0000**
after Shift+Play, and audition, tracks and sends all work normally afterwards.

Every feedback path is also **sanitised**: `CheckBadValues` plus a hard bound on the voice
output, on both effect inputs, on the delay's stored feedback and on both effect outputs.
One non-finite sample entering a feedback network poisons it permanently, and NaNs cost
nothing to reject and everything to let through.

### The harvester — Shift + Track 4

Pulls fresh material off YouTube, cuts an excerpt from it and drops it into Granola's own
sample library. A port of SampleHarvester's HARVEST layer, rebuilt from what this device
actually has.

| Control | |
|---|---|
| Row 1 | the eight **tracks**, momentary — a destination to tap, not a state |
| Rows 2–3 | the sixteen **samples** of the last batch |
| Row 4, pad 1 | **generate**, momentary; blinks while a batch comes in |
| Jog | how many samples one batch brings back, **1–16** |
| Excerpt length | **4–9 s** by default; set the range in the web UI, anywhere in 3–20 s |
| audition | **hold a sample pad** — it plays for as long as you hold it |
| assign | **hold a sample pad, then short-press a track pad** |
| leave | any Track button |

The screen carries a full-width progress bar with the percentage in the same oversized
block font every other value in Granola uses.

**Granola starts empty.** It does not reload the last session's model. Restoring it meant
the instrument came up with samples on tracks nobody had loaded, and pressing Play produced
a grain cloud from a track the performer had never touched — audible, with no visible cause,
because the mental model was "nothing is loaded". Projects are the deliberate way to bring
state back: Menu, then a pad. `GR_RESTORE_MODEL=1` restores the old behaviour.

**A batch outlives the view, and the process.** The samples are written to
`state/harvest.json` as each one lands, so leaving the harvester view and coming back — or
a restart, or a crash halfway through a batch — keeps everything already gathered. Those
pads are the only place harvested material is reachable, and a batch costs minutes of
network time. Entries whose file has since disappeared are dropped rather than shown as
pads that do nothing. This is deliberately *not* project state: the machine still starts
with no project loaded.

**Excerpt length is a range, not the app's buckets.** SampleHarvester draws from weighted
buckets spanning 2–60 s. Here it is 4–9 s by default, settable from 3 to 20 s in the web
UI: on this instrument a harvested sample is grain fodder rather than a clip — long enough
to scan through, short enough to stay one gesture. The web control clamps to the floor and
ceiling and orders the pair, so no combination of typing can produce an invalid range.

**Audition and assign are one gesture, not two.** Holding a pad both sounds the sample and
arms it; tapping a track then places it. So the ordinary way to use a batch — hear it,
decide, place it — is a single uninterrupted hold, and the pad behaves like a monitor
button rather than a launcher. While a pad is held the screen names the sample and its
length, because that is the question the ear is already asking.

The audition deliberately bypasses the track strips and the performance chain: it is a
"what is this?" monitor, so it must not change with whichever track happens to be routed
or what FX chain is loaded. It sits in the **voices** group writing to the mix bus — after
`granolaMaster`, which reads the mix bus and then clears it, it would be silent — and it
does **not** need the transport running, since Play only gates the granular clocks. A
sample can therefore be auditioned with the machine stopped.

Buffers and node IDs for auditioning are a **ring of four** rather than one of each. The
release fades over 60 ms, so moving quickly from pad to pad can start the next audition
while the last is still sounding; a single node ID risks a duplicate-ID failure and a
single buffer means freeing memory a still-reading synth is using. Measured: six
back-to-back re-triggers, then the same file plays at full level with zero server
failures, and the bus returns to exact silence on release.

#### The search is the YouTube Music catalogue, not YouTube

This is the single thing that decides whether a batch is usable. SampleHarvester's provider
contract is `YouTubeMusicSourceProvider`, and `DiscoveryEngine` asks it for
`SearchFilters(catalogue: .musicSongs, …)` — the Songs tab of YouTube Music, which contains
catalogued music tracks and nothing else. The first version of this port used yt-dlp's plain
`ytsearch`, which searches **all** of YouTube: podcasts, lectures, tutorials and news are in
that index. It returned exactly that. No downstream filtering repairs drawing from the wrong
pool, and the batches were rightly called rubbish.

Songs only, deliberately: the multi-catalogue default on `SearchFilters` is never what
discovery uses — it calls the single-catalogue initialiser, and album hits are playlist
entries carrying no streams.

Three layers of `DiscoveryEngine` are now ported, all of which were missing:

| | what it does |
|---|---|
| `negativeTerms` (40 terms) | drops the wrong repertoire on title + channel before anything is fetched — `podcast`, `interview`, `tutorial`, `lesson`, `how to`, `news`, `audiobook`, `type beat`, `karaoke` … |
| `positiveTerms` (29) + `rank()` | scores what is left, plus duration bonuses and a repeat-channel penalty |
| qualified randomness | `RegionEngine.select`'s weighted draw, `(quality × (1 − diversityPenalty)) ^ 6(1 − randomness)`, at the app's `randomness 0.5` / `diversity 0.75` |

Discovery runs **once per batch** and builds a ranked pool (measured: ~115 candidates from
6 queries), and a dud candidate then costs a candidate rather than a sample. Searching per
sample — the first version — meant one throttled search cost a whole slot.

Qualified randomness is not a detail. Taking the strict top rank produced a technically
correct batch that was six variations of one idea (measured: four of six were cello),
because ranking rewards the same terms every run.

**One deliberate deviation:** the app's diversity penalty is computed from 10-dimensional
audio descriptor vectors produced by its analysis engine. There is no analysis layer here,
so the penalty is computed from title-token overlap against what the batch already holds.
It is a weaker proxy and is not the same thing — but it is what stops a batch converging on
one instrument.

Measured before and after, same machine, batch of 6–8:

| | result |
|---|---|
| plain `ytsearch` | 1/6 usable; returned *Raspberry Pi – Remote…* |
| ported discovery | 8/8, in 104 s: *Granular Rain*, *Hovering Resonance*, *Metamorphosis and Resonances for Bass Clarinet*, *Modular Ambient in Cminor*, *Multiphonics Etude*, *Bowed Metal and Resin*, *Granular Blankets*, *Shortwave radio* |

#### No audio analysis — and why

The app rejects material after decoding, in `AnalysisEngine` / `RegionEngine`: a speech
gate, a rhythmicity gate, a silence gate, over 96 scored candidate regions. That was fully
ported here, it worked, and its gates did reject spoken-word sources outright.

**It has been removed.** Measured on the device, it cost 40–75 s of near-100% CPU *per
source* — on a machine whose own application already sits at ~70% CPU and drops audio
frames continuously. A batch of sixteen meant ten minutes of that, and the Move hard-reset
twice in one morning with no OOM and no kernel panic recorded, which is the signature of a
starved watchdog (the kernel registers `bcm2835-wdt`). This device does not have the
headroom for per-source DSP at that scale, and an instrument that reboots mid-set is worth
nothing. Region choice is back to the loudest-window probe: 24 small reads, no DSP.

Measured after removal: **4/4 in 70 s, 18 s per sample**, against ~60 s per sample with the
analysis in place.

#### The filter is the whole quality story

With no audio gate behind it, the metadata filter is load-bearing. It runs before a single
byte of audio is fetched, which is also the only place a rejection is free:

| layer | what it does |
|---|---|
| YouTube Music **songs** catalogue | the pool contains catalogued music and nothing else |
| 120 negative terms | wrong repertoire, on title **and** channel — podcast, lecture, tutorial, meditation, type beat, 10 hours of…, self-help, hobbyist electronics |
| 10 negative patterns | shapes words cannot catch — `Ep. 42`, `#1999`, `S02E14`, timestamps, `Dr. <name>`, `Change Your…` |
| 3 channel patterns | the uploader is often cleaner evidence than the title |
| 64 positive terms + `rank()` | orders what survives |
| qualified randomness | weighted draw, so a batch is a spread rather than a cluster |

Load-bearing text rules drift — every term added to catch one bad title risks blocking a
real piece of music — so both directions are tested:
[tools/test-filter.py](tools/test-filter.py) asserts 16 known-bad titles are rejected and
20 known-good ones survive. Run it after touching the lists.

**The settings are SampleHarvester's own `GenerationConfig` defaults**, read off the source
rather than invented:

| | app default | here |
|---|---|---|
| `sampleCount` | 8 | 8 (the jog overrides it, 1–16) |
| `minDuration` / `maxDuration` + `durationBuckets` | 3–30 s, weighted 2-5 / 5-15 / 15-30 / 30-60 | same, so a batch comes back with a spread of lengths |
| `minSourceDuration` / `maxSourceDuration` | 45 s / 40 min | same |
| `queriesPerRun` / `candidatesPerQuery` | 6 / 20 | same |
| `maxSourcesPerUploader` | 1 | same |
| `excerptSeconds` | 240 | the prefix is sized to it (~3.9 MB at itag 251's bitrate) |
| `fadeIn` / `fadeOut` / `targetLoudnessDB` | 10 ms / 20 ms / −1.0 dB | applied in the engine |
| `boundaryAvoidance` / `avoidBoundaries` | 0.05 / true | the opening and closing 5% are skipped |
| `termPools` / query shapes | `TermPools.default`, 7 `QueryGenerator.Shape`s | ported verbatim — the four pools recombined, no canned query list |

The one deliberate departure, as asked: **harmonisation is off and there is no harmonic
target** (the app defaults to `rootAlignment` onto pitch class 0). Nothing is pitch-shifted,
so a harvested sample is the source material untouched apart from being cut, faded and
normalised.

**Nothing filters on content.** The only rejections are technical — a source too short to
excerpt from, or a chosen region that is silence. The app's descriptor gates, rhythmicity
threshold and speech policy need per-frame analysis this device has no numpy for; they are
absent, not replaced by anything.

#### How it runs without ffmpeg or a JVM

The desktop tool is Swift plus a JVM helper (NewPipeExtractor), ffmpeg and yt-dlp. The Move
has **none** of that — no JVM, no ffmpeg, no pip, no numpy — so each stage was rebuilt from
what is there:

| Stage | On the Move |
|---|---|
| discover | **yt-dlp**, vendored whole (12 MB, pure Python, no compiled deps) |
| acquire | a **bounded prefix** (~1.2 MB ≈ 75 s) of the Opus stream — a harvest wants an excerpt, not a two-hour upload |
| decode | **none needed**: itag 251 is already Opus. `webm_opus.py` lifts the packets out of Matroska and re-frames them as Ogg, and libsndfile 1.0.31 reads Ogg Opus. Sample-for-sample the original bitstream |
| region | the **engine** picks it (`/gr/excerpt`), because the audio is already decoded there and Python has no numpy |
| render | written as a plain wav into `granola/samples` |

Three things this ran into, each worth recording:

* **YouTube binds its stream URLs to the request context that produced them.** A hand-rolled
  GET with the exact headers yt-dlp reports gets a flat `403`. yt-dlp therefore does the
  fetching, and the prefix is taken by aborting from a progress hook — the `.part` file it
  leaves *is* the WebM prefix, and the remuxer expects truncation.
* **A recursive EBML walk cannot collect a TrackEntry's fields**, because each child lands
  in the callee's locals where the caller deciding "is this the Opus track?" cannot see
  them. A WebM full of Opus reported no Opus track at all until TrackEntry got its own
  parser.
* **`Buffer`'s async callbacks do not fire reliably in this sclang.** A first excerpt stage
  built on `Buffer.read` + `Buffer.getn` returned nothing whatsoever — not even an error —
  which is the same failure `Buffer.readChannel` showed during the initial port. It is now
  pure `SoundFile`, synchronous, through the same libsndfile.

Measured on the device, a batch of three:

```
  2.0s  harvest 1/3   0%   spring reverb tank long take
 10.0s  harvest 1/3 remux
 26.0s  done        100%   3/3 samples, 4.0s each
```

Queries are recombined from the app's own term pools each run, so discovery stays
unpredictable across sessions while staying inside the repertoire — `spectral tape music
electronics`, `ondes martenot piece`, `subterranean tape music pipe organ`. What YouTube
returns for them is what YouTube returns.

## 3. Projects

A project is one complete Granola machine: eight tracks, each with its sample, its whole
parameter set and every one of its selector toggles across all views, plus the master level. That is exactly a model
snapshot, so a project *is* a snapshot with a name — there is no second representation to
keep in step. 32 slots, one file each under `projects/`, in the same place the other
takeovers on this box put them.

**On the Move** — the **Menu** button opens the projects view; the 32 pads are the 32 slots.

| Gesture | Result |
|---|---|
| short press | **load** that slot |
| **Shift + pad** | **save** the live machine into that slot |
| hold (> 420 ms) | **save** as well, for a hand that is not on Shift |

Shift + pad is the primary save. It is what the other takeovers on this box use, so it is
what a hand reaches for — and unlike a hold it commits the instant you press, with no
question of whether you held long enough. Both gestures go through one function, so they
cannot drift apart in what they do or what they show.

While a save is in flight the pad **blinks** white/yellow. A save is a few milliseconds of
JSON — far too fast to see — but "did that take?" is exactly the question worth answering
mid-set, so the controller holds a blink window and the pad reports *that*, not the button
press. The blink starts the moment the hold commits, while the finger is still down.

Slot colours sit deliberately outside the eight track hues: a project is not a track, and
borrowing a track's colour would read as "this slot belongs to track 3".

| | |
|---|---|
| free | Dark Blue 95 `#0C1940` — almost unlit |
| **holds a project** | Azure 15 `#0074FC` — the same hue family at nearly full value |
| loaded, or just saved into | White |
| saving | blinks White ↔ Vivid Yellow at ~5 Hz |

Occupied has to be unmistakable at a glance rather than a shade of empty: this was Royal
Blue against Dark Blue at first, which is two dim blues and reads as "nothing here" either
way. After a save the slot becomes the *current* project, so it stays White — a persistent
answer to "where am I working".

While the projects view is open the encoders, master knob and jog are inert — those
gestures belong to the performance view and would otherwise quietly edit a track that is
not on screen.

## 4. The web interface — http://move.local:7135

Load samples into the eight track slots, and manage the 32 projects, from a browser.

The page is a **schematic of the Move**: eight columns of one encoder over four pads, the
sixteen step buttons underneath, the Track buttons and the Menu button down the left side.
Pad, encoder and step colours are read from the Move's *own* LED palette table
(`constants.mjs`), so a swatch in the browser is the colour the hardware actually lights.

* **Sample slot under each column** — click it for a filtered browser over Granola's own
  library, with "clear this track" at the top. Picking one loads it into that track's grain
  cloud immediately.
* **Drag a file onto the browser** (or click the drop zone) to upload it to
  `granola/samples` and assign it in one gesture. The write is atomic, so a rescan can
  never offer a half-written file as a sample.
* **Pads are clickable** — they toggle the same Size/Density/Jitter/Pitch selectors as the
  hardware, and show each parameter's current value.
* **Projects grid** — click to load; *Save to slot…* then click a slot to save, with an
  optional name. Rename and delete are available on the API.
* **Play / Stop and an output-level meter** in the header, mirroring the hardware
  transport — so the page shows the instrument is actually making sound, not only that it
  thinks it is.
* All **eleven selectors** are shown at once, grouped and labelled by the view that owns
  them, so the page never implies the hardware has eleven rows of pads.

It runs **inside the controller process** and holds a reference to it, so a change made in
a browser and the same change made on the hardware go through one code path — there is no
second copy of the state to drift. (OneManShow needs a file spool for this because its
engine is sclang; here the model is already Python, in this process.)

One deliberate departure from LED fidelity: the Move's dark band is genuinely near-black
(track 1's off-state is `#1A0404`), which is right on an LED beside unlit plastic but makes
a whole column vanish on a bright screen. The fill stays the true colour and the identity
is carried by a low-alpha border in the lit hue.

## 5. Architecture

The Schwung host gives `ui.js` no sockets, so the layering matches the proven PoundHard
pattern on this device:

```
move/schwung-module/granola/ui.js     hardware I/O only: pads, encoders, LEDs, screen
        |  ipc/control.json  (queued, seq-deduped)
controller/granola/                   the Granola control model — authoritative state
        |  OSC /gr/...
supercollider/granola-engine.scd      graph, buffers, voices
        |
    scsynth
```

`ui.js` owns **no** synthesis state. It sends gestures and renders `status.json`, so
redrawing LEDs, switching views or relaunching the module cannot reset a parameter. Views
2–4 can be added by extending `renderLEDs`/`drawScreen` and adding commands; the model,
the engine API and the persistence do not need to change.

Web API: `GET /api/state` · `GET /api/samples` · `POST /api/track/sample` ·
`POST /api/track/macro` · `POST /api/upload?name=` · `POST /api/project/{save,load,rename,delete}`
· `POST /api/transport` · `POST /api/fx/{slot,reroll,lock,track}` · `POST /api/rescan`.

Engine OSC API (sclang, port 57120): `/gr/ping`, `/gr/load`, `/gr/param`, `/gr/shape`,
`/gr/mute`, `/gr/run`, `/gr/master`, `/gr/reverb`, `/gr/delay`, `/gr/free`, `/gr/route`,
`/gr/fxadd`, `/gr/fxfree`, `/gr/fxgain`, `/gr/panic`. Telemetry back
to the controller (port 57141): `/gr/ready`, `/gr/cpu`, `/gr/loaded`, `/gr/loadfail`,
`/gr/head`, `/gr/meter`, `/gr/master`.

State is persisted to `state/model.json` and restored on launch — everything except the
scrub head, which returns to zero, exactly as the desktop app does.

## 6. Deploying

```bash
./move/deploy.sh move.local
```

Then on the Move: **Schwung → overtake → Granola**.

## 7. Device-specific findings

These were all measured on the device, and they are the reasons for the few places where
this differs from the desktop build.

**No SuperCollider bundle ships with Granola.** Every takeover on this box already carries
the same ~200-plugin SC build. `run-engine.sh` probes for one (`$GR`, then poundhard,
onemanshow, wildrider, interwoven) and reuses it. Granola's only requirement beyond core SC
is JPverb, which those bundles have.

**scsynth, not supernova.** `JPverb_supernova.so` on this device does not register its UGen:
under supernova, loading a SynthDef that uses JPverb gives `Unit generator JPverbRaw not
installed` and the reverb node is never created, while the identical test under scsynth
builds and runs it. Granola's master reverb *is* JPverb, so supernova would cost the reverb
outright — a change to the ported sound, not a performance trade. `GR_THREADS=3` forces
supernova if you ever want the extra cores and can accept a silent reverb bus.

**Measured load**: 8 voices + reverb + delay + limiter = 21 synths, 1252 UGens, ~34 % of one
core, 1 xrun (at startup). Density and grain size are the expensive controls and their full
desktop ranges are preserved, so the top of the density range across all eight tracks will
cost more than this.

**Three sclang traps, all hit and all worked around** (documented at the code):

1. A `Routine` + `s.sync` forked from an OSC responder **deadlocks** — the fork is entered
   and the `/synced` reply never resumes it. Loads stalled forever, logging nothing.
2. `Buffer.readChannel(..., action, bufnum)` never fires its action under this sclang
   (3.13): `/b_query` showed the buffer still unallocated and the server sent nothing.
   Buffer reads therefore use the raw `/b_allocReadChannel` plus an explicit `/sync`, whose
   reply is the completion edge — a callback, not a wait.
3. `/sync` ids must come from `UniqueID.next`. `Server:sync` draws from the same counter, so
   a private sequence starting at 1000 collided head-on and SC's own boot-time syncs fired
   Granola's load callbacks before the graph existed.

**The handshake must not lie.** The engine's OSC handlers are live as soon as the file is
evaluated, roughly 15 seconds before `waitForBoot` has a server. Answering `/gr/ping` in
that window told the controller the engine was up, so it pushed the whole model — samples
included — at a server that did not exist, and never pushed again because the handshake was
already satisfied. Every track stayed silent with nothing logged. `~grReady` now gates the
answer, and a command that arrives early is dropped *loudly*.

**Two process-lifecycle traps.** `setsid` fails when called from a process that is already
a session leader, so `run-controller.sh` calling it *again* under `run-stack.sh` (which
already detaches it) killed the controller seconds after it had logged a clean startup —
the inner call is gone. And the stale-queue guard on `control.json` used "the first thing I
read" as its test for staleness; since `stop-stack.sh` deletes that file, the first thing
read after a normal restart is the performer's *first real gesture*, which was then
silently swallowed. It now compares the file's mtime against the controller's start time.

**Samples: one folder, and it is Granola's own.** The library is
`/data/UserData/granola/samples` and nothing else — not the Move's user library, not the
sibling takeovers' audio. This stack's material is what you put in it, uploaded through the
web UI or copied into that folder (subfolders are scanned, so organise it as you like).

The restriction is *enforced*, not merely defaulted. A track sample outside the library is
dropped when state or a project is loaded (as is one whose file no longer exists), and the
API rejects an out-of-library path — so
it also holds for state and projects written before the rule existed, and for a project
file copied in from elsewhere. Otherwise a stale state file quietly reloads another
takeover's audio on the next launch and the rule is a comment rather than a rule.

There is also **no auto-assignment**: tracks start empty and stay empty until you put a
sample on one. Filling eight columns with whatever eight files happened to sort first is a
guess about the music, and this stack's content is chosen, not discovered.

Only formats libsndfile opens directly are offered — the desktop app's AVFoundation
transcode path has no equivalent here, and offering a file the server cannot read would
just produce a silent track.

## 8. Not in this iteration

* **FX in project files** — locked chains persist in memory but a saved project does not
  yet carry the rack.
* **Per-link FX editing** — the app can set an individual link's parameters and blend;
  here a chain is rolled, locked, kept or re-rolled. View 2's fourth row and View 4's middle rows
  are unlit for the same reason: nothing is there yet.
* **The master effects' own parameters** (decay, size, delay time, feedback…) — the sends
  are live and the effects run at their defaults; a surface for the effects themselves is
  a later pass.
* **The Airwindows chain.** `GranolaAirwindows.scx` is a macOS build; running it here needs
  an ARM64 Linux cross-build of the plugin. The performance group, its head and its
  terminator *are* constructed exactly as on the desktop, so links can be inserted later
  without touching the engine.

* `s.numSynths` reads 0 in this sclang while the server really has 21, so the status file
  reports the loaded-track count instead of a node count rather than publish a number known
  to be wrong.
