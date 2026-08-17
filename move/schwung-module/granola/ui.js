// Granola — Schwung overtake runner (eight-track granular synthesiser).
//
// VIEW 1 (default, Track button 1): the hardware IS eight vertical control strips.
// One encoder at the top of a column, the four pads beneath it, one Granola track per
// column — Encoder 1 + its pads = Track 1, and so on for all eight, identically.
//
//   Encoder n            with NO pad lit in its column: the track's sample scan /
//                        playhead. This is the default state, and it is PER COLUMN —
//                        another track's pads never change this encoder's role.
//                        With pads lit: those parameters, moved together as a macro.
//   Pads (top->bottom)   SIZE / DENSITY / JITTER / PITCH. Persistent toggles, NOT
//                        mutually exclusive: light several and the encoder drives them
//                        all; turn them all off and the encoder is the playhead again.
//   16 step buttons      a 16-segment position bar for the track whose encoder was
//                        last touched or turned (step 1 = start of sample, 16 = end).
//   Track buttons 1-4    view selectors. 1 = this view (default), 2 and 3 below; 4 reserved.
//   Play                 transport: start / stop every granulator together.
//   Master knob          output level.  Jog wheel = next/previous sample on the
//                        focused track.  Back = ARMS the exit confirmation.
//
// VIEW 2 (Track button 2): the same eight columns, further parameters — one per pad row.
//   Row 1 = DRIFT, row 2 = SPREAD, row 3 = GRAIN SHAPE — a continuous morph of the grain
//   window through gaussian -> percussive -> plateau -> reverse. Row 4 is unlit because
//   there is nothing on it yet.
//
// VIEW 3 (Track button 3): the mixer and the master-effect sends, same columns again.
//   Row 1 = VOLUME, row 2 = PAN (centre-detented), row 3 = DELAY SEND, row 4 = REVERB
//   SEND. The two master effects themselves keep their defaults for now; a view for
//   their own parameters comes later.
//
// Selectors ACCUMULATE across all three views: Size in View 1 plus Shape in View 2 plus
// Reverb Send in View 3 gives one encoder that moves all three at once.
//
// VIEW 4 (Track button 4): the performance FX — Airwindows, as in the desktop app.
//   Row 1  which TRACKS are routed through the chain, each in its own hue. With NONE
//          routed the rack is silent by construction — the links are not in the graph at
//          all — and armed slots BREATHE to say so.
//   Row 4  four CHAIN SLOTS. A short press toggles a slot, and turning one ON is what
//          ASSEMBLES its chain — one to four Airwindows effects at random, no effect
//          twice — so a short press off and on again is how a chain is re-rolled. Slots
//          stack in the order they were switched on, which is the order they appear in
//          the signal.
//          HOLD a slot + turn the JOG to ride that chain's DRY/WET, every link at once.
//          The hold is only a modifier; it does nothing on its own.
//          SHIFT + slot LOCKS it (cyan instead of amber): a locked slot keeps its chain
//          and its parameters, so switching it off and on again brings the same sound
//          back rather than a new roll. Shift + slot again frees it.
//   The encoders keep doing what they do everywhere else (the selector set is global),
//   so a hand on an encoder behaves the same whichever view is open.
//
// HARVESTER (SHIFT + Track 4): pulls fresh material off YouTube and cuts it into samples.
//   Row 1   the eight TRACKS, MOMENTARY — a destination to tap, not a state.
//   Rows 2-3  the sixteen SAMPLES of the last batch.
//   Row 4 pad 1  GENERATE, momentary; it BLINKS while a batch is coming in, and the
//                screen carries a full-width progress bar.
//   Jog     how many samples one batch brings back, 1..16.
//   To place a sample: HOLD its pad, then short-press the track pad it should go to.
//   Any Track button leaves the harvester.
//
// PROJECTS (Menu button): the 32 pads are 32 project slots — a project being all eight
// tracks, their samples, every parameter and every selector across all views.
//   * short press        LOAD that slot
//   * SHIFT + pad        SAVE the live machine into that slot
//   * hold               SAVE as well, for a hand that is not on Shift
//   Either save BLINKS the pad while it is in flight, so the gesture reports itself, and
//   the slot then stays lit as occupied.
//
// The UI owns NO synthesis state. Every gesture is a command written to control.json;
// the Python controller owns the Granola model and publishes status.json, which is
// what the pads, the step bar and the screen render. Redrawing LEDs, switching views
// or relaunching this module therefore cannot reset a parameter.

import {
    Black, White, DarkGrey, BrightGreen,
    MoveShift, MoveBack, MoveMenu, MovePlay, MoveRow1, MoveRow2, MoveRow3, MoveRow4,
    MoveKnob1, MoveKnob1Touch, MoveKnob8Touch,
    MoveMaster, MoveMasterTouch, MoveMainKnob, MoveMainButton, MoveRec
} from '/data/UserData/move-anything/shared/constants.mjs';
import { setLED, setButtonLED, decodeDelta } from '/data/UserData/move-anything/shared/input_filter.mjs';

const GR = '/data/UserData/granola';
const MODULE_DIR = '/data/UserData/schwung/modules/overtake/granola';
const HOOKS_DIR = '/data/UserData/schwung/hooks';
/* IPC under /data/UserData: the Schwung host only reads files there, and reading
 * through a tmpfs symlink hangs the host — so these are plain files on /data. */
const STATUS_FILE = GR + '/ipc/status.json';
const CONTROL_FILE = GR + '/ipc/control.json';
const HB_FILE = GR + '/ipc/ui_hb.txt';

const N_TRACKS = 8, N_STEPS = 16;
/* Every parameter a column's encoder can be pointed at, in the controller's slot order
 * (MACRO_SLOTS in params.py). View 1 owns slots 0-3, one per pad row; View 2 continues
 * from slot 4, also one per row. The set GROWS across views rather than being replaced —
 * a track lit for Size in View 1 and Spread in View 2 has an encoder that macros over
 * both, and leaving a view does not hand the encoder back to the playhead. */
const N_SLOTS = 11;
/* Which slot each view's pad rows carry, top row first. -1 = a row with nothing on it
 * yet, left unlit rather than given something invented. Mirrors VIEW_ROWS in params.py. */
const VIEW_ROWS = {
    1: [0, 1, 2, 3],      /* size, density, jitter, pitch */
    2: [4, 5, 6, -1],     /* drift, spread, grain-shape morph */
    3: [7, 8, 9, 10]      /* volume, pan, delay send, reverb send */
};
const N_VIEWS = 4;
/* VIEW 4 is not a parameter view: its rows are a track picker and four FX chain slots,
 * so it has its own layout rather than an entry in VIEW_ROWS. */
const V_FX = 4;
/* THE HARVESTER — Shift + Track 4. Not one of the four Track-button views: it is a mode
 * reached from one, and any Track button leaves it. */
const V_HARV = 5;
const HARV_SAMPLES = 16;        /* rows 2-3 hold one batch */
/* Sample pads are GREEN — nothing else in Granola uses it, so a harvested batch reads as
 * its own thing rather than as tracks or chains. The generate pad is red while idle and
 * blinks white while a batch is coming in. */
const HARV_ON = 11, HARV_OFF = 83;      /* neon green / dark green */
const HARV_GEN = 5, HARV_BUSY = White;  /* light yellow idle / white blink */
const FX_SLOTS = 4;
/* Chain slots are deliberately OUTSIDE the eight track hues — a slot is not a track.
 * TWO hue families, so free and locked are told apart at a glance rather than by
 * brightness alone: AMBER = free to re-roll, CYAN = locked. Within each family the lit
 * index means the chain is running and the dark one of the same hue means it is not,
 * which is the same rule the track columns use. */
const FX_ON = 3, FX_OFF = 75;                           /* amber  #FF9900 / #403302 */
const FX_LOCK_ON = 14, FX_LOCK_OFF = 90;                /* cyan   #00FFFF / #030D0A */
/* A slot with a finger on it goes WHITE, whichever family it belongs to. Holding is a
 * MODIFIER — it arms the jog to ride that chain's dry/wet — and a modifier that leaves no
 * mark is one you cannot tell you have engaged. White is used nowhere else in this view,
 * so it reads as "this pad, right now" rather than as a state of the chain. */
const FX_HELD = White;
/* Pad matrix, top row first. Column c of row r is PAD_NOTES[r * 8 + c], so a track's
 * four pads are the four entries with the same c — the layout the whole view rests on. */
const PAD_NOTES = [
    92, 93, 94, 95, 96, 97, 98, 99,
    84, 85, 86, 87, 88, 89, 90, 91,
    76, 77, 78, 79, 80, 81, 82, 83,
    68, 69, 70, 71, 72, 73, 74, 75
];
const NOTE_TO_CELL = {};
for (let i = 0; i < 32; i++) NOTE_TO_CELL[PAD_NOTES[i]] = i;
const STEP_BASE = 16;

const SLOT_NAMES = ['SIZE', 'DENS', 'JIT', 'PITCH', 'DRIFT', 'SPRD', 'SHAPE',
                    'VOL', 'PAN', 'DLY', 'RVB'];

/* ---- track colours -------------------------------------------------------------
 * The Move's LEDs take a PALETTE INDEX, not RGB — so a track's identity has to be a
 * pair of indices chosen from the hardware's own ramps, not a computed shade. Each
 * track gets one hue as [lit, dark]: the lit index for a pad that is toggled ON, the
 * dark index of the SAME hue for OFF. That makes all three readings work at once —
 * the eight columns are eight different hues, a column's four pads are visibly one
 * family, and on/off is a brightness difference within that family.
 * The pairs run left to right through the spectrum so column order is itself legible.
 * Both indices of a pair were chosen by reading the palette's ACTUAL hex values rather
 * than by name: the eight hues sit 24-67 degrees apart, and every dark index is the
 * same hue as its lit partner. Two earlier picks failed that test — index 84 is not in
 * the palette at all (it would have rendered as fallback grey), and "lime" 31 is
 * #4A8700, near enough to green 8 (#56BF13) that two adjacent columns read alike. */
const TRACK_COL = [
    [1, 66],    /* T1 red      #FF2424 / #1A0404   hue 0   */
    [3, 75],    /* T2 orange   #FF9900 / #403302   hue 36  */
    [7, 78],    /* T3 yellow   #FFFF00 / #1A1A00   hue 60  */
    [8, 79],    /* T4 green    #56BF13 / #1C4007   hue 97  */
    [12, 87],   /* T5 teal     #159573 / #073327   hue 164 */
    [16, 95],   /* T6 blue     #274FCC / #0C1940   hue 225 */
    [20, 104],  /* T7 violet   #8700FF / #0D001A   hue 272 */
    [23, 110]   /* T8 pink     #FF0099 / #1A000F   hue 324 */
];
const VIEW_RESERVED = DarkGrey;   /* a view button that is closed, or not yet implemented */

/* ---- projects view ----
 * Slot colours are deliberately OUTSIDE the eight track hues: a project is not a track,
 * and reusing a track's colour here would read as "this slot belongs to track 3".  */
const N_PROJ = 32;
/* Occupied has to be unmistakable at a glance, not a shade of empty: Royal Blue (16,
 * #274FCC) against Dark Blue (95, #0C1940) was two dim blues. Azure (15, #0074FC) is the
 * same hue family at nearly full value, so a filled slot reads as LIT and a free one as
 * almost unlit. */
const PROJ_FILLED = 15;      /* Azure Blue  — holds a project */
const PROJ_EMPTY = 95;       /* Dark Blue   — free slot */
const PROJ_CUR = White;      /* the slot currently loaded, or just saved into */
const PROJ_SAVE = 7;         /* Vivid Yellow — blinks against White while saving */
const SAVE_HOLD_MS = 420;    /* press longer than this and it is a save, not a load */

/* ---- runtime state (a MIRROR of status.json, never the owner) ---- */
let phase = 0, launched = false, lastStatusAt = -100;
let ready = false, engine = false, cpu = 0, poolSize = 0, notice = '';
let focus = 0, master = 0.8;
/* --- playhead gesture loops --- */
let recHeld = false;          /* Rec is down and we are capturing */
let recTrack = null;          /* which track the controller says it is recording */
let recLoops = new Array(N_TRACKS).fill(false);
let masterTouched = false;    /* volume knob touch, for the clear-everything chord */
let recConsumed = false;      /* a chord fired on this Rec press: no record on release */
let names = new Array(N_TRACKS).fill('—');
let loaded = new Array(N_TRACKS).fill(false);
let sel = [];                     /* [track][slot] = selector toggled on, across all views */
let head = new Array(N_TRACKS).fill(0);
let labels = new Array(N_TRACKS).fill('Position');
let readouts = new Array(N_TRACKS).fill('0%');
for (let i = 0; i < N_TRACKS; i++) sel.push(new Array(N_SLOTS).fill(false));

let view = 1;                     /* Track buttons 1-4 select a view; 3 and 4 are reserved */
let running = false;              /* transport, mirrored from the controller */
/* Back ARMS this rather than exiting. The prompt is MODAL and has no timeout: it stays up
 * until the performer actually decides, so Back can never drop them out of Granola by
 * accident mid-set. Same gesture as PoundHard and OneManShow. */
let exitConfirm = false;
/* View 4 state, mirrored from the controller. */
let fxSlots = [false, false, false, false];
let fxLocked = [false, false, false, false];
let fxLabels = ['—', '—', '—', '—'];
let fxTracks = [];
let fxSummary = 'no chain';
let fxCount = 0;
/* Whether the rack is actually AUDIBLE — it only is while at least one track is routed
 * into it. An armed slot with nothing routed is legitimate, but it must not look
 * identical to one that is making sound. */
let fxLive = false;
let fxWet = [0.4, 0.4, 0.4, 0.4];
/* ---- harvester, mirrored from the controller ---- */
let harvRunning = false, harvProgress = 0, harvStage = 'idle', harvNote = '';
let harvBatch = 4, harvNames = [];
/* The sample pad under the finger: hold one, then tap a track pad to assign it. */
let harvHeld = -1, harvSecs = [];

/* Holding a sample pad auditions it. `harvHeld` doubles as the audition state because the
 * two are the same gesture: hold to hear it, tap a track to place it. Releasing the pad
 * stops the sound, so the pad behaves like a monitor button rather than a launcher. */
function releaseAudition() {
    if (harvHeld >= 0) { harvHeld = -1; sendCmd('auditionoff', -1); ledDirty = true; screenDirty = true; }
}
let harvFlash = -1, harvFlashUntil = 0;   /* the track pad just assigned to */
/* Which slot's dry/wet the jog is riding, and how long the readout stays up after the
 * last detent. */
let wetShow = -1, wetShowUntil = 0;
/* The slot pad under the finger. Holding one is a MODIFIER — it arms the jog to ride
 * that chain's dry/wet — and `fxConsumed` records that the hold did something, so the
 * release does not also toggle the slot. */
let fxHeld = -1, fxConsumed = false;
let projView = false;
let projFilled = new Array(N_PROJ).fill(false);
let projCur = -1;
/* Which slot the controller says it is saving (-1 = none). The blink is driven by the
 * CONTROLLER's window, not by a local timer: the pad then reports the state of the real
 * save rather than the state of the button press. */
let saving = -1;
/* The optimistic blink must survive the ~170ms until the next status read, or the very
 * first read after the press — which still says "not saving" — would cancel it and the
 * pad would flicker instead of confirming. */
let savingLocalUntil = 0;
/* The pad under the finger in the projects view, and when it went down. */
let projHeld = -1, projHeldAt = 0, projConsumed = false;
let shiftHeld = false;
let seq = 0, cmdQueue = [], controlDirty = false;
let overlay = null, overlayUntil = -1;
let ledDirty = true, screenDirty = true, lastLedSig = '', lastDrawAt = -100;
/* Transient: which column the screen is showing a value for, and until when. */
let showUntil = 0;

/* ---------------------------------------------------------------------------------
 * OPTIMISTIC PLAYHEAD. status.json arrives at ~12Hz and the round trip through the
 * controller is longer than a fast encoder sweep, so the position bar would lag the
 * hand that is moving it. The encoder's own delta is therefore applied locally and
 * the engine's reported head (which is authoritative — it includes any free-running
 * scan) overwrites it whenever the column is not actively being turned.
 * ------------------------------------------------------------------------------- */
let headLocal = new Array(N_TRACKS).fill(0);
let headTouched = new Array(N_TRACKS).fill(0);   /* Date.now() of the last local move */
const HEAD_HOLD_MS = 400;

function clampf(x, lo, hi) { return x < lo ? lo : x > hi ? hi : x; }

/* Larger sweeps for faster spins without losing fine control — the same curve the
 * desktop app applied (GranolaModel.encoderDelta). Kept here as well as in the
 * controller only so the LOCAL playhead estimate moves by exactly what the model
 * will move by; the controller remains the one that actually changes the value. */
function encoderDelta(ticks) {
    const m = Math.abs(ticks);
    const sign = ticks > 0 ? 1 : (ticks < 0 ? -1 : 0);
    return sign * 0.004 * m * (1 + m * 0.35);
}

/* Any selector lit ANYWHERE decides this column's encoder — the role is per track, not
 * per view, so leaving View 2 does not silently hand the encoder back to the playhead. */
function anySelected(t) {
    for (let k = 0; k < N_SLOTS; k++) if (sel[t][k]) return true;
    return false;
}

function sys(cmd) { if (typeof host_system_cmd === 'function') host_system_cmd(cmd); }
function showAction(label) { overlay = label; overlayUntil = phase + 24; screenDirty = true; }

/* ---- control.json (ui.js -> controller), queued so rapid gestures aren't lost ---- */
function writeControl() {
    if (typeof host_write_file !== 'function') return;
    host_write_file(CONTROL_FILE, JSON.stringify({ seq: seq, cmds: cmdQueue }));
}
function sendCmd(cmd, arg, p) {
    seq++;
    const entry = { seq: seq, cmd: cmd, arg: arg };
    if (p) entry.p = p;
    cmdQueue.push(entry);
    if (cmdQueue.length > 32) cmdQueue = cmdQueue.slice(-32);
    /* Coalesce: flag dirty and let tick() flush at most once per frame. An encoder
     * sweep otherwise bursts host_write_file calls, and every write is a chance to hit
     * the SD I/O stall that freezes the host's tick. The queue + seq dedup on the
     * controller side make the batching lossless. */
    controlDirty = true;
}

/* ---- big block-glyph renderer (values must be readable at a glance) ---- */
const FONT = {
    '0': ['###', '# #', '# #', '# #', '###'], '1': [' # ', '## ', ' # ', ' # ', '###'],
    '2': ['###', '  #', '###', '#  ', '###'], '3': ['###', '  #', ' ##', '  #', '###'],
    '4': ['# #', '# #', '###', '  #', '  #'], '5': ['###', '#  ', '###', '  #', '###'],
    '6': ['###', '#  ', '###', '# #', '###'], '7': ['###', '  #', '  #', '  #', '  #'],
    '8': ['###', '# #', '###', '# #', '###'], '9': ['###', '# #', '###', '  #', '###'],
    'A': [' # ', '# #', '###', '# #', '# #'], 'B': ['## ', '# #', '## ', '# #', '## '],
    'C': ['###', '#  ', '#  ', '#  ', '###'], 'D': ['## ', '# #', '# #', '# #', '## '],
    'E': ['###', '#  ', '## ', '#  ', '###'], 'F': ['###', '#  ', '## ', '#  ', '#  '],
    'G': ['###', '#  ', '# #', '# #', '###'], 'H': ['# #', '# #', '###', '# #', '# #'],
    'I': ['###', ' # ', ' # ', ' # ', '###'], 'J': ['  #', '  #', '  #', '# #', '###'],
    'K': ['# #', '# #', '## ', '# #', '# #'], 'L': ['#  ', '#  ', '#  ', '#  ', '###'],
    'M': ['# #', '###', '###', '# #', '# #'], 'N': ['# #', '###', '###', '###', '# #'],
    'O': ['###', '# #', '# #', '# #', '###'], 'P': ['###', '# #', '###', '#  ', '#  '],
    'Q': ['###', '# #', '# #', '###', '  #'], 'R': ['## ', '# #', '## ', '# #', '# #'],
    'S': ['###', '#  ', '###', '  #', '###'], 'T': ['###', ' # ', ' # ', ' # ', ' # '],
    'U': ['# #', '# #', '# #', '# #', '###'], 'V': ['# #', '# #', '# #', '# #', ' # '],
    'W': ['# #', '# #', '###', '###', '# #'], 'X': ['# #', ' # ', ' # ', ' # ', '# #'],
    'Y': ['# #', '# #', ' # ', ' # ', ' # '], 'Z': ['###', '  #', ' # ', '#  ', '###'],
    '-': ['   ', '   ', '###', '   ', '   '], '.': ['   ', '   ', '   ', '   ', ' # '],
    '+': ['   ', ' # ', '###', ' # ', '   '], '%': ['# #', '  #', ' # ', '#  ', '# #'],
    '/': ['  #', '  #', ' # ', '#  ', '#  '], ':': ['   ', ' # ', '   ', ' # ', '   '],
    ' ': ['   ', '   ', '   ', '   ', '   ']
};
function drawBig(text, yTop, maxScale) {
    if (typeof fill_rect !== 'function') return;
    text = String(text).toUpperCase();
    const n = text.length || 1;
    const scale = Math.max(2, Math.min(maxScale || 11, Math.floor(122 / (4 * n - 1))));
    const gw = 3 * scale, gap = scale, totalW = n * gw + (n - 1) * gap;
    const x0 = Math.max(0, Math.floor((128 - totalW) / 2));
    for (let i = 0; i < text.length; i++) {
        const g = FONT[text[i]] || FONT[' '];
        const gx = x0 + i * (gw + gap);
        for (let r = 0; r < 5; r++) {
            const row = g[r];
            let c = 0;
            while (c < 3) {                       /* contiguous '#' as one rect = fewer host calls */
                if (row.charCodeAt(c) === 35) {
                    const s = c;
                    while (c < 3 && row.charCodeAt(c) === 35) c++;
                    fill_rect(gx + s * scale, yTop + r * scale, (c - s) * scale, scale, 1);
                } else { c++; }
            }
        }
    }
}

/* Saving a project. One function for both gestures — shift+pad and hold — so the two
 * can never drift apart in what they do or what they show. */
function saveProject(slot) {
    saving = slot;                        /* optimistic; the controller confirms */
    savingLocalUntil = phase + 20;
    projFilled[slot] = true;              /* the pad lights as occupied straight away */
    sendCmd('saveproj', slot);
    showAction('SAVED ' + (slot + 1));
    ledDirty = true; screenDirty = true;
}

/* ---- LEDs ---- */
function btnLED(cc, color) { try { setButtonLED(cc, color); } catch (e) {} }

/* A track's effective playhead: the local estimate while the column is being turned,
 * the engine's reported head otherwise. */
function trackHead(t) {
    return (Date.now() - headTouched[t]) < HEAD_HOLD_MS ? headLocal[t] : head[t];
}

/* Every view draws the same way: one parameter per row, each column in its track's hue,
 * lit when that selector is on. The only thing a view changes is WHICH parameters the
 * rows carry, so there is one renderer rather than one per view. */
/* View 4: row 1 picks which tracks are routed through the performance chain, row 4's
 * first four pads are the chain slots. The two middle rows are dark — the FX view is a
 * routing surface, not a parameter grid. */
function renderPadsFX() {
    for (let t = 0; t < N_TRACKS; t++) {
        const pair = TRACK_COL[t];
        /* A routed track lights in its OWN hue, so the picker reads as "these columns",
         * matching every other view's column identity. */
        setLED(PAD_NOTES[t], fxTracks.indexOf(t) >= 0 ? pair[0] : pair[1]);
    }
    for (let c = 8; c < 24; c++) setLED(PAD_NOTES[c], Black);
    for (let c = 24; c < 32; c++) {
        const slot = c - 24;
        let color = Black;
        if (slot < FX_SLOTS) {
            if (fxLocked[slot]) color = fxSlots[slot] ? FX_LOCK_ON : FX_LOCK_OFF;
            else color = fxSlots[slot] ? FX_ON : FX_OFF;
            /* Armed but silent — no track routed — BREATHES between its lit and dark
             * shade instead of sitting steady. The slot is on, the chain is held, and
             * nothing is being processed: that is a real state and it needs its own
             * look, or a lit pad that makes no sound reads as a fault. */
            if (fxSlots[slot] && !fxLive) {
                const pair = fxLocked[slot] ? [FX_LOCK_ON, FX_LOCK_OFF] : [FX_ON, FX_OFF];
                color = (phase % 24 < 12) ? pair[0] : pair[1];
            }
            /* A finger on the pad wins over everything else that pad might be saying: the
             * performer needs to know the modifier is engaged BEFORE reaching for the jog,
             * not after turning it. */
            if (slot === fxHeld) color = FX_HELD;
        }
        setLED(PAD_NOTES[c], color);
    }
}

/* The harvester: eight track pads on top, sixteen sample pads across the middle, and the
 * generate button alone at the bottom left. */
function renderPadsHarv() {
    for (let t = 0; t < N_TRACKS; t++) {
        const pair = TRACK_COL[t];
        /* Track pads are MOMENTARY here — a destination to tap, not a state to hold — so
         * they sit at their track's hue. BRIGHT means that track already holds a sample,
         * dark means it is empty: the performer can see at a glance which parts are still
         * waiting for material, without leaving the harvester to check. White is the
         * flash confirming a sample has just landed there. */
        setLED(PAD_NOTES[t], t === harvFlash ? White : (loaded[t] ? pair[0] : pair[1]));
    }
    for (let i = 0; i < HARV_SAMPLES; i++) {
        const has = i < harvNames.length;
        /* White while held: the pad is both auditioning and armed for assignment, and one
         * colour is right for that because it is one gesture. */
        setLED(PAD_NOTES[8 + i],
               i === harvHeld ? White : (has ? HARV_ON : HARV_OFF));
    }
    for (let c = 24; c < 32; c++) {
        let color = Black;
        if (c === 24) {
            color = harvRunning ? ((phase % 8 < 4) ? HARV_BUSY : HARV_GEN) : HARV_GEN;
            if (!harvRunning && harvStage === 'failed') color = 1;   /* bright red */
        }
        setLED(PAD_NOTES[c], color);
    }
}

function renderPads() {
    if (view === V_HARV) { renderPadsHarv(); return; }
    if (view === V_FX) { renderPadsFX(); return; }
    const rows = VIEW_ROWS[view] || VIEW_ROWS[1];
    for (let row = 0; row < 4; row++) {
        const slot = rows[row];
        for (let t = 0; t < N_TRACKS; t++) {
            const pair = TRACK_COL[t];
            setLED(PAD_NOTES[row * 8 + t],
                   slot < 0 ? Black : (sel[t][slot] ? pair[0] : pair[1]));
        }
    }
}

/* The 16 step buttons: a position bar for the FOCUSED track, IN THAT TRACK'S COLOUR.
 *
 * The colour is half the information. A bar that is only a bar says where the playhead
 * is; a bar in the track's own hue also says WHOSE playhead it is — so touching an
 * encoder tells the performer which track they are about to move and where that track
 * currently sits, in one glance and without looking at the screen.
 *
 * Both halves of the bar are coloured: filled segments in the lit hue, the remainder in
 * the same hue's dark index. That keeps the track's identity legible across the whole
 * strip even when the head is at the very start, where a single lit segment would be all
 * there is to go on. */
/* The Rec button says three different things:
 *   blinking red   - a gesture is being recorded right now
 *   track colour   - the ACTIVE track has a loop running
 *   white          - some OTHER track has a loop, and the active one does not
 * so a glance tells the performer both that loops exist and whether the track under
 * their hands is one of them. */
function renderRec() {
    const t = clampf(focus, 0, N_TRACKS - 1) | 0;
    let col = Black;
    if (recTrack !== null) {
        col = (phase % 8 < 4) ? 1 : Black;        /* 1 = bright red */
    } else if (recLoops[t]) {
        col = TRACK_COL[t][0];
    } else if (recLoops.some(function (x, i) { return x && i !== t; })) {
        col = White;
    }
    setButtonLED(MoveRec, col);
}

function renderSteps() {
    const t = clampf(focus, 0, N_TRACKS - 1) | 0;
    const pair = TRACK_COL[t];
    const pos = clampf(trackHead(t), 0, 0.99999);
    const seg = Math.floor(pos * N_STEPS);
    for (let i = 0; i < N_STEPS; i++) setLED(STEP_BASE + i, i <= seg ? pair[0] : pair[1]);
}

/* The 32 pads as 32 project slots. The slot being saved BLINKS between white and
 * yellow: a save is a few milliseconds of JSON, far too fast to see, and "did that
 * take?" is exactly the question a performer needs answered mid-set. */
function renderProjPads() {
    for (let i = 0; i < N_PROJ; i++) {
        let color;
        /* ~5Hz, White against Vivid Yellow: a save takes milliseconds, so this window is
         * held open by the controller purely so the gesture can be SEEN to have worked. */
        if (i === saving) color = (phase % 6 < 3) ? White : PROJ_SAVE;
        else if (i === projCur) color = PROJ_CUR;
        else color = projFilled[i] ? PROJ_FILLED : PROJ_EMPTY;
        setLED(PAD_NOTES[i], color);
    }
}

function renderLEDs() {
    if (projView) {
        renderProjPads();
        /* The step buttons belong to View 1's playhead; in the projects view they would
         * be reporting a track nobody is looking at, so they go dark. */
        for (let i = 0; i < N_STEPS; i++) setLED(STEP_BASE + i, Black);
        btnLED(MoveRow1, VIEW_RESERVED); btnLED(MoveRow2, VIEW_RESERVED);
        btnLED(MoveRow3, VIEW_RESERVED); btnLED(MoveRow4, VIEW_RESERVED);
        btnLED(MoveMenu, White);
        btnLED(MovePlay, running ? BrightGreen : Black);
        renderRec();
        return;
    }
    btnLED(MoveMenu, Black);
    renderPads();
    renderSteps();
    /* Track buttons are the view selectors: the open one is white, the others dim. Views
     * 3-4 are reserved and shown in the palette's "you cannot have this yet" grey rather
     * than left dark, which would read as broken. */
    btnLED(MoveRow1, view === 1 ? White : VIEW_RESERVED);
    btnLED(MoveRow2, view === 2 ? White : VIEW_RESERVED);
    btnLED(MoveRow3, view === 3 ? White : VIEW_RESERVED);
    btnLED(MoveRow4, (view === V_FX || view === V_HARV) ? White : VIEW_RESERVED);
    btnLED(MovePlay, running ? BrightGreen : Black);
    renderRec();
}

/* ---- screen ---- */
function drawScreen() {
    if (typeof clear_screen !== 'function') return;
    clear_screen();

    /* Modal, and first: nothing behind it may draw over the decision. */
    if (exitConfirm) {
        drawBig('EXIT', 3, 5);
        drawBig('YES?', 31, 5);
        print(0, 58, 'JOG PUSH = EXIT   BACK = STAY', 1);
        return;
    }

    if (!launched || !engine) {
        print(0, 12, 'GRANOLA', 2);
        print(0, 40, launched ? 'starting engine...' : 'starting...', 1);
        return;
    }
    if (!ready) {
        print(0, 12, 'GRANOLA', 2);
        print(0, 40, 'engine booting...', 1);
        return;
    }
    if (overlay) {
        print(0, 12, 'GRANOLA', 2);
        print(0, 40, overlay, 1);
        return;
    }

    if (projView) {
        let n = 0;
        for (let i = 0; i < N_PROJ; i++) if (projFilled[i]) n++;
        if (saving >= 0) {
            print(0, 0, 'PROJECTS', 1);
            drawBig('SAVE ' + (saving + 1), 18, 9);
            return;
        }
        print(0, 0, 'PROJECTS', 1);
        print(0, 18, n + '/32 saved' + (projCur >= 0 ? ('   IN ' + (projCur + 1)) : '   unsaved'), 1);
        print(0, 34, 'tap = load   shift+pad = save', 1);
        print(0, 48, 'hold = save too (pad blinks)', 1);
        return;
    }

    if (view === V_HARV) {
        print(0, 0, 'HARVEST', 1);
        print(84, 0, harvNames.length + '/' + harvBatch, 1);
        if (harvRunning) {
            /* A VERY VISIBLE bar: the whole width, tall enough to read across a room,
             * with the percentage in the same oversized block font every other value in
             * Granola uses. A harvest takes tens of seconds — the screen has to make it
             * obvious that something is happening and how far along it is. */
            drawBig(Math.round(harvProgress * 100) + '%', 14, 9);
            if (typeof fill_rect === 'function') {
                fill_rect(0, 40, 128, 12, 1);
                fill_rect(1, 41, 126, 10, 0);
                fill_rect(2, 42, Math.max(0, Math.round(124 * harvProgress)), 8, 1);
            }
            print(0, 55, harvNote.substring(0, 21), 1);
        } else if (harvStage === 'failed') {
            /* A harvest that died has to say so. The first version showed the bar only
             * while running, so a batch that failed on its first step left the screen
             * exactly as it was — indistinguishable from the pad doing nothing. */
            drawBig('FAILED', 14, 9);
            print(0, 44, harvNote.substring(0, 21), 1);
            print(0, 55, 'pad 25 = try again', 1);
        } else if (harvHeld >= 0 && harvHeld < harvNames.length) {
            /* While auditioning, the screen answers the question the ear is already
             * asking: which one is this, and how long is it. The batch size can wait —
             * it is not what the performer is looking at mid-hold. */
            drawBig('S' + (harvHeld + 1), 12, 9);
            print(46, 16, (harvSecs[harvHeld] || 0).toFixed(1) + 's', 1);
            print(0, 42, harvNames[harvHeld].substring(0, 21), 1);
            print(0, 55, 'tap a track to assign', 1);
        } else {
            drawBig(String(harvBatch), 12, 11);
            print(0, 42, harvNames.length ? (harvNames.length + ' SAMPLES  hold+track') : 'jog = batch size', 1);
            print(0, 55, 'pad 25 = generate', 1);
        }
        return;
    }

    if (view === V_FX) {
        print(0, 0, 'V4 FX  ' + fxTracks.length + '/8 TRACKS'
                  + (fxLive ? '' : '  (SILENT)'), 1);
        print(96, 0, fxCount + 'FX', 1);
        /* The composite chain in signal order — which is ACTIVATION order, not slot
         * order, so this is the one place it can be read. */
        const sum = fxSummary.length > 42 ? fxSummary.substring(0, 41) + '.' : fxSummary;
        print(0, 18, sum.substring(0, 21), 1);
        if (sum.length > 21) print(0, 31, sum.substring(21, 42), 1);
        if (wetShow >= 0) {
            print(0, 48, 'S' + (wetShow + 1) + ' DRY/WET ' +
                  Math.round(fxWet[wetShow] * 100) + '%', 1);
        } else {
            let locks = '';
            for (let i = 0; i < FX_SLOTS; i++) locks += fxLocked[i] ? (i + 1) : '-';
            print(0, 48, 'hold+jog=wet  shift=lock ' + locks, 1);
        }
        return;
    }

    const t = clampf(focus, 0, N_TRACKS - 1) | 0;
    const scanning = !anySelected(t);
    /* Top line: which view, which track, and what its encoder is currently holding.
     * The transport is shown as a word rather than only as a button LED — a granulator
     * that is stopped looks exactly like one with no sample loaded. */
    print(0, 0, 'V' + view + ' T' + (t + 1) + ' ' + (scanning ? 'SCAN' : labels[t]), 1);
    print(84, 0, running ? 'RUN' : 'STOP', 1);
    print(112, 0, (cpu | 0) + '%', 1);

    /* The value, big. While scanning that is the playhead as a percentage — computed
     * locally so it tracks the hand, not the 12Hz status file. */
    const big = scanning ? (Math.round(trackHead(t) * 100) + '%') : readouts[t];
    drawBig(big, 16, 11);

    /* The step bar is ALWAYS this track's playhead, whatever the encoder happens to be
     * driving — so name the track next to the position, or a lit selector makes it look
     * as though the bar has stopped responding. */
    print(0, 40, 'BAR: T' + (t + 1) + ' @ ' + Math.round(trackHead(t) * 100) + '%', 1);

    /* Bottom: the sample, and which selectors are lit on this column. */
    let picked = '';
    for (let s = 0; s < N_SLOTS; s++) if (sel[t][s]) picked += (picked ? '+' : '') + SLOT_NAMES[s];
    picked = picked.substring(0, 22);
    print(0, 54, (loaded[t] ? names[t] : (names[t] + ' ?')).substring(0, 14), 1);
    print(84, 54, picked || 'SCAN', 1);
}

/* ---- status.json (controller -> ui.js) ---- */
function readStatus() {
    if (typeof host_read_file !== 'function') return;
    const raw = host_read_file(STATUS_FILE);
    if (!raw) return;
    let s;
    try { s = JSON.parse(raw); } catch (e) { return; }
    ready = !!s.ready; engine = !!s.engine;
    cpu = s.cpu != null ? s.cpu : 0;
    poolSize = s.pool != null ? s.pool : 0;
    notice = s.notice || '';
    if (s.master != null) master = s.master;
    if (s.running != null) running = !!s.running;
    if (Array.isArray(s.fxSlots)) fxSlots = s.fxSlots;
    if (Array.isArray(s.fxLocked)) fxLocked = s.fxLocked;
    if (Array.isArray(s.fxLabels)) fxLabels = s.fxLabels;
    if (Array.isArray(s.fxWet) && wetShow < 0) fxWet = s.fxWet;
    if (s.rec) {
        recTrack = (s.rec.track === null || s.rec.track === undefined) ? null : (s.rec.track | 0);
        if (Array.isArray(s.rec.loops)) recLoops = s.rec.loops;
    }
    if (s.harvest) {
        harvRunning = !!s.harvest.running;
        harvProgress = s.harvest.progress || 0;
        harvStage = s.harvest.stage || 'idle';
        harvNote = s.harvest.note || '';
        harvBatch = s.harvest.batch || harvBatch;
        harvNames = (s.harvest.samples || []).map(function (x) { return x.name; });
        harvSecs = (s.harvest.samples || []).map(function (x) { return x.seconds || 0; });
    }
    if (Array.isArray(s.fxTracks)) fxTracks = s.fxTracks;
    if (s.fxSummary != null) fxSummary = s.fxSummary;
    if (s.fxCount != null) fxCount = s.fxCount | 0;
    if (s.fxLive != null) fxLive = !!s.fxLive;
    if (Array.isArray(s.projFilled)) projFilled = s.projFilled;
    if (s.projCur != null) projCur = s.projCur | 0;
    if (s.saving != null) {
        const rep = s.saving | 0;
        if (rep >= 0 || phase >= savingLocalUntil) saving = rep;
    }
    /* The controller's focus is authoritative only when we have not just moved one
     * ourselves — a knob touch must not be undone by an in-flight status file. */
    if (s.focus != null && (phase - lastStatusAt) > 2 && showUntil < phase) focus = s.focus | 0;
    if (Array.isArray(s.tracks)) {
        for (let i = 0; i < N_TRACKS; i++) {
            const tr = s.tracks[i] || {};
            if (tr.name != null) names[i] = tr.name;
            if (tr.loaded != null) loaded[i] = !!tr.loaded;
            if (Array.isArray(tr.sel)) {
                for (let k = 0; k < N_SLOTS; k++) sel[i][k] = !!tr.sel[k];
            }
            if (tr.head != null) head[i] = tr.head;
            if (tr.label != null) labels[i] = tr.label;
            if (tr.readout != null) readouts[i] = tr.readout;
        }
    }
    /* Repaint only when something visible actually changed. */
    let sig = (ready ? '1' : '0') + focus + view + (running ? 'R' : 's')
        + (projView ? 'P' : '-') + projCur + ',' + saving + '|'
        + projFilled.map(function (b) { return b ? '1' : '0'; }).join('') + '|'
        + fxSlots.map(function (b) { return b ? '1' : '0'; }).join('')
        + fxLocked.map(function (b) { return b ? 'L' : '.'; }).join('')
        + (fxLive ? 'A' : 'q') + fxTracks.join('.') + 'h' + fxHeld + '|'
        + (harvRunning ? 'H' : '-') + harvStage + harvNames.length + ',' + harvHeld
        + ',' + harvFlash + '|';
    for (let i = 0; i < N_TRACKS; i++) {
        sig += sel[i].map(function (b) { return b ? '1' : '0'; }).join('')
            + Math.round(trackHead(i) * 64) + ',';
    }
    if (sig !== lastLedSig) { lastLedSig = sig; ledDirty = true; }
    screenDirty = true;
}

/* ================= host entry points ================= */
globalThis.init = function () {
    if (typeof host_set_refresh_rate === 'function') host_set_refresh_rate(30);
    phase = 0; launched = false; lastStatusAt = -100;
    ready = false; engine = false; cpu = 0; poolSize = 0; notice = '';
    focus = 0; master = 0.8; view = 1; shiftHeld = false;
    running = false; exitConfirm = false;
    names = new Array(N_TRACKS).fill('—');
    loaded = new Array(N_TRACKS).fill(false);
    head = new Array(N_TRACKS).fill(0);
    headLocal = new Array(N_TRACKS).fill(0);
    headTouched = new Array(N_TRACKS).fill(0);
    labels = new Array(N_TRACKS).fill('Position');
    readouts = new Array(N_TRACKS).fill('0%');
    sel = [];
    for (let i = 0; i < N_TRACKS; i++) sel.push(new Array(N_SLOTS).fill(false));
    fxSlots = [false, false, false, false]; fxLocked = [false, false, false, false];
    fxLabels = ['—', '—', '—', '—'];
    fxTracks = []; fxSummary = 'no chain'; fxCount = 0; fxLive = false;
    fxWet = [0.4, 0.4, 0.4, 0.4]; wetShow = -1; wetShowUntil = 0;
    harvRunning = false; harvProgress = 0; harvStage = 'idle'; harvNote = '';
    harvBatch = 4; harvNames = []; harvHeld = -1; harvSecs = []; harvFlash = -1; harvFlashUntil = 0;
    recHeld = false; recTrack = null; recLoops = new Array(N_TRACKS).fill(false);
    masterTouched = false; recConsumed = false;
    fxHeld = -1; fxConsumed = false;
    projView = false; projFilled = new Array(N_PROJ).fill(false); projCur = -1; saving = -1;
    savingLocalUntil = 0;
    projHeld = -1; projHeldAt = 0; projConsumed = false;
    seq = 0; cmdQueue = []; controlDirty = false;
    overlay = null; overlayUntil = -1; showUntil = 0;
    ledDirty = true; screenDirty = true; lastLedSig = ''; lastDrawAt = -100;
};

globalThis.tick = function () {
    phase++;
    if (phase === 2) {
        sys('mkdir -p ' + HOOKS_DIR);
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit-granola.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit-granola.sh');
        sys('cp ' + MODULE_DIR + '/exit-hook.sh ' + HOOKS_DIR + '/overtake-exit.sh');
        sys('chmod +x ' + HOOKS_DIR + '/overtake-exit.sh');
    }
    if (phase === 3) {
        if (typeof clear_screen === 'function') {
            clear_screen(); print(0, 12, 'GRANOLA', 2); print(0, 40, 'starting engine...', 1);
        }
        sys('sh -c "sh ' + GR + '/run-stack.sh &"');
        launched = true;
    }
    if (!launched) return;
    /* heartbeat, ~every 8s: a trickle. Every host_write_file is a chance to hit the SD
     * I/O stall that hangs tick(), so diagnostic writes stay rare. */
    if (phase % 240 === 0 && typeof host_write_file === 'function') host_write_file(HB_FILE, '' + phase);
    if (controlDirty) { writeControl(); controlDirty = false; }
    /* Read status ~6Hz: host_read_file is synchronous and blocks the tick, so every
     * read is exposure. The playhead stays smooth regardless because it is estimated
     * locally while a column is being turned. */
    if (phase - lastStatusAt >= 5) { readStatus(); lastStatusAt = phase; }
    /* A press held past the threshold becomes a SAVE. Doing it here rather than on
     * release means the pad starts blinking while the finger is still down — the
     * gesture confirms itself at the moment it commits, not afterwards. */
    if (projView && projHeld >= 0 && !projConsumed && (Date.now() - projHeldAt) >= SAVE_HOLD_MS) {
        projConsumed = true;
        saveProject(projHeld);
    }
    if (projView && saving >= 0) { ledDirty = true; screenDirty = true; }
    if (wetShow >= 0 && phase >= wetShowUntil) { wetShow = -1; screenDirty = true; }
    if (view === V_HARV && harvRunning) { ledDirty = true; screenDirty = true; }
    if (recTrack !== null) { ledDirty = true; }      /* keep the record blink moving */
    if (harvFlash >= 0 && phase >= harvFlashUntil) { harvFlash = -1; ledDirty = true; }
    if (view === V_FX && !fxLive) {
        for (let i = 0; i < FX_SLOTS; i++) if (fxSlots[i]) { ledDirty = true; break; }
    }
    /* Keep the position bar animating while a local estimate is live. */
    for (let i = 0; i < N_TRACKS; i++) {
        if ((Date.now() - headTouched[i]) < HEAD_HOLD_MS) { ledDirty = true; break; }
    }
    if (overlay && phase >= overlayUntil) { overlay = null; screenDirty = true; }
    /* SELF-HEAL: the host clears the display when a module is switched in, which can
     * land after our first paint. Since we only repaint on change, an idle rig would
     * then stay blank until something moved. Re-assert a couple of times a second. */
    if (phase - lastDrawAt >= 45) { ledDirty = true; screenDirty = true; }
    if (ledDirty) { renderLEDs(); ledDirty = false; }
    /* Throttle screen redraws to ~10Hz: the block font is heavy on the SPI display and
     * flooding it freezes the Move's UI. */
    if (screenDirty && (phase - lastDrawAt >= 3)) { drawScreen(); screenDirty = false; lastDrawAt = phase; }
};

globalThis.onMidiMessageInternal = function (data) {
    /* The Move emits a background trickle of malformed zero-byte messages, and some
     * Shift combos turn that into a flood that starves tick() and gets the module
     * watchdog-killed. Real channel-voice MIDI has a status byte in 0x80..0xEF. */
    if (!data || data.length < 3 || data[0] < 0x80 || data[0] >= 0xF0) return;
    const status = data[0] & 0xF0;
    const d1 = data[1];
    const d2 = data[2];

    /* The exit prompt is MODAL and PERSISTENT: it stays up until the performer decides —
     * jog push = exit, Back = stay — with no timeout and no accidental dismissal.
     * Everything else is swallowed, so a stray pad can neither cancel the prompt nor
     * trigger an edit behind it. The two deciding controls are accepted as either CC or
     * Note: the jog push is a CC on this firmware, but taking both costs nothing and
     * cannot collide (the pads are notes 68..99). */
    if (exitConfirm) {
        if (d2 > 0 && (status === 0xB0 || status === 0x90)) {
            if (d1 === MoveMainButton) {
                exitConfirm = false;
                sys('sh ' + GR + '/stop-stack.sh');
                if (typeof host_exit_module === 'function') host_exit_module();
                return;
            }
            if (d1 === MoveBack) { exitConfirm = false; screenDirty = true; return; }
        }
        return;
    }

    /* --- knob touch: focus the column being reached for ------------------------- */
    if (d1 >= MoveKnob1Touch && d1 <= MoveKnob8Touch && (status === 0x90 || status === 0x80)) {
        if (status === 0x90 && d2 >= 64) {
            const t = d1 - MoveKnob1Touch;
            if (t !== focus) {
                focus = t;
                sendCmd('focus', t);
                /* Drop any local estimate for the track just selected, so the bar shows
                 * the ENGINE's playhead at this moment rather than wherever this column
                 * was left the last time it was turned. Touching an encoder is a
                 * question — "where is this track?" — and it has to be answered with the
                 * truth, not with a cached answer. */
                headTouched[t] = 0;
                /* ...and read the engine's state on the very next frame rather than
                 * waiting up to ~170ms for the next scheduled poll. One extra read, only
                 * on a focus change, is what makes "where is this track RIGHT NOW"
                 * actually answer with now. */
                lastStatusAt = -100;
            }
            /* Repaint unconditionally, not just when the focus changed: touching the
             * encoder of the track already shown must still re-assert the bar. */
            ledDirty = true;
            showUntil = phase + 30;
            screenDirty = true;
        }
        return;
    }
    /* The volume knob's touch sensor is not used to control anything, but it IS the
     * third finger in the clear-everything chord (shift + volume touch + Rec) — a
     * destructive action deliberately made awkward enough that it cannot happen by
     * accident. */
    if (d1 === MoveMasterTouch && (status === 0x90 || status === 0x80)) {
        masterTouched = (status === 0x90 && d2 > 0);
        return;
    }

    /* --- pads in the PROJECTS view: 32 slots ------------------------------------ */
    if (projView && NOTE_TO_CELL[d1] !== undefined && (status === 0x90 || status === 0x80)) {
        const slot = NOTE_TO_CELL[d1];
        if (status === 0x90 && d2 > 0) {
            /* SHIFT + pad SAVES, immediately and on the press. Holding also saves (see
             * tick), but shift is the gesture the other takeovers on this box use and it
             * is the one a hand reaches for — and unlike a hold it commits the instant
             * you press, so there is never a doubt about whether you held long enough.
             * Marked consumed so the release cannot also fire the load. */
            if (shiftHeld) {
                saveProject(slot);
                projHeld = slot; projConsumed = true;
                return;
            }
            projHeld = slot; projHeldAt = Date.now(); projConsumed = false;
            return;
        }
        /* Release. If the hold already committed a save, the release does nothing —
         * otherwise this was a short press, which loads. */
        if (projHeld === slot) {
            if (!projConsumed) {
                /* Occupied loads, empty starts blank. No confirmation either way:
                 * loading already replaces the live machine without asking, so making
                 * the empty case ask would be the same destructive step with arbitrarily
                 * different friction. The pad's state alone decides what happens. */
                if (projFilled[slot]) {
                    sendCmd('loadproj', slot); showAction('LOAD ' + (slot + 1));
                } else {
                    sendCmd('newproj', slot); showAction('NEW PROJECT ' + (slot + 1));
                }
            }
            projHeld = -1; projConsumed = false;
            ledDirty = true; screenDirty = true;
        }
        return;
    }

    /* --- pads in the HARVESTER: track row, sample rows, generate -------------- */
    if (view === V_HARV && !projView && NOTE_TO_CELL[d1] !== undefined
        && (status === 0x90 || status === 0x80)) {
        const cell = NOTE_TO_CELL[d1];
        const down = (status === 0x90 && d2 > 0);
        if (cell < 8) {                                   /* row 1: the eight tracks */
            /* MOMENTARY, as asked: a destination to tap, never a state to leave behind.
             * It only means anything while a sample pad is held — that pairing IS the
             * assignment gesture. */
            if (down && harvHeld >= 0 && harvHeld < harvNames.length) {
                sendCmd('harvestassign', -1, { sample: harvHeld, track: cell });
                harvFlash = cell; harvFlashUntil = phase + 20;
                showAction('T' + (cell + 1) + ' <- S' + (harvHeld + 1));
                ledDirty = true; screenDirty = true;
            } else if (down) {
                showAction('HOLD A SAMPLE FIRST');
            }
            return;
        }
        if (cell < 24) {                                  /* rows 2-3: the batch */
            const idx = cell - 8;
            if (down) {
                if (idx < harvNames.length) {
                    harvHeld = idx;
                    sendCmd('audition', idx);
                    ledDirty = true; screenDirty = true;
                } else showAction('SLOT ' + (idx + 1) + ' EMPTY');
            } else if (harvHeld === idx) {
                releaseAudition();
            }
            return;
        }
        if (cell === 24 && down) {                        /* row 4, leftmost: GENERATE */
            if (harvRunning) { showAction('HARVESTING'); }
            else {
                harvRunning = true;                       /* optimistic: blink at once */
                sendCmd('harvest', -1);
                showAction('HARVEST ' + harvBatch);
                ledDirty = true; screenDirty = true;
            }
        }
        return;
    }

    /* --- pads in the FX view: track picker + chain slots ------------------------- */
    if (view === V_FX && !projView && NOTE_TO_CELL[d1] !== undefined
        && (status === 0x90 || status === 0x80)) {
        const cell = NOTE_TO_CELL[d1];
        const row = (cell / 8) | 0;
        const col = cell % 8;
        if (row === 0) {                                  /* which tracks go through FX */
            if (status === 0x90 && d2 > 0) {
                const i = fxTracks.indexOf(col);
                if (i >= 0) fxTracks = fxTracks.filter(function (x) { return x !== col; });
                else fxTracks = fxTracks.concat([col]).sort();
                sendCmd('fxtrack', col);
                ledDirty = true; screenDirty = true;
            }
            return;
        }
        if (row === 3 && col < FX_SLOTS) {                /* the four chain slots */
            if (status === 0x90 && d2 > 0) {
                /* SHIFT + slot = LOCK / UNLOCK. Acted on at PRESS, and the press is
                 * marked consumed so the release cannot also toggle the slot — the
                 * gesture must not switch a chain on as a side effect of locking it. */
                if (shiftHeld) {
                    fxLocked[col] = !fxLocked[col];       /* optimistic */
                    sendCmd('fxlock', col);
                    showAction((fxLocked[col] ? 'LOCK S' : 'FREE S') + (col + 1));
                    fxHeld = col; fxConsumed = true;
                    ledDirty = true; screenDirty = true;
                    return;
                }
                /* Repaint at once: the pad going white IS the signal that the modifier is
                 * engaged, and it has to be there before the hand reaches the jog. */
                fxHeld = col; fxConsumed = false;
                ledDirty = true; screenDirty = true;
                return;
            }
            if (fxHeld === col) {
                /* A short press toggles the slot — and turning one ON is what assembles a
                 * fresh chain, so this is also how a chain is re-rolled. Skipped when the
                 * hold already did something: locked it, or rode its dry/wet. */
                if (!fxConsumed) {
                    fxSlots[col] = !fxSlots[col];         /* optimistic */
                    sendCmd('fxslot', col);
                    showAction((fxSlots[col] ? 'ROLL S' : 'OFF S') + (col + 1));
                }
                fxHeld = -1; fxConsumed = false;
                ledDirty = true; screenDirty = true;
            }
            return;
        }
        return;                                            /* middle rows: nothing here */
    }

    /* --- pads: the parameter selectors of one column ---------------------------- */
    if (status === 0x90 && d2 > 0 && NOTE_TO_CELL[d1] !== undefined) {
        const cell = NOTE_TO_CELL[d1];
        const row = (cell / 8) | 0;
        const t = cell % 8;                   /* column: the track, in every view */
        /* The open view's row table decides which parameter this pad selects. A row
         * with nothing on it does nothing, rather than something invented. */
        const slot = (VIEW_ROWS[view] || VIEW_ROWS[1])[row];
        if (slot == null || slot < 0) return;
        /* Persistent toggle, and deliberately NOT exclusive — several lit pads make the
         * encoder a macro over all of them, none makes it the playhead again. The
         * controller owns the state; this is optimistic so the pad lights instantly. */
        sel[t][slot] = !sel[t][slot];
        sendCmd('macro', t, { track: t, slot: slot });
        ledDirty = true; screenDirty = true;
        return;
    }
    if (status === 0x80 && NOTE_TO_CELL[d1] !== undefined) return;   /* pad release: nothing */

    /* --- step buttons: TAP TO JUMP THE PLAYHEAD ---------------------------------- */
    /* The strip already shows the focused track's head; pressing a segment moves it
     * there. It is a position, not an offset, so tapping the same button repeatedly cuts
     * back to the same place — that repetition IS the effect. The local head is set at
     * once and held briefly so the LEDs move under the finger rather than after the next
     * telemetry frame. */
    if (status === 0x90 && d1 >= STEP_BASE && d1 <= STEP_BASE + 15) {
        if (d2 > 0) {
            const t = clampf(focus, 0, N_TRACKS - 1) | 0;
            const step = d1 - STEP_BASE;
            if (loaded[t]) {
                sendCmd('jump', t, { step: step });
                headLocal[t] = step / N_STEPS;
                headTouched[t] = Date.now();
                showAction('T' + (t + 1) + ' JUMP ' + Math.round(step / N_STEPS * 100) + '%');
            } else {
                showAction('T' + (t + 1) + ' EMPTY');
            }
            ledDirty = true; screenDirty = true;
        }
        return;
    }
    if (status === 0x80 && d1 >= STEP_BASE && d1 <= STEP_BASE + 15) return;

    /* --- buttons / encoders ----------------------------------------------------- */
    if (status === 0xB0) {
        if (d1 === MoveShift) { shiftHeld = d2 > 0; return; }

        /* Back ARMS the confirmation — it never exits on its own. */
        if (d2 > 0 && d1 === MoveBack) { exitConfirm = true; screenDirty = true; return; }

        /* Play = the transport: start / stop every granulator together. Works from any
         * view, including the projects view — stopping the sound is never something to
         * have to navigate to. */
        /* --- Rec: record playhead gestures, and the two clear chords --------------- */
        if (d1 === MoveRec) {
            if (d2 > 0) {
                recConsumed = false;
                if (shiftHeld && masterTouched) {
                    /* the widest gesture clears the most: every loop on every track */
                    sendCmd('gestclearall', -1);
                    showAction('ALL LOOPS CLEARED');
                    recConsumed = true;
                } else if (shiftHeld) {
                    sendCmd('gestclear', -1);
                    showAction('LOOP CLEARED');
                    recConsumed = true;
                } else {
                    /* Hold to record. Step presses while held both move the head and go
                     * into the loop — one gesture, not two. */
                    recHeld = true;
                    sendCmd('gestrec', -1, { on: true });
                    showAction('REC - TAP STEPS');
                }
            } else {
                /* Release starts the loop, unless this press was a clear chord. */
                if (recHeld && !recConsumed) sendCmd('gestrec', -1, { on: false });
                recHeld = false;
            }
            ledDirty = true; screenDirty = true;
            return;
        }

        if (d2 > 0 && d1 === MovePlay && shiftHeld) {
            /* KILL SWITCH. Play alone only gates the grain clocks, which cannot stop a
             * master effect whose feedback state has run away — that survives a stop, a
             * view change and an empty project, because it is downstream of every voice.
             * Shift+Play frees the voices, stops any audition and REBUILDS the reverb and
             * delay, which is the only thing that clears their internal state. */
            sendCmd('panic', -1);
            running = false;
            showAction('PANIC - FX REBUILT');
            ledDirty = true; screenDirty = true;
            return;
        }
        if (d2 > 0 && d1 === MovePlay) {
            running = !running;                  /* optimistic; the controller confirms */
            sendCmd('transport', -1);
            ledDirty = true; screenDirty = true;
            return;
        }

        /* Menu = the projects view, exactly where the other takeovers on this box put
         * it. It is a toggle: pressing it again returns to the performance view. */
        if (d2 > 0 && d1 === MoveMenu) {
            projView = !projView;
            projHeld = -1; projConsumed = false;
            ledDirty = true; screenDirty = true;
            return;
        }

        /* View selectors. Views 3-4 are reserved: the button says so rather than
         * doing something invented. */
        /* SHIFT + Track 4 opens the HARVESTER. It is a mode rather than a fifth view:
         * any Track button leaves it, which is the way back out. */
        if (d2 > 0 && shiftHeld && d1 === MoveRow4) {
            view = V_HARV; projView = false;
            releaseAudition(); fxHeld = -1; fxConsumed = false;
            ledDirty = true; screenDirty = true;
            return;
        }
        if (d2 > 0 && (d1 === MoveRow1 || d1 === MoveRow2 || d1 === MoveRow3 || d1 === MoveRow4)) {
            const v = (d1 === MoveRow1) ? 1 : (d1 === MoveRow2) ? 2 : (d1 === MoveRow3) ? 3 : 4;
            if (v <= N_VIEWS) {
                /* A view button always lands you in that view, even from the projects
                 * view — it is the way back out as well as the way in. */
                /* Leaving the harvester silences any audition still sounding — a held
                 * pad in a view you can no longer see would otherwise ring on forever. */
                releaseAudition();
                view = v; projView = false; projHeld = -1; projConsumed = false;
                fxHeld = -1; fxConsumed = false;
                ledDirty = true; screenDirty = true;
            } else showAction('VIEW ' + v + ' RESERVED');
            return;
        }

        /* Everything below edits the PERFORMANCE view. While the projects view is open
         * those controls are inert rather than quietly editing a track that is not on
         * screen — except Back, handled above, which must always work. */
        if (projView) return;

        /* The eight column encoders. */
        if (d1 >= MoveKnob1 && d1 <= MoveKnob1 + 7) {
            const t = d1 - MoveKnob1;
            const dn = decodeDelta(d2);
            if (dn === 0) return;
            /* No separate focus command here: the encoder command carries the track,
             * and the controller sets focus from it. */
            if (t !== focus) { focus = t; headTouched[t] = 0; }
            /* The controller decides what this moves — the column's own pads do, and
             * only its own. Sending the raw detent count keeps that decision in the
             * one place that owns the model. */
            sendCmd('encoder', t, { track: t, ticks: dn });
            if (!anySelected(t)) {
                /* Scanning: keep the position bar under the hand. `position` is a
                 * linear 0..1 parameter, so the normalized delta IS the value delta. */
                headLocal[t] = clampf((((Date.now() - headTouched[t]) < HEAD_HOLD_MS)
                    ? headLocal[t] : head[t]) + encoderDelta(dn), 0, 1);
                headTouched[t] = Date.now();
            }
            showUntil = phase + 30;
            ledDirty = true; screenDirty = true;
            return;
        }

        /* Master knob = output level. */
        if (d1 === MoveMaster) {
            const dn = decodeDelta(d2);
            if (dn === 0) return;
            master = clampf(master + dn * 0.01, 0, 1.4);
            sendCmd('master', -1, { value: master });
            showAction('LEVEL ' + Math.round(master * 100));
            return;
        }

        /* In the harvester the jog sets how many samples one batch brings back, 1..16 —
         * the same count the sixteen sample pads can hold. */
        if (view === V_HARV && d1 === MoveMainKnob) {
            const dn = decodeDelta(d2);
            if (dn === 0) return;
            harvBatch = Math.max(1, Math.min(HARV_SAMPLES, harvBatch + (dn > 0 ? 1 : -1)));
            sendCmd('harvestbatch', -1, { ticks: dn > 0 ? 1 : -1 });
            screenDirty = true;
            return;
        }

        /* HOLD A CHAIN SLOT + JOG = that slot's DRY/WET, across every Airwindows link in
         * its chain at once. The hold is purely a modifier: a short press still toggles
         * the slot (and turning one on is what rolls a new chain), so nothing else has to
         * move out of the way for this. Marking the press consumed stops the release
         * from also toggling the slot the hand is deliberately holding. */
        if (view === V_FX && fxHeld >= 0 && d1 === MoveMainKnob) {
            const dn = decodeDelta(d2);
            if (dn === 0) return;
            fxConsumed = true;
            fxWet[fxHeld] = clampf(fxWet[fxHeld] + dn * 0.02, 0, 1);   /* optimistic */
            sendCmd('fxwet', fxHeld, { ticks: dn });
            wetShow = fxHeld; wetShowUntil = phase + 30;
            screenDirty = true;
            return;
        }

        /* Jog wheel = step the focused track through the discovered samples. Eight
         * granular tracks are useless without material, and this view has no browser;
         * a sample browser belongs to one of the reserved views. */
        if (d1 === MoveMainKnob) {
            const dn = decodeDelta(d2);
            if (dn === 0) return;
            sendCmd('sample', focus, { track: focus, dir: dn > 0 ? 1 : -1 });
            showAction('T' + (focus + 1) + ' SAMPLE');
            return;
        }

        /* Jog push = re-scan the sample folders (something was just copied over). */
        if (d2 > 0 && d1 === MoveMainButton) {
            sendCmd('rescan', -1);
            showAction('RESCAN');
            return;
        }
    }
};

globalThis.onMidiMessageExternal = function (data) {};

/* Defensive: never let a stray exception in a frame or input handler crash the JS
 * runtime or hang the Schwung host — a hung tick freezes the whole Move UI. */
(function () {
    const _tick = globalThis.tick, _mid = globalThis.onMidiMessageInternal;
    globalThis.tick = function () { try { _tick(); } catch (e) { ledDirty = false; screenDirty = false; } };
    globalThis.onMidiMessageInternal = function (data) { try { _mid(data); } catch (e) { } };
})();
