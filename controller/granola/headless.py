"""Granola headless controller (runs on the Move).

Bridges the Schwung ui.js hardware layer (which cannot open sockets) to the SC engine:

    ui.js  --writes-->  ipc/control.json  --polled by--> this controller
    ui.js  <--reads--   ipc/status.json   <--written by-- this controller
    this controller  --OSC /gr/...-->  sclang engine (127.0.0.1:57120)

This process owns the authoritative Granola model (Model/Track — the port of the app's
GranolaModel/TrackModel). The UI never owns synthesis state: it sends gestures and
renders whatever status.json says, so redrawing LEDs, switching views or relaunching
the module cannot reset a parameter.

    Hardware input -> ui.js view state -> [control.json] -> Granola control model -> DSP
    DSP/model state -> [status.json] -> ui.js view state -> Move display / LEDs
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from . import airwindows as aw
from . import harvester as hv
from . import samples as samples_mod
from .engine_bridge import EngineBridge
from .projects import N_SLOTS, ProjectStore
from .params import MACRO_SLOTS, SPECS, format_value
from .tracks import N_TRACKS, Model


def _env(k: str, d: str) -> str:
    v = os.environ.get(k)
    return v if v not in (None, "") else d


SC_HOST = _env("SC_HOST", "127.0.0.1")
SC_PORT = int(_env("SC_PORT", "57120"))
TELEMETRY_PORT = int(_env("CONTROLLER_PORT", "57141"))
IPC = Path(_env("GR_IPC", "/data/UserData/granola/ipc"))
STATE = Path(_env("GR_STATE", "/data/UserData/granola/state"))
SAMPLES_DIR = _env("GR_SAMPLES", "/data/UserData/granola/samples")
CONTROL_FILE = IPC / "control.json"
STATUS_FILE = IPC / "status.json"
MODEL_FILE = STATE / "model.json"
HARVEST_FILE = STATE / "harvest.json"
PROJECTS_DIR = Path(_env("GR_PROJECTS", "/data/UserData/granola/projects"))
WEB_PORT = int(_env("GR_WEB_PORT", "7135"))
FX_MANIFEST = Path(_env("GR_FX_MANIFEST", "/data/UserData/granola/airwindows-manifest.json"))
FX_COSTS = Path(_env("GR_FX_COSTS", "/data/UserData/granola/fx-cost.json"))
HARVEST_WORK = _env("GR_HARVEST_WORK", "/data/UserData/granola/harvest-tmp")
# Ceiling for one region analysis. Measured worst case is ~55 s (a 90 s source with a 30 s
# region); this is deliberately several times that, because the cost of waiting too long is
# a slow harvest and the cost of not waiting long enough is a dead engine.
EXCERPT_TIMEOUT = float(_env("GR_EXCERPT_TIMEOUT", "240"))
# How long the Move's project pad keeps blinking after a save is issued. A save is a
# few milliseconds of JSON, far too fast to see — but "did that take?" is exactly the
# question a performer needs answered, so the blink is held long enough to read.
SAVE_BLINK_SEC = 0.9

CONTROL_HZ = float(_env("GR_CONTROL_HZ", "30"))     # control.json poll rate
STATUS_HZ = float(_env("GR_STATUS_HZ", "12"))       # status.json write rate
AUTOSAVE_SEC = float(_env("GR_AUTOSAVE_SEC", "20"))


class Controller:
    def __init__(self) -> None:
        self.model = Model()
        self.bridge = EngineBridge(SC_HOST, SC_PORT, "127.0.0.1", TELEMETRY_PORT,
                                   n_tracks=N_TRACKS)
        self.bridge.on_loaded = self._on_loaded
        self.bridge.on_loadfail = self._on_loadfail
        self._stop = threading.Event()
        self._built = threading.Event()
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._last_seq = -1
        # control.json survives a restart, so a queue left on disk belongs to the PREVIOUS
        # session and replaying it would fire stale gestures at a booting engine. But
        # "the first thing I read" is the wrong test for staleness: stop-stack deletes the
        # file, so after a normal restart the first thing read is the performer's FIRST
        # REAL GESTURE — and priming on it silently swallowed it. Compare the file's mtime
        # with our start time instead: older than us = leftover, newer = a live command.
        self._seq_primed = False
        self._started_at = time.time()
        self._dirty = True
        self._last_status_key: str | None = None
        self._pool: list[str] = []
        self._notice = ""
        self._notice_until = 0.0
        self.projects = ProjectStore(PROJECTS_DIR)
        # Slot currently being saved, and until when — drives the pad blink on the Move.
        self._saving = -1
        self._saving_until = 0.0
        # Transport. Starts STOPPED: eight grain clouds coming up at full tilt the moment
        # the module opens is not a neutral state to hand someone, and it would make the
        # Play button meaningless on launch.
        self.running = False
        # --- performance FX (the Airwindows chain) ---
        self.catalog = aw.Catalog(FX_MANIFEST, FX_COSTS)
        self.fx_slots = [aw.Slot(i) for i in range(aw.N_SLOTS)]
        self.fx_tracks: set[int] = set()      # tracks routed through the chain
        self._fx_order = 0                    # activation counter
        # Node ids for chain links. Fixed range, well clear of voices (1000-1099),
        # strips (1100+) and the fixed effect/master nodes, exactly as the app did.
        self._fx_node = 3000
        # --- harvester ---
        self.harvest: hv.Harvest | None = None
        self.harvest_batch = hv.BATCH_DEFAULT
        self.harvest_dur = (hv.DUR_DEFAULT_MIN, hv.DUR_DEFAULT_MAX)
        # The last batch, kept so it OUTLIVES the Harvest object. Leaving the harvester
        # view and coming back — or a restart — must not lose a batch that took minutes of
        # network time to gather; the pads are the only place those samples are reachable.
        self._last_batch: list[dict] = []
        self._auditioning: int | None = None
        self._excerpt_tag = 0
        # Playheads a project asked for, waiting for their samples to finish loading.
        self._pending_head: dict[int, float] = {}
        # --- playhead gesture loops ---
        # Per track: the step presses made while Rec was held, with their timings, looped
        # back on release. The loop length is the WHOLE held window, not the span between
        # first and last press, so how long you hold Rec sets the bar — a tap at the start
        # and nothing else gives a long, sparse loop rather than a one-shot.
        self.gest: dict[int, dict] = {}
        self._rec_track: int | None = None
        self._rec_t0 = 0.0

    # -- startup ----------------------------------------------------------- #
    def start(self) -> None:
        IPC.mkdir(parents=True, exist_ok=True)
        STATE.mkdir(parents=True, exist_ok=True)
        Path(SAMPLES_DIR).mkdir(parents=True, exist_ok=True)

        # GRANOLA STARTS EMPTY. It does NOT reload the last session's model.
        #
        # Restoring it meant the instrument came up with samples on tracks nobody had
        # loaded in this session, and pressing Play produced a grain cloud from a track the
        # performer had not touched — audible, and with no visible cause, because the
        # performer's mental model was "nothing is loaded". Projects are the deliberate way
        # to bring state back: Menu, then a pad. Startup is a blank instrument.
        #
        # GR_RESTORE_MODEL=1 brings the old behaviour back for anyone who wants it.
        if _env("GR_RESTORE_MODEL", "0") == "1" and self.model.load(MODEL_FILE):
            print("[granola] restored model from %s" % MODEL_FILE, flush=True)
            n = self._confine_to_library()
            if n:
                print("[granola] dropped %d sample(s) from outside %s"
                      % (n, SAMPLES_DIR), flush=True)
        else:
            print("[granola] starting empty (no project loaded)", flush=True)

        # The harvested batch is NOT project state — it is a scratch pad of material — so
        # it is restored even though the model is not. Entries whose file has since gone
        # are dropped rather than shown as pads that do nothing.
        self._load_batch()

        self._pool = samples_mod.discover((SAMPLES_DIR,))
        print("[granola] %d sample(s) in %s" % (len(self._pool), SAMPLES_DIR), flush=True)
        print("[granola] %d airwindows effect(s), %d cost entries, budget %.0f%%%s" %
              (len(self.catalog), len(self.catalog.costs), aw.FX_CPU_BUDGET,
               "" if len(self.catalog) else " — FX view will be empty"), flush=True)

        # The web UI runs in this process and shares the model directly, so a change
        # made in a browser and a change made on the hardware go through exactly the
        # same code — there is no second copy of the state to drift.
        try:
            from .webserver import serve
            serve(self, WEB_PORT)
            print("[granola] web UI on http://move.local:%d" % WEB_PORT, flush=True)
        except Exception:
            print("[granola] web UI failed to start", flush=True)
            traceback.print_exc()

        self.bridge.start(on_ready=self._on_ready)
        for fn in (self._handshake_loop, self._control_loop,
                   self._status_loop, self._autosave_loop, self._gesture_loop):
            t = threading.Thread(target=self._safe_loop, args=(fn,), daemon=True)
            t.start()
            self._threads.append(t)

    def run(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.1)

    def stop(self, *_a) -> None:
        self._stop.set()
        self.bridge.stop()
        with self._lock:
            self.model.save(MODEL_FILE)

    def _safe_loop(self, fn) -> None:
        """A crashed loop must not permanently kill the instrument: a dead control loop
        means no encoders and no pads until relaunch. Log loudly, then resume."""
        while not self._stop.is_set():
            try:
                fn()
                return                                   # clean exit (stop requested)
            except Exception:
                print("[granola] LOOP CRASHED: %s — restarting" % fn.__name__, flush=True)
                traceback.print_exc()
                sys.stdout.flush()
                sys.stderr.flush()
                time.sleep(0.5)

    # -- engine handshake --------------------------------------------------- #
    def _handshake_loop(self) -> None:
        while not self._stop.is_set():
            if not self._built.is_set():
                self.bridge.ping()
            time.sleep(1.0)

    def _on_ready(self) -> None:
        self._built.set()
        self._push_all()

    def _push_all(self) -> None:
        """Push the authoritative model at a freshly-booted engine."""
        n = sum(1 for t in self.model.tracks if t.sample_path)
        print("[granola] engine ready — pushing model (%d/%d tracks have a sample)"
              % (n, N_TRACKS), flush=True)
        with self._lock:
            self.bridge.master(self.model.master)
            self.bridge.run(self.running)
            for t in self.model.tracks:
                self.bridge.mute(t.index, t.mute)
                # Every parameter, not just the four on the pads: the engine's graph is
                # new, so anything not sent would sit at its SynthDef default.
                self.bridge.params(t.index, [(SPECS[k].osc_key, v)
                                             for k, v in t.values.items()])
                if t.sample_path:
                    self.bridge.load(t.index, t.sample_path)
        self._restore_fx()
        self._dirty = True

    def _on_loaded(self, track: int, duration: float, channels: int) -> None:
        with self._lock:
            if 0 <= track < N_TRACKS:
                t = self.model.tracks[track]
                t.duration, t.channels, t.loaded = duration, channels, True
                # The voice is rebuilt around the new buffer, so re-assert the track's
                # parameters on top of it.
                self.bridge.params(track, [(SPECS[k].osc_key, v) for k, v in t.values.items()])
                # A project restores the playhead, and the voice that has to carry it only
                # exists now. Sent as a JUMP so it lands exactly rather than gliding in
                # from wherever the fresh voice started.
                want = self._pending_head.pop(track, None)
                if want is not None:
                    t.head = want
                    self.bridge.jump(track, want)
        self._dirty = True

    def _on_loadfail(self, track: int) -> None:
        with self._lock:
            if 0 <= track < N_TRACKS:
                t = self.model.tracks[track]
                t.loaded = False
                t.sample_name = "!" + t.sample_name
        self._dirty = True

    # -- samples ------------------------------------------------------------ #
    def _confine_to_library(self) -> int:
        """Drop any track sample that does not live in Granola's own folder.

        Enforced rather than merely defaulted: the restriction has to hold for state and
        for projects written BEFORE it existed, and for a project file copied in from
        somewhere else. Without this, a stale state file quietly reloads another
        takeover's audio on the next launch and the rule is a comment, not a rule.
        """
        root = os.path.realpath(SAMPLES_DIR)
        dropped = 0
        for t in self.model.tracks:
            if not t.sample_path:
                continue
            real = os.path.realpath(t.sample_path)
            inside = real == root or real.startswith(root + os.sep)
            # Existence matters as much as location: a sample deleted from the library
            # leaves state and projects pointing at nothing, and the track would come up
            # as a failed load rather than as the empty slot it actually is.
            if not inside or not os.path.exists(real):
                t.sample_path, t.sample_name, t.loaded = None, "—", False
                dropped += 1
        return dropped

    def _load_all_samples(self) -> None:
        with self._lock:
            for t in self.model.tracks:
                if t.sample_path:
                    self.bridge.load(t.index, t.sample_path)

    def _cycle_sample(self, track: int, direction: int) -> None:
        """Step a track through the discovered pool. This is the only sample-choosing
        gesture View 1 exposes; a browser belongs to a later view."""
        if not self._pool or not 0 <= track < N_TRACKS:
            return
        with self._lock:
            t = self.model.tracks[track]
            try:
                cur = self._pool.index(t.sample_path)
            except ValueError:
                cur = -1
            nxt = (cur + direction) % len(self._pool)
            t.sample_path = self._pool[nxt]
            t.sample_name = Path(t.sample_path).stem[:16]
            t.loaded = False
            self.bridge.load(track, t.sample_path)
        self._notify(t.sample_name)
        self._dirty = True

    # -- performance FX ----------------------------------------------------- #
    def _next_fx_node(self) -> int:
        self._fx_node += 1
        if self._fx_node > 3899:
            self._fx_node = 3000
        # --- harvester ---
        self.harvest: hv.Harvest | None = None
        self.harvest_batch = hv.BATCH_DEFAULT
        self._auditioning: int | None = None
        self._excerpt_tag = 0
        return self._fx_node

    def _fx_instantiate(self, slot) -> None:
        """Put a slot's chain into the graph. Links are added in order, and the engine
        inserts each before the terminator, so this order IS the signal order."""
        for link in slot.chain:
            link.node = self._next_fx_node()
            # Carry the slot's current wet level, so a rack brought back up comes back at
            # the blend it was left at rather than at the one it was rolled with.
            self.bridge.fx_add(link.node, link, mix=slot.mix_of(link))

    def _fx_teardown(self, slot) -> None:
        """Take a slot's links out of the graph, keeping the chain itself. The engine
        fades each one out over fxFadeTime, so this never clicks."""
        for link in slot.chain:
            if link.node:
                self.bridge.fx_free(link.node)
                link.node = 0

    def _sync_fx_live(self) -> None:
        """THE CHAIN ONLY EXISTS WHILE SOMETHING IS ROUTED INTO IT.

        granolaReverb and granolaDelay both output to the PERFORMANCE bus — that is the
        app's design, so the chain mangles the effect returns too. The consequence on
        this instrument was that a raised reverb send made the Airwindows chain audible
        with no track routed at all (measured: 0.0501 -> 0.0722 with one chain on), which
        is not what "these tracks go through the effects" should mean.

        Rather than bypassing each link — the compiled SynthDefs have no bypass control,
        and stepping `mix` to zero would click — the links are simply not in the graph
        while nothing is routed. The slot keeps its state and its chain, so this is
        invisible except in the one way that matters: no routed tracks, no effect. It
        also costs nothing to run an armed rack that is not being used.
        """
        routed = bool(self.fx_tracks)
        # Activation order, so re-entering the graph rebuilds the same signal order.
        for slot in sorted((s for s in self.fx_slots if s.active), key=lambda s: s.order):
            live = any(l.node for l in slot.chain)
            if routed and not live:
                self._fx_instantiate(slot)
            elif not routed and live:
                self._fx_teardown(slot)

    def set_fx_slot(self, index: int, active: bool) -> bool:
        """Toggling a slot ON assembles a brand-new random chain for it; toggling it OFF
        frees that slot's links and lets the rest of the chain close up. Ported from
        GranolaModel.setFXSlot — generating on activation is the gesture, not a separate
        'roll' button."""
        if not 0 <= index < aw.N_SLOTS:
            return False
        with self._lock:
            slot = self.fx_slots[index]
            if slot.active == active:
                return False
            slot.active = active
            if active:
                self._fx_order += 1
                slot.order = self._fx_order
                # A locked slot REPLAYS its chain; only an unlocked one rolls a new
                # one. A locked slot that has never been rolled has nothing to preserve,
                # so it rolls once — and that is then the chain the lock holds.
                if not (slot.locked and slot.chain):
                    slot.chain = self.catalog.assemble_chain(aw.FX_WET_MIX,
                                                             budget=self.fx_budget_left())
                # Only put it in the graph if there is something to process. Arming a
                # slot with no track routed is legitimate — it just makes no sound yet.
                if self.fx_tracks:
                    self._fx_instantiate(slot)
                self._notify("S%d %s%s" % (index + 1, "* " if slot.locked else "", slot.label))
            else:
                self._fx_teardown(slot)
                # Keeping the chain is the whole point of the lock: clearing it here is
                # what would make the slot roll afresh next time it is switched on.
                if not slot.locked:
                    slot.chain = []
                slot.order = 0
        self._dirty = True
        return True

    def reroll_fx_slot(self, index: int) -> bool:
        """Re-roll a slot that is already running, without disturbing the others."""
        if not 0 <= index < aw.N_SLOTS or not self.fx_slots[index].active:
            return False
        if self.fx_slots[index].locked:
            # Refused, not silently obeyed: a lock that a different gesture can overwrite
            # is not a lock. Unlock it first.
            self._notify("S%d LOCKED" % (index + 1))
            return False
        self.set_fx_slot(index, False)
        self.set_fx_slot(index, True)
        return True

    def set_fx_wet(self, index: int, ticks: int = 0, value: float | None = None) -> bool:
        """Move a slot's dry/wet, and with it every link in that slot's chain.

        One gesture over the whole chain rather than per link: the links were rolled with
        their own blends around FX_WET_MIX and that relative balance is the chain's
        character, so this scales them together. At 0 the chain is fully dry.
        """
        if not 0 <= index < aw.N_SLOTS:
            return False
        with self._lock:
            slot = self.fx_slots[index]
            if value is None:
                # ~2% of the range per detent: a full sweep is about a turn and a half,
                # fine enough to ride under a hand and coarse enough to get there.
                value = slot.wet + ticks * 0.02
            slot.wet = min(1.0, max(0.0, value))
            for link in slot.chain:
                if link.node:
                    self.bridge.fx_mix(link.node, slot.mix_of(link))
        self._dirty = True
        return True

    def set_fx_lock(self, index: int, locked: bool | None = None) -> bool:
        """Lock or unlock a slot. Locking an EMPTY slot is allowed — it arms the lock, and
        the first chain rolled into it becomes the one it holds."""
        if not 0 <= index < aw.N_SLOTS:
            return False
        with self._lock:
            slot = self.fx_slots[index]
            slot.locked = (not slot.locked) if locked is None else bool(locked)
            if not slot.locked and not slot.active:
                # An inactive slot only keeps its chain because it was locked; once
                # unlocked, that chain has no reason to survive and the slot must roll
                # fresh next time — otherwise "unlocked" would still replay the old one.
                slot.chain = []
            self._notify("S%d %s" % (index + 1, "LOCKED" if slot.locked else "UNLOCKED"))
        self._dirty = True
        return True

    def set_fx_track(self, track: int, on: bool) -> bool:
        if not 0 <= track < N_TRACKS:
            return False
        with self._lock:
            if on:
                self.fx_tracks.add(track)
            else:
                self.fx_tracks.discard(track)
            self.bridge.route(track, on)
            # Routing the first track in brings the rack up; routing the last one out
            # takes it down again.
            self._sync_fx_live()
        self._dirty = True
        return True

    def fx_budget_left(self, excluding: int = -1) -> float:
        """How much of the rack's CPU budget is still unspent.

        Counts only slots that are ACTUALLY RUNNING: a locked-but-off slot holds its
        chain but costs nothing, so reserving budget for it would starve the rolls that
        are audible right now.
        """
        spent = sum(self.catalog.chain_cost(s.chain)
                    for s in self.fx_slots if s.active and s.index != excluding)
        return max(0.0, aw.FX_CPU_BUDGET - spent)

    @property
    def fx_cost(self) -> float:
        return sum(self.catalog.chain_cost(s.chain) for s in self.fx_slots if s.active)

    @property
    def fx_summary(self) -> str:
        """The whole composite chain, in signal order — which is ACTIVATION order."""
        names = []
        for slot in sorted((s for s in self.fx_slots if s.active), key=lambda s: s.order):
            names += [l.summary for l in slot.chain]
        return " > ".join(names) if names else "no chain"

    def _restore_fx(self) -> None:
        """Re-apply routing and rebuild any running chain after an engine restart. The
        chains are NOT re-rolled — a restart must not change what you were playing."""
        with self._lock:
            self.bridge.fx_gain(aw.FX_AUTO_GAIN)
            for t in self.fx_tracks:
                self.bridge.route(t, True)
            for slot in self.fx_slots:
                for link in slot.chain:
                    link.node = 0          # the old graph is gone; nothing is live
            self._sync_fx_live()

    # -- harvester ----------------------------------------------------------- #
    def excerpt_blocking(self, src: str, dst: str, seconds: float,
                         timeout: float = EXCERPT_TIMEOUT) -> bool:
        """Ask the engine to choose and write an excerpt, and wait for its answer.

        Blocking is right here: it runs on the harvest thread, which is a sequence of
        slow network steps anyway, and the alternative — threading a callback back into
        the pipeline — would buy nothing.

        THE TIMEOUT MUST EXCEED THE ANALYSIS, generously. Region analysis is tens of
        seconds; when this was 40 s the controller abandoned requests the engine was still
        working on, deleted the source from under it, and issued the next one — so two
        analyses ran at once over one shared server buffer and one shared scratch file,
        and the stack came down. Giving up early does not cancel the work, it only makes
        it collide with the work that follows.
        """
        self._excerpt_tag += 1
        tag = self._excerpt_tag
        self.bridge.excerpt(src, dst, seconds, tag)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = self.bridge.excerpt_result(tag)
            if res != "pending":
                return bool(res)
            time.sleep(0.05)
        return False

    def start_harvest(self, count: int | None = None) -> bool:
        if self.harvest is not None and self.harvest.running:
            return False
        n = self.harvest_batch if count is None else count
        self.harvest = hv.Harvest(self, n, SAMPLES_DIR, HARVEST_WORK)
        ok = self.harvest.start()
        if ok:
            self._notify("HARVEST %d" % n)
        self._dirty = True
        return ok

    def set_harvest_batch(self, ticks: int = 0, value: int | None = None) -> int:
        n = self.harvest_batch + ticks if value is None else value
        self.harvest_batch = max(hv.BATCH_MIN, min(hv.BATCH_MAX, int(n)))
        self._dirty = True
        return self.harvest_batch

    def assign_harvested(self, sample_index: int, track: int) -> bool:
        """Put one harvested sample on a track — the harvester's whole point."""
        batch = self.harvest.samples if self.harvest is not None else self._last_batch
        if not (0 <= sample_index < len(batch)):
            return False
        ok = self.set_track_sample(track, batch[sample_index]["path"])
        if ok:
            self._notify("T%d %s" % (track + 1, batch[sample_index]["name"][:14]))
        return ok

    def _load_batch(self) -> None:
        try:
            raw = json.loads(HARVEST_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            return
        keep = [e for e in raw.get("samples", [])
                if isinstance(e, dict) and e.get("path") and os.path.exists(e["path"])]
        self._last_batch = keep
        d = raw.get("dur")
        if isinstance(d, list) and len(d) == 2:
            self.set_harvest_dur(d[0], d[1], save=False)
        if keep:
            print("[granola] restored %d harvested sample(s)" % len(keep), flush=True)

    def _save_batch(self) -> None:
        try:
            HARVEST_FILE.write_text(json.dumps(
                {"samples": self._last_batch, "dur": list(self.harvest_dur)}))
        except OSError:
            pass

    def set_harvest_dur(self, lo, hi, save: bool = True) -> tuple[float, float]:
        """Excerpt length range, clamped to the permitted floor and ceiling."""
        lo = max(hv.DUR_FLOOR, min(hv.DUR_CEIL, float(lo)))
        hi = max(lo, min(hv.DUR_CEIL, float(hi)))
        self.harvest_dur = (round(lo, 1), round(hi, 1))
        if save:
            self._save_batch()
        self._dirty = True
        return self.harvest_dur

    # -- playhead gesture loops --------------------------------------------- #
    def gesture_record(self, on: bool) -> None:
        """Rec held: capture step presses for the focused track. Rec released: loop them."""
        if on:
            self._rec_track = self.model.focus
            self._rec_t0 = time.monotonic()
            self.gest.pop(self._rec_track, None)
            self._notify("REC T%d" % (self._rec_track + 1))
        else:
            t = self._rec_track
            self._rec_track = None
            if t is None:
                return
            g = self.gest.get(t)
            if not g or not g["events"]:
                self.gest.pop(t, None)
                self._notify("REC EMPTY")
            else:
                g["len"] = max(0.20, time.monotonic() - self._rec_t0)
                g["start"] = time.monotonic()
                g["idx"] = 0
                self._notify("LOOP T%d %.1fs" % (t + 1, g["len"]))
        self._dirty = True

    def gesture_capture(self, track: int, step: int) -> None:
        """Record one step press, if this track is the one being recorded."""
        if self._rec_track != track:
            return
        g = self.gest.setdefault(track, {"events": [], "len": 0.0, "start": 0.0, "idx": 0})
        g["events"].append((time.monotonic() - self._rec_t0, step))

    def gesture_clear(self, track: int | None = None) -> None:
        t = self.model.focus if track is None else track
        if self.gest.pop(t, None) is not None:
            self._notify("LOOP T%d CLEARED" % (t + 1))
        else:
            self._notify("T%d NO LOOP" % (t + 1))
        if self._rec_track == t:
            self._rec_track = None
        self._dirty = True

    def gesture_clear_all(self) -> None:
        n = len(self.gest)
        self.gest.clear()
        self._rec_track = None
        self._notify("ALL LOOPS CLEARED (%d)" % n)
        self._dirty = True

    def _gesture_tick(self) -> None:
        """Fire whatever each looping track owes. Runs on the control thread: the events
        are jumps, a handful per second at most, so it costs nothing to poll them here."""
        now = time.monotonic()
        for track, g in list(self.gest.items()):
            if not g.get("len"):
                continue                       # still being recorded
            phase = now - g["start"]
            if phase >= g["len"]:
                g["start"] += g["len"]
                g["idx"] = 0
                phase = now - g["start"]
            ev = g["events"]
            while g["idx"] < len(ev) and ev[g["idx"]][0] <= phase:
                step = ev[g["idx"]][1]
                g["idx"] += 1
                pos = step / 16.0
                t = self.model.tracks[track]
                t.set_value("position", pos)
                t.head = pos
                self.bridge.jump(track, pos)

    @property
    def gesture_state(self) -> dict:
        return {"track": self._rec_track,
                "loops": [(i in self.gest and bool(self.gest[i].get("len")))
                          for i in range(N_TRACKS)]}

    def audition(self, sample_index: int) -> bool:
        """Hold a harvested pad to hear the sample. Auditioning is deliberately
        independent of the tracks: it does not disturb a loaded sample, a routing, or the
        transport, so a performer can listen to a candidate mid-set without consequences."""
        batch = self.harvest.samples if self.harvest is not None else self._last_batch
        if not (0 <= sample_index < len(batch)):
            return False
        self._auditioning = sample_index
        self.bridge.audition(batch[sample_index]["path"])
        return True

    def audition_off(self) -> None:
        if self._auditioning is not None:
            self._auditioning = None
            self.bridge.audition_off()

    @property
    def harvest_state(self) -> dict:
        h = self.harvest
        if h is None:
            return {"running": False, "progress": 0.0, "stage": "idle", "note": "",
                    "samples": [{"name": x["name"], "seconds": x["seconds"],
                                 "source": x.get("source", "")} for x in self._last_batch],
                    "batch": self.harvest_batch, "auditioning": self._auditioning,
                    "durMin": self.harvest_dur[0], "durMax": self.harvest_dur[1]}
        return {
            "running": h.running,
            "progress": round(h.progress, 3),
            "stage": h.stage,
            "note": h.note,
            "samples": [{"name": s["name"], "seconds": s["seconds"],
                         "source": s["source"]} for s in h.samples],
            "batch": self.harvest_batch,
            "auditioning": self._auditioning,
            "durMin": self.harvest_dur[0],
            "durMax": self.harvest_dur[1],
        }

    # -- projects ----------------------------------------------------------- #
    def machine_snapshot(self) -> dict:
        """Everything about the live machine that is NOT in the track model.

        A project is meant to be the instrument at that instant, so the transport and the
        whole FX rack belong in it — including each slot's exact chain and the parameters
        every link was rolled with. Re-rolling on load would hand back a different sound
        under the same name, which is the one thing a saved project must never do.
        """
        return {
            "running": self.running,
            "fxTracks": sorted(self.fx_tracks),
            "fxSlots": [{
                "active": s.active,
                "locked": s.locked,
                "wet": round(s.wet, 4),
                "order": s.order,
                "chain": [{
                    "effect": l.effect.name,
                    "params": [round(x, 5) for x in l.params],
                    "mix": round(l.mix, 5),
                    "lfoTarget": l.lfo_target,
                    "lfoRate": round(l.lfo_rate, 5),
                    "lfoDepth": round(l.lfo_depth, 5),
                    "lfoShape": l.lfo_shape,
                } for l in s.chain],
            } for s in self.fx_slots],
        }

    def _apply_machine(self, m: dict) -> None:
        """Put a saved machine block back, chains and all."""
        by_name = {e.name: e for e in self.catalog.effects}
        for i, snap in enumerate(m.get("fxSlots") or []):
            if i >= len(self.fx_slots):
                break
            slot = self.fx_slots[i]
            slot.active = bool(snap.get("active"))
            slot.locked = bool(snap.get("locked"))
            try:
                slot.wet = float(snap.get("wet", aw.FX_WET_MIX))
            except (TypeError, ValueError):
                slot.wet = aw.FX_WET_MIX
            slot.order = int(snap.get("order", 0))
            chain = []
            for ls in snap.get("chain") or []:
                eff = by_name.get(ls.get("effect"))
                if eff is None:
                    # The catalogue changed under a saved project. Drop the link rather
                    # than the whole chain, and say so.
                    print("[granola] project: unknown effect %r, dropped"
                          % ls.get("effect"), flush=True)
                    continue
                chain.append(aw.Link(
                    effect=eff,
                    params=[float(x) for x in (ls.get("params") or [])],
                    mix=float(ls.get("mix", aw.FX_WET_MIX)),
                    lfo_target=int(ls.get("lfoTarget", -1)),
                    lfo_rate=float(ls.get("lfoRate", 0.0)),
                    lfo_depth=float(ls.get("lfoDepth", 0.0)),
                    lfo_shape=int(ls.get("lfoShape", 0)),
                ))
            slot.chain = chain
        self.fx_tracks = {int(t) for t in (m.get("fxTracks") or [])
                          if 0 <= int(t) < N_TRACKS}
        for t in range(N_TRACKS):
            self.bridge.route(t, t in self.fx_tracks)
        self._sync_fx_live()
        want_run = bool(m.get("running", False))
        self.running = want_run
        self.bridge.run(want_run)

    def save_project(self, slot: int, name: str | None = None) -> bool:
        """Save the live machine into a slot. The blink window is opened FIRST so the
        hardware shows the save even though the write itself is over in milliseconds."""
        with self._lock:
            self._saving = slot
            self._saving_until = time.monotonic() + SAVE_BLINK_SEC
            doc = self.model.snapshot()
            doc["machine"] = self.machine_snapshot()
            ok = self.projects.save(slot, doc, name)
        self._notify(("SAVED %d" % (slot + 1)) if ok else "SAVE FAILED")
        self._dirty = True
        return ok

    def load_project(self, slot: int) -> bool:
        doc = self.projects.load(slot)
        if doc is None:
            self._notify("SLOT %d EMPTY" % (slot + 1))
            return False
        with self._lock:
            self.model.apply(doc)
            self._confine_to_library()
            # Push the whole machine at the engine: a project changes every parameter and
            # every sample at once, so this is a full re-assert rather than a diff.
            self.bridge.master(self.model.master)
            for t in self.model.tracks:
                t.loaded = False
                self.bridge.mute(t.index, t.mute)
                self.bridge.params(t.index, [(SPECS[k].osc_key, v) for k, v in t.values.items()])
                if t.sample_path and os.path.exists(t.sample_path):
                    self.bridge.load(t.index, t.sample_path)
                else:
                    self.bridge.free(t.index)
            # The head travels with the parameters above, but the voice does not exist
            # until its sample finishes loading, so the position is re-asserted as a jump
            # once it does — see _on_loaded.
            self._pending_head = {t.index: t.value("position")
                                  for t in self.model.tracks if t.sample_path}
            self._apply_machine(doc.get("machine") or {})
        self._notify("LOADED %d" % (slot + 1))
        self._dirty = True
        return True

    def set_track_sample(self, track: int, path: str | None) -> bool:
        """Assign a sample to a track (the web UI's core action)."""
        if not 0 <= track < N_TRACKS:
            return False
        with self._lock:
            t = self.model.tracks[track]
            if path is None:
                t.sample_path, t.sample_name, t.loaded = None, "—", False
                self.bridge.free(track)
            else:
                real = os.path.realpath(path)
                root = os.path.realpath(SAMPLES_DIR)
                # The web UI only ever offers library paths, but the API is reachable by
                # anything on the network — the boundary is checked here, not assumed.
                if not (real == root or real.startswith(root + os.sep)):
                    return False
                if not os.path.exists(real):
                    return False
                path = real
                t.sample_path = path
                t.sample_name = Path(path).stem[:16]
                t.loaded = False
                self.bridge.load(track, path)
        self._dirty = True
        return True

    def rescan_samples(self) -> int:
        self._pool = samples_mod.discover((SAMPLES_DIR,))
        self._dirty = True
        return len(self._pool)

    @property
    def pool(self) -> list[str]:
        return self._pool

    def _notify(self, text: str, seconds: float = 1.6) -> None:
        self._notice = text
        self._notice_until = time.monotonic() + seconds

    # -- control.json (UI -> controller) ------------------------------------ #
    def _control_loop(self) -> None:
        period = 1.0 / max(10.0, CONTROL_HZ)
        while not self._stop.is_set():
            self._read_control()
            time.sleep(period)

    def _gesture_loop(self) -> None:
        """Its own thread at 200 Hz. The control loop polls at 30 Hz, and 33 ms of jitter
        on a jump cut is audible as sloppy timing — the whole point of the gesture is that
        the cuts land where you put them."""
        while not self._stop.is_set():
            with self._lock:
                self._gesture_tick()
            time.sleep(0.005)

    def _read_control(self) -> None:
        try:
            raw = CONTROL_FILE.read_text()
        except OSError:
            return
        if not raw:
            return
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return                      # partial write; try again next poll
        # The UI's seq rises within a session but RESETS when the module reloads. If it
        # dropped, resync the dedup — otherwise every post-reload gesture whose seq is
        # below our high-water mark is silently discarded and the hardware looks dead.
        ui_seq = doc.get("seq")
        if isinstance(ui_seq, (int, float)) and ui_seq < self._last_seq:
            self._last_seq = -1

        cmds = doc.get("cmds")
        if not self._seq_primed:
            self._seq_primed = True
            try:
                stale = CONTROL_FILE.stat().st_mtime < self._started_at
            except OSError:
                stale = True
            if stale:
                seqs = [e.get("seq", 0) for e in cmds] if isinstance(cmds, list) else []
                if isinstance(ui_seq, (int, float)):
                    seqs.append(ui_seq)
                if seqs:
                    self._last_seq = max(seqs)
                return
        # Until the engine answers there is no graph to talk to. Swallow the queue but
        # keep the high-water mark, so nothing replays once it comes up.
        if not self._built.is_set():
            if isinstance(cmds, list) and cmds:
                self._last_seq = max([self._last_seq] + [e.get("seq", 0) for e in cmds])
            return
        if not isinstance(cmds, list):
            return
        newest = self._last_seq
        for e in cmds:
            s = e.get("seq", 0)
            if s > self._last_seq:
                with self._lock:
                    self._dispatch(e.get("cmd", ""), e.get("arg", -1), e.get("p") or {})
                newest = max(newest, s)
        self._last_seq = newest

    def _dispatch(self, cmd: str, arg, p: dict) -> None:
        if cmd == "encoder":
            # The one hot path: a detent on column `arg`.
            track = int(p.get("track", arg))
            ticks = int(p.get("ticks", 0))
            changed = self.model.nudge_encoder(track, ticks)
            if changed:
                # One coalesced message for the whole macro group.
                self.bridge.params(track, [(SPECS[k].osc_key, v) for k, v in changed])
            self.model.focus = track
            self._dirty = True

        elif cmd == "jump":
            # A step button taps the playhead straight to that sixteenth of the sample.
            # The step is a POSITION, not an offset, so a repeated tap is a repeated cut to
            # the same place — which is the whole point of the gesture.
            track = int(arg)
            step = int(p.get("step", 0))
            if 0 <= track < N_TRACKS and 0 <= step < 16:
                pos = step / 16.0
                t = self.model.tracks[track]
                t.set_value("position", pos)
                # The engine's own head report is what the step bar draws, so the local
                # value only has to hold until the next telemetry frame arrives.
                t.head = pos
                self.bridge.jump(track, pos)
                # Captured if Rec is held: the same press that moves the head is the one
                # that gets recorded, so there is no separate "record" gesture to learn.
                self.gesture_capture(track, step)
                self.model.focus = track
                self._dirty = True

        elif cmd == "gestrec":
            self.gesture_record(bool(p.get("on")))

        elif cmd == "gestclear":
            self.gesture_clear()

        elif cmd == "gestclearall":
            self.gesture_clear_all()

        elif cmd == "focus":
            self.model.focus = max(0, min(N_TRACKS - 1, int(arg)))
            self._dirty = True

        elif cmd == "macro":
            # Toggle one of the four parameter pads under a column. Deliberately NOT
            # mutually exclusive: several on = the encoder becomes a macro over them,
            # none on = the encoder returns to the sample scan/playhead.
            self.model.toggle_macro(int(p.get("track", arg)), int(p.get("slot", -1)))
            self._dirty = True

        elif cmd == "mute":
            t = self.model.tracks[int(arg)]
            t.mute = not t.mute
            self.bridge.mute(t.index, t.mute)
            self._dirty = True

        elif cmd == "param":
            # Direct set of any Granola parameter, normalized — the generic path other
            # views will use.
            track = int(p.get("track", arg))
            key = str(p.get("param", ""))
            if key in SPECS and 0 <= track < N_TRACKS:
                t = self.model.tracks[track]
                if t.set_normalized(key, float(p.get("value", 0.0))):
                    self.bridge.params(track, [(SPECS[key].osc_key, t.value(key))])
                self._dirty = True

        elif cmd == "sample":
            self._cycle_sample(int(p.get("track", arg)), int(p.get("dir", 1)))

        elif cmd == "fxslot":
            i = int(arg)
            want = p.get("on")
            self.set_fx_slot(i, (not self.fx_slots[i].active) if want is None else bool(want))

        elif cmd == "fxreroll":
            self.reroll_fx_slot(int(arg))

        elif cmd == "harvest":
            self.start_harvest()

        elif cmd == "harvestbatch":
            self.set_harvest_batch(ticks=int(p.get("ticks", 0)))

        elif cmd == "panic":
            # A hardware kill switch. Play only gates the grain clocks, so it cannot stop
            # a master effect whose feedback state has run away; this frees the voices,
            # stops any audition and rebuilds the reverb and delay.
            self.running = False
            self.bridge.run(False)
            self.bridge.panic()
            self._auditioning = None
            self._notify("PANIC — FX REBUILT")

        elif cmd == "audition":
            self.audition(int(arg))

        elif cmd == "auditionoff":
            self.audition_off()

        elif cmd == "harvestassign":
            self.assign_harvested(int(p.get("sample", -1)), int(p.get("track", -1)))

        elif cmd == "fxwet":
            self.set_fx_wet(int(arg), ticks=int(p.get("ticks", 0)))

        elif cmd == "fxlock":
            want = p.get("locked")
            self.set_fx_lock(int(arg), None if want is None else bool(want))

        elif cmd == "fxtrack":
            t = int(arg)
            want = p.get("on")
            self.set_fx_track(t, (t not in self.fx_tracks) if want is None else bool(want))

        elif cmd == "saveproj":
            # Released outside the lock: save_project takes it itself, and _dispatch is
            # already holding a reentrant lock so this is safe either way.
            self.save_project(int(arg))

        elif cmd == "loadproj":
            self.load_project(int(arg))

        elif cmd == "rescan":
            self._pool = samples_mod.discover((SAMPLES_DIR,))
            self._notify("%d SAMPLES" % len(self._pool))
            self._dirty = True

        elif cmd == "transport":
            # `arg` is the requested state, or -1 to toggle.
            want = int(arg)
            self.running = (not self.running) if want < 0 else bool(want)
            self.bridge.run(self.running)
            self._dirty = True

        elif cmd == "master":
            self.model.master = max(0.0, min(1.4, float(p.get("value", 0.8))))
            self.bridge.master(self.model.master)
            self._dirty = True

        elif cmd == "reset":
            track = int(arg)
            if 0 <= track < N_TRACKS:
                t = self.model.tracks[track]
                t.reset_parameters()
                self.bridge.params(track, [(SPECS[k].osc_key, v) for k, v in t.values.items()])
                self._notify("T%d RESET" % (track + 1))
                self._dirty = True

        elif cmd == "panic":
            self.bridge.panic()
            self._load_all_samples()

    # -- status.json (controller -> UI) ------------------------------------- #
    def _status_loop(self) -> None:
        period = 1.0 / max(4.0, STATUS_HZ)
        while not self._stop.is_set():
            self._write_status()
            time.sleep(period)

    def _write_status(self) -> None:
        with self._lock:
            tracks = []
            for t in self.model.tracks:
                params = t.macro_params
                # The value the UI shows for this column: the single selected parameter,
                # or the playhead when none is. With several selected there is no single
                # number, so the label carries the count instead.
                if len(params) == 1:
                    readout = format_value(params[0], t.value(params[0]))
                elif params:
                    readout = "+".join(SPECS[p].short for p in params)
                else:
                    readout = "%d%%" % round(t.head * 100)
                tracks.append({
                    "name": t.sample_name,
                    "loaded": t.loaded,
                    "dur": round(t.duration, 3),
                    "mute": t.mute,
                    # The pads' toggle state, as four booleans in pad order.
                    "sel": [s in t.macros for s in MACRO_SLOTS],
                    # The TRUE playhead as the engine reports it — position plus any
                    # free-running scan — which is what the 16 step buttons draw.
                    #
                    # Except on a track with no sample: there is no voice, so nothing
                    # reports a head and the bar would sit at zero however far the encoder
                    # was turned. That reads as a broken control rather than as an empty
                    # track, so fall back to the MODEL's position. The screen still names
                    # the track empty, which is where "why is there no sound" is answered.
                    "head": round(t.head if t.loaded else t.value("position"), 4),
                    "label": t.macro_label,
                    "readout": readout,
                    # Normalized values of the four macro parameters, for the display.
                    "vals": [round(t.normalized(s), 4) for s in MACRO_SLOTS],
                })
            saving = self._saving if time.monotonic() < self._saving_until else -1
            status = {
                "ready": self.bridge.ready,
                "running": self.running,
                "engine": self.bridge.connected,
                "cpu": round(self.bridge.cpu.get("avg", 0.0), 1),
                "loaded": sum(1 for t in self.model.tracks if t.loaded),
                "focus": self.model.focus,
                "master": round(self.model.master, 3),
                "level": round(max(self.bridge.master_amp), 3),
                "pool": len(self._pool),
                "notice": self._notice if time.monotonic() < self._notice_until else "",
                "fxSlots": [s.active for s in self.fx_slots],
                "fxLocked": [s.locked for s in self.fx_slots],
                "fxWet": [round(s.wet, 3) for s in self.fx_slots],
                "fxLabels": [s.label for s in self.fx_slots],
                "fxTracks": sorted(self.fx_tracks),
                "fxSummary": self.fx_summary,
                # Armed slots exist; whether they are AUDIBLE depends on a track being
                # routed. The UI needs both to avoid a lit pad that makes no sound.
                "fxLive": bool(self.fx_tracks),
                "harvest": self.harvest_state,
                "rec": self.gesture_state,
                "fxCount": len(self.catalog),
                "fxCost": round(self.fx_cost, 1),
                "fxBudget": aw.FX_CPU_BUDGET,
                "projFilled": self.projects.filled,
                "projCur": self.projects.current,
                "saving": saving,
                "tracks": tracks,
            }
        # Skip the write when nothing the UI can see has changed — every write is SD I/O,
        # and SD stalls are what freeze the Schwung host's tick. CPU and node count are
        # excluded from the comparison because it never stops moving; per-track levels are
        # not published at all for the same reason (View 1 draws no meters).
        key = json.dumps({k: v for k, v in status.items() if k != "cpu"})
        if key == self._last_status_key:
            return
        self._last_status_key = key
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(status))
            tmp.replace(STATUS_FILE)
        except OSError:
            pass

    # -- telemetry -> model -------------------------------------------------- #
    def _autosave_loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            # Fold engine telemetry into the model at the status rate, so status.json is
            # written from one place and the UI never reads a half-updated track.
            with self._lock:
                for i, t in enumerate(self.model.tracks):
                    t.head = self.bridge.heads[i]
                    t.meter = self.bridge.meters[i]
            now = time.monotonic()
            if self._dirty and (now - last) >= AUTOSAVE_SEC:
                with self._lock:
                    self.model.save(MODEL_FILE)
                self._dirty = False
                last = now
            time.sleep(0.05)


def main() -> None:
    ctl = Controller()
    signal.signal(signal.SIGTERM, ctl.stop)
    signal.signal(signal.SIGINT, ctl.stop)
    ctl.start()
    print("[granola] controller up (sc %s:%d, telemetry :%d)"
          % (SC_HOST, SC_PORT, TELEMETRY_PORT), flush=True)
    ctl.run()


if __name__ == "__main__":
    main()
