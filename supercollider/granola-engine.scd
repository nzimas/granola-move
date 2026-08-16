// Granola — on-device engine host (Ableton Move).
//
// This is the layer the macOS app implemented in Swift (Engine/GranolaEngine.swift):
// it builds the graph, owns buffers and voices, and answers parameter changes. The
// fixed bus / group / buffer map is carried over UNCHANGED so the two versions
// address the same graph, and so any part of the system can talk to a track's synth
// without holding a reference to it.
//
// On macOS the client spoke raw scsynth OSC. On the Move the client is a Python
// controller (which owns the parameter model and the UI-facing state), so the wire
// protocol is a small semantic API — /gr/... — rather than /s_new and /n_set. The
// controller therefore never has to know node ids, and a graph rebuild here cannot
// desynchronise it.
//
// Inbound (sclang, port 57120):
//   /gr/ping                                  -> replies /gr/ready
//   /gr/load    track path                    load a sample + (re)start its voice
//   /gr/param   track key val [key val ...]   set voice / strip controls
//   /gr/mute    track 0|1
//   /gr/run     0|1                       transport: start / stop every granulator
//   /gr/master  amp
//   /gr/reverb  key val [key val ...]
//   /gr/delay   key val [key val ...]
//   /gr/free    track
//   /gr/route   track 0|1                 crossfade a track into the performance chain
//   /gr/fxadd   node defName mix fade autoGain lfoTarget lfoRate lfoDepth lfoShape p...
//   /gr/fxfree  node fade                 release a link; the chain closes up behind it
//   /gr/fxgain  0|1                       chain-wide gain matching at the terminator
//   /gr/fxmix   node mix                  one link's dry/wet blend
//   /gr/excerpt src dst seconds tag       choose + write an excerpt (harvester)
//   /gr/audition path                     play a file to the mix (hold-to-hear)
//   /gr/auditionoff                       stop it
//   /gr/panic
//
// Outbound (to CONTROLLER_HOST:CONTROLLER_PORT):
//   /gr/ready
//   /gr/cpu     avgCPU peakCPU numSynths
//   /gr/loaded  track durationSeconds numChannels
//   /gr/loadfail track
//   /gr/head    h0 h1 .. h7      the TRUE playhead per track (pos + free scan)
//   /gr/meter   m0 m1 .. m7      per-track amplitude
//   /gr/master  ampL ampR
//   /gr/excerpted  tag path seconds rms   an excerpt was written
//   /gr/excerptfail tag                   the source was unusable (silent / too short)

(
var s = Server.default;
var ctlHost = "CONTROLLER_HOST".getenv ?? "127.0.0.1";
var ctlPort = ("CONTROLLER_PORT".getenv ?? "57141").asInteger;
var here = thisProcess.nowExecutingPath.dirname;

~grN = 8;
~grCtl = NetAddr(ctlHost, ctlPort);
// THE HANDSHAKE MUST NOT LIE. The OSC handlers below are live as soon as this file is
// evaluated, which is ~15 seconds before waitForBoot has a server and a graph. Answering
// /gr/ping during that window told the controller the engine was up, so it pushed the
// whole model — samples included — at a server that did not exist yet, and then never
// pushed again because the handshake had already been satisfied. Every track stayed
// silent with nothing logged anywhere. `~grReady` is the one flag that gates the answer.
~grReady = false;

// --- fixed graph addresses (identical to GranolaEngine.swift) --------------
~grGroup = (voices: 10, strips: 11, effects: 12, master: 13, performance: 14);
~grBus = (
    mix: 16, reverb: 18, record: 20, delay: 22,
    performance: 40, chainLevel: 8
);
~grTrackBus = { |i| 24 + (i * 2) };
~grBufL = { |i| i * 2 };
~grBufR = { |i| (i * 2) + 1 };
~grEnvBase = 200;
// Where the Airwindows effect SynthDefs live on the device.
~grFxDefs = ("GR_HOME".getenv ?? "/data/UserData/granola") +/+ "synthdefs";
// Each voice's true scrub head, pushed up from the graph by SendReply.
~grHead = Array.fill(8, 0.0);

// The authoritative parameter model lives in the controller, but the engine keeps the
// last value it was told for every voice control so that a voice rebuilt after a sample
// load comes up with the track's real settings rather than SynthDef defaults. Reloading
// a sample, restarting the UI or redrawing LEDs therefore cannot reset synthesis state.
~grPending = Array.fill(~grN, { () });
~grPendingSet = { |i, k, v| ~grPending[i][k] = v };

~grVoices = Array.newClear(~grN);      // Synth or nil
~grLoaded = Array.fill(~grN, false);
~grChans = Array.fill(~grN, 0);
~grDur = Array.fill(~grN, 0.0);
// Transport. Held here as well as in the controller so a voice REBUILT while the
// transport is stopped (a sample load) comes up stopped rather than suddenly sounding.
~grRun = 0;
~grPaths = Array.fill(~grN, "");
~grMeter = Array.fill(~grN, 0.0);

// --- grain windows: a MORPH TABLE ------------------------------------------
// The app's four GrainShape cases, same maths, but reachable as a continuum rather than
// as four buttons. GrainBuf takes a BUFFER for its window and reads it at grain onset,
// so a continuous morph cannot be computed in the SynthDef — the interpolated windows
// have to exist. They are therefore pre-rendered once at boot into a table of buffers,
// and the encoder simply picks one; a grain that starts a moment later picks up the new
// window, which is what makes the change sound continuous rather than switched.
//
// Rewriting the window in place instead would mean pushing 1024 floats per encoder
// detent and mutating a buffer while the audio thread is reading it. A table costs
// 64 x 1024 floats (256 kB) once, and nothing per gesture.
~grShapeFn = (
    1: { |t| exp(-0.5 * (((t - 0.5) / 0.22) ** 2)) },                 // gaussian
    2: { |t| ((1 - t) ** 3) * (1 - exp(t * -60)) },                   // percussive
    3: { |t| var r = 1 - t; ((1 - r) ** 3) * (1 - exp(r * -60)) },    // reverse
    4: { |t| var e = 0.15;                                            // plateau (Tukey)
            if (t < e) { 0.5 - (0.5 * cos(pi * t / e)) } {
            if (t > (1 - e)) { 0.5 - (0.5 * cos(pi * (1 - t) / e)) } { 1.0 } } }
);
// The order the morph travels through, as asked for: gaussian -> percussive -> plateau
// -> reverse. Adjacent shapes crossfade; the ends do not wrap.
~grShapeSeq = [1, 2, 4, 3];
~grMorphN = 64;                        // steps across the whole path (~21 per transition)

// ALLOCATION AND FILL CANNOT BE SENT BACK TO BACK. /b_alloc is asynchronous — the
// server defers it to its command thread — while /b_setn executes immediately on the
// audio thread. Sent together, the data arrives before the buffer exists and is
// discarded, leaving a window full of zeros; a grain windowed by zeros is silence.
// The macOS app hit exactly this and fixed it with a /sync between the two phases.
// Must be called from inside a Routine (it waits on the server).
~grBuildEnvelopes = {
    var size = 1024;
    var n = ~grMorphN;
    var nSeg = ~grShapeSeq.size - 1;
    var bufs = Array.newClear(n);
    n.do { |k| bufs[k] = Buffer.alloc(s, size, 1, nil, ~grEnvBase + k) };
    s.sync;
    n.do { |k|
        var m = k / (n - 1);
        var seg = m * nSeg;
        var i0 = seg.floor.asInteger.clip(0, nSeg - 1);
        var f = seg - i0;
        var fa = ~grShapeFn[~grShapeSeq[i0]];
        var fb = ~grShapeFn[~grShapeSeq[i0 + 1]];
        var sig = Signal.newClear(size);
        size.do { |j|
            var t = j / (size - 1);
            sig[j] = (fa.value(t) * (1 - f)) + (fb.value(t) * f);
        };
        // NOT normalised, and that is deliberate. Normalising each blend to unit peak was
        // tried and is wrong twice over: the four anchor shapes have genuinely different
        // energies in the app (a percussive window carries far less than a plateau), and
        // rescaling the blends breaks that relationship — and because a crossfade of two
        // differently-placed peaks has a LOWER peak than either, dividing by it pushed the
        // in-between windows ABOVE both neighbours. Measured: the mid-blends came out
        // louder than gaussian and percussive alike, so the sweep bulged instead of
        // travelling. A plain crossfade makes the window's energy move linearly between
        // its neighbours, which is what turning the encoder should sound like.
        bufs[k].sendCollection(sig);
    };
};

// Morph position (0..1) -> the buffer holding that window.
~grEnvBufFor = { |m| ~grEnvBase + (m.clip(0, 1) * (~grMorphN - 1)).round.asInteger };

// --- graph -----------------------------------------------------------------
~grBuildGraph = {
    // Execution order is head-to-tail: voices, strips, effects, performance, master.
    s.sendMsg(\g_new, ~grGroup[\voices], 0, 0);
    s.sendMsg(\g_new, ~grGroup[\strips], 3, ~grGroup[\voices]);
    s.sendMsg(\g_new, ~grGroup[\effects], 3, ~grGroup[\strips]);
    s.sendMsg(\g_new, ~grGroup[\performance], 3, ~grGroup[\effects]);
    s.sendMsg(\g_new, ~grGroup[\master], 3, ~grGroup[\performance]);

    ~grN.do { |i|
        s.sendMsg(\s_new, \granolaStrip, 1100 + i, 0, ~grGroup[\strips],
            \in, ~grTrackBus.value(i),
            \mixOut, ~grBus[\mix],
            \perfOut, ~grBus[\performance],
            \reverbOut, ~grBus[\reverb],
            \delayOut, ~grBus[\delay],
            \index, i);
    };

    // The terminator must exist before any link, since links are always inserted
    // immediately in front of it; the head is added afterwards so it lands in front.
    s.sendMsg(\s_new, \granolaPerfOut, 1500, 0, ~grGroup[\performance],
        \in, ~grBus[\performance], \out, ~grBus[\mix], \levelBus, ~grBus[\chainLevel]);
    s.sendMsg(\s_new, \granolaPerfIn, 1490, 0, ~grGroup[\performance],
        \in, ~grBus[\performance], \levelBus, ~grBus[\chainLevel]);

    s.sendMsg(\s_new, \granolaReverb, 1200, 0, ~grGroup[\effects],
        \in, ~grBus[\reverb], \out, ~grBus[\performance]);
    s.sendMsg(\s_new, \granolaDelay, 1250, 0, ~grGroup[\effects],
        \in, ~grBus[\delay], \out, ~grBus[\performance]);
    s.sendMsg(\s_new, \granolaMaster, 1300, 0, ~grGroup[\master],
        \in, ~grBus[\mix], \out, 0, \recOut, ~grBus[\record]);
};

// --- voices ----------------------------------------------------------------
// Voices must NOT use a fixed id: freeing one starts a release envelope, so the old
// node is briefly still alive and a replacement with the same id is rejected as a
// duplicate — the track then goes silent. Synth() allocates a fresh id each time,
// which is exactly what the app's nextVoiceNode counter was doing by hand.
~grFreeVoice = { |i|
    var v = ~grVoices[i];
    if (v.notNil) { v.set(\gate, 0); ~grVoices[i] = nil; };
};

~grStartVoice = { |i, params|
    var args;
    if (~grLoaded[i]) {
        ~grFreeVoice.value(i);
        args = [
            \out, ~grTrackBus.value(i),
            \bufL, ~grBufL.value(i),
            \bufR, if (~grChans[i] >= 2) { ~grBufR.value(i) } { ~grBufL.value(i) },
            \envBuf, ~grEnvBase,
            \index, i,
            \run, ~grRun
        ];
        if (params.notNil) { params.keysValuesDo { |k, v| args = args ++ [k.asSymbol, v] } };
        ~grVoices[i] = Synth(\granolaVoice, args, ~grGroup[\voices]);
    };
};

// --- buffer loading --------------------------------------------------------
// A buffer read is asynchronous, so the voice must not start until it has finished — a
// cloud built over a buffer that is not filled yet plays whatever was in that memory.
//
// Getting that completion edge here took two attempts, both worth recording:
//
//   * A Routine + `s.sync` — the obvious sclang idiom — DEADLOCKS when forked from an
//     OSC responder: measured, the fork is entered and the /synced reply never resumes
//     it, so every load stalls forever and logs nothing.
//   * `Buffer.readChannel(..., action, bufnum)` never fires its action under this
//     sclang (3.13): measured with /b_query, the buffer is still unallocated afterwards
//     and the server has been sent nothing.
//
// So the load is done with the raw asynchronous command and an EXPLICIT /sync, whose
// reply is the completion edge. /sync is defined to be answered only once every async
// command sent before it has finished, which is exactly the guarantee needed — and it
// is a callback, not a wait, so nothing can block the OSC thread. Everything here uses
// only sendMsg and an OSCdef, both of which are proven to work in this environment.
// Ids come from UniqueID, NOT a private counter: Server:sync draws its own /sync ids
// from UniqueID too, and a private sequence collides with them — SC's boot-time syncs
// then fire OUR callbacks (measured: a load callback ran during boot, before the graph
// existed). Sharing the one allocator is what makes the ids unique.
~grSyncWait = IdentityDictionary.new;

OSCdef(\grSynced, { |msg|
    var id = msg[1].asInteger;
    var fn = ~grSyncWait[id];
    if (fn.notNil) { ~grSyncWait.removeAt(id); fn.value };
}, '/synced', s.addr);

~grLoadSample = { |i, path|
    var sf = SoundFile.new;
    var p = path.standardizePath;
    if (sf.openRead(p).not) {
        ("[granola] cannot read " ++ p).postln;
        ~grCtl.sendMsg("/gr/loadfail", i);
    } {
        var chans = sf.numChannels;
        var dur = sf.numFrames / max(sf.sampleRate, 1);
        var id = UniqueID.next;
        sf.close;
        ~grFreeVoice.value(i);
        // GrainBuf reads mono buffers only, so a stereo file is granulated as two mono
        // clouds sharing one trigger; a file with more channels contributes its first two.
        // numFrames 0 = the whole file.
        s.sendMsg(\b_allocReadChannel, ~grBufL.value(i), p, 0, 0, 0);
        if (chans >= 2) { s.sendMsg(\b_allocReadChannel, ~grBufR.value(i), p, 0, 0, 1) };
        ~grSyncWait[id] = {
            ~grLoaded[i] = true; ~grChans[i] = chans; ~grDur[i] = dur; ~grPaths[i] = p;
            // Start the voice carrying whatever the controller has already told us about
            // this track, so a reload cannot quietly reset it to SynthDef defaults.
            ~grStartVoice.value(i, ~grPending[i]);
            ~grCtl.sendMsg("/gr/loaded", i, dur, chans);
            ("[granola] track % <- % (%s, %ch)"
                .format(i, PathName(p).fileName, dur.round(0.01), chans)).postln;
        };
        s.sendMsg(\sync, id);
    };
};

// --- OSC API ---------------------------------------------------------------
~grDefs = [];
~grOSC = { |path, fn| ~grDefs = ~grDefs.add(OSCdef(("gr" ++ path).asSymbol, fn, path)) };

~grOSC.value("/gr/ping", { if (~grReady) { ~grCtl.sendMsg("/gr/ready") } });

~grOSC.value("/gr/load", { |msg|
    var i = msg[1].asInteger, path = msg[2].asString;
    // Belt and braces: a command that arrives before the graph exists is dropped LOUDLY
    // rather than sent into a void, so this failure can never be silent again.
    if (~grReady.not) { ("[granola] dropped /gr/load (engine not ready): " ++ path).postln } {
        if (i.inclusivelyBetween(0, ~grN - 1)) { ~grLoadSample.value(i, path) };
    };
});

// Parameters arrive as key/value pairs. The strip owns level, mute and the two
// sends; everything else is the voice (ParamID.target in the app).
~grStripKeys = [\amp, \mute, \reverbSend, \delaySend, \routing];
~grOSC.value("/gr/param", { |msg|
    var i = msg[1].asInteger, voice, strip;
    if (i.inclusivelyBetween(0, ~grN - 1)) {
        voice = []; strip = [];
        (2, 4 .. msg.size - 2).do { |x|
            var k = msg[x].asSymbol, v = msg[x + 1].asFloat;
            // The grain window is a parameter to everything upstream, and a buffer number
            // down here. Translating at the boundary keeps the table's size and layout a
            // detail of the engine — nothing else has to know how many steps it has.
            if (k == \envMorph) { k = \envBuf; v = ~grEnvBufFor.value(v) };
            if (~grStripKeys.includes(k)) { strip = strip ++ [k, v] } { voice = voice ++ [k, v] };
        };
        // Remember voice settings so a reload/restart re-applies them: the UI must
        // never be the sole owner of synthesis state.
        voice.pairsDo { |k, v| ~grPendingSet.value(i, k, v) };
        if (strip.size > 0) { s.sendMsg(\n_set, 1100 + i, *strip) };
        if ((voice.size > 0) and: { ~grVoices[i].notNil }) {
            s.sendMsg(\n_set, ~grVoices[i].nodeID, *voice);
        };
    };
});

~grOSC.value("/gr/mute", { |msg|
    var i = msg[1].asInteger;
    if (i.inclusivelyBetween(0, ~grN - 1)) { s.sendMsg(\n_set, 1100 + i, \mute, msg[2].asFloat) };
});

~grOSC.value("/gr/master", { |msg| s.sendMsg(\n_set, 1300, \amp, msg[1].asFloat) });

// Transport. One message moves every voice: the granulators are a single instrument, so
// they start and stop together rather than eight nodes drifting apart by a few blocks.
~grOSC.value("/gr/run", { |msg|
    ~grRun = msg[1].asInteger.clip(0, 1);
    s.sendMsg(\n_set, ~grGroup[\voices], \run, ~grRun);
});

// The last values sent for each, REMEMBERED — so panic can rebuild the two synths and put
// them back exactly as they were instead of silently reverting to SynthDef defaults.
~grFxState = IdentityDictionary[\reverb -> IdentityDictionary.new,
                               \delay -> IdentityDictionary.new];

~grFxSet = { |which, node, msg|
    var st = ~grFxState[which], a = [];
    (1, 3 .. msg.size - 2).do { |x|
        var k = msg[x].asSymbol, v = msg[x + 1].asFloat;
        st[k] = v;
        a = a ++ [k, v];
    };
    if (a.size > 0) { s.sendMsg(\n_set, node, *a) };
};

~grPushFx = {
    [[\reverb, 1200], [\delay, 1250]].do { |pair|
        var a = [];
        ~grFxState[pair[0]].keysValuesDo { |k, v| a = a ++ [k, v] };
        if (a.size > 0) { s.sendMsg(\n_set, pair[1], *a) };
    };
};

~grOSC.value("/gr/reverb", { |msg| ~grFxSet.value(\reverb, 1200, msg) });
~grOSC.value("/gr/delay",  { |msg| ~grFxSet.value(\delay, 1250, msg) });

// A JUMP CUT, not a scrub.
//
// The scrub head is de-zippered by posLag so an encoder sweep does not zipper; but that
// same lag turns a step-button jump into a 50 ms glide THROUGH everything between the two
// positions, which is a zip, not a cut. So the lag is dropped to zero for the jump and
// restored a moment later, leaving the encoder's smoothing intact.
//
// Grains already in flight are untouched — they finish where they started, which is what
// keeps the cut from clicking.
~grJumpRestore = 0.06;

~grOSC.value("/gr/jump", { |msg|
    var i = msg[1].asInteger, p = msg[2].asFloat.clip(0, 1);
    if (i.inclusivelyBetween(0, ~grN - 1) and: { ~grVoices[i].notNil }) {
        var node = ~grVoices[i];
        var back = (~grPending[i] !? { |d| d[\posLag] }) ? 0.05;
        s.sendMsg(\n_set, node.nodeID, \posLag, 0.0, \pos, p);
        SystemClock.sched(~grJumpRestore, {
            s.sendMsg(\n_set, node.nodeID, \posLag, back);
            nil;
        });
    };
});

~grOSC.value("/gr/free", { |msg|
    var i = msg[1].asInteger;
    if (i.inclusivelyBetween(0, ~grN - 1)) { ~grFreeVoice.value(i); ~grLoaded[i] = false };
});

// --- performance FX chain --------------------------------------------------
// The Airwindows links live between granolaPerfIn and granolaPerfOut on the performance
// bus, and every one is created with addAction 2 (add-before) against the TERMINATOR, so
// it lands at the tail of whatever chain already exists. Series order is therefore just
// node order, and nothing needs re-patching when links come and go — the same scheme the
// macOS app used, carried over intact.
~grOSC.value("/gr/route", { |msg|
    var i = msg[1].asInteger;
    // A crossfade, not a bus reassignment: switching a strip's output bus changes the
    // signal in a single sample, which clicks. granolaStrip fades between two permanent
    // outputs instead, so routing a track mid-performance cannot.
    if (i.inclusivelyBetween(0, ~grN - 1)) { s.sendMsg(\n_set, 1100 + i, \routing, msg[2].asFloat) };
});

~grOSC.value("/gr/fxadd", { |msg|
    var node = msg[1].asInteger;
    var def = msg[2].asString;
    var args = [\bus, ~grBus[\performance],
                \mix, msg[3].asFloat, \fade, msg[4].asFloat, \autoGain, msg[5].asFloat,
                \lfoTarget, msg[6].asFloat, \lfoRate, msg[7].asFloat,
                \lfoDepth, msg[8].asFloat, \lfoShape, msg[9].asFloat];
    // Remaining values are the effect's own parameters, named a, b, c, ... as the
    // generated SynthDefs declare them.
    (10 .. msg.size - 1).do { |x|
        args = args ++ [(97 + x - 10).asAscii.asString.asSymbol, msg[x].asFloat];
    };
    s.sendMsg(\s_new, def, node, 2, 1500, *args);
});

~grOSC.value("/gr/fxfree", { |msg|
    // Set the release time BEFORE opening the gate: EnvGen reads it as the segment
    // starts, so a fade changed since the link was created still applies to its fade-out.
    s.sendMsg(\n_set, msg[1].asInteger, \fade, msg[2].asFloat, \gate, 0);
});

~grOSC.value("/gr/fxgain", { |msg| s.sendMsg(\n_set, 1500, \autoGain, msg[1].asFloat) });

// One link's dry/wet blend. The generated SynthDefs expose `mix` directly, so a slot's
// whole chain is moved by sending one of these per link.
~grOSC.value("/gr/fxmix", { |msg| s.sendMsg(\n_set, msg[1].asInteger, \mix, msg[2].asFloat) });

// --- harvesting: pick an excerpt and write it -------------------------------
// The harvester downloads and remuxes in Python, but CHOOSING the excerpt is a DSP
// question and belongs here: libsndfile has already decoded the Opus, and the Move has no
// numpy to do it in Python.
//
// Done entirely in SCLANG with SoundFile — no server buffers, no async callbacks. That is
// not a stylistic choice: Buffer's action callbacks do not fire reliably in this sclang
// (Buffer.readChannel's action never fires at all, measured earlier in this port), and a
// first version of this built on Buffer.read + Buffer.getn returned nothing whatsoever,
// not even an error. SoundFile reads and writes synchronously through the same libsndfile
// the server uses, so it decodes Ogg Opus just as well and cannot silently stall.
//
// It runs on the OSC responder thread and blocks sclang for a moment. That is fine: it is
// a harvest step measured in seconds of network time either side, and the audio thread is
// scsynth's, untouched by anything here.
// SampleHarvester's own render settings (GenerationConfig defaults), applied here because
// this is where the audio is: fadeInMilliseconds 10, fadeOutMilliseconds 20,
// targetLoudnessDB -1.0, boundaryAvoidance 0.05 with avoidBoundaries true.
// The loudest-window probe that used to live here is GONE: region choice is now the
// analysis engine's job, which qualifies regions instead of merely ranking their level.
~grBoundary = 0.05;             // boundaryAvoidance: skip the first and last 5%
~grFadeIn = 0.010;
~grFadeOut = 0.020;
~grTargetPeak = -1.0.dbamp;     // targetLoudnessDB: -1.0


// REGION CHOICE: probe a spread of start points and take the loudest.
//
// There was a full port of the app's AnalysisEngine/RegionEngine here — an STFT over the
// source plus 96 scored, gated candidate regions. It worked and its gates did reject
// spoken-word sources, but it cost 40-75 s of near-100% CPU PER SOURCE on a machine whose
// own application already sits at ~70% CPU and drops audio frames continuously. A batch of
// sixteen meant ten minutes of that, and the Move hard-reset with no OOM and no panic
// logged — the signature of a starved watchdog. It is removed rather than tuned: this
// device does not have the headroom for per-source DSP of that scale, and an instrument
// that reboots mid-set is worth nothing.
//
// Keeping the material musical is therefore entirely the DISCOVERY layer's job — the
// YouTube Music catalogue plus the term filters in harvester.py. That is text matching
// against metadata, it costs nothing, and it is where the leverage always was.
~grExcerptWindows = 24;
~grExcerptProbe = 2048;         // frames per probe window
~grExcerptFloor = 0.004;        // below this the source is unusable

~grOSC.value("/gr/excerpt", { |msg|
    var src = msg[1].asString, dst = msg[2].asString;
    var want = msg[3].asFloat, tag = msg[4].asInteger;
    var sf = SoundFile.new;
    if (sf.openRead(src.standardizePath).not) {
        ("[granola] harvest: cannot read " ++ src).postln;
        ~grCtl.sendMsg("/gr/excerptfail", tag);
    } {
        var ch = sf.numChannels.max(1), sr = sf.sampleRate.max(1).asInteger;
        var frames = sf.numFrames;
        var need = min((want * sr).asInteger, frames);
        var probe = min(~grExcerptProbe, need);
        // avoidBoundaries: the opening and closing 5% of a recording are where the
        // applause, the announcement and the fade live.
        var edge = (frames * ~grBoundary).asInteger;
        var lo = edge, hi = (frames - need - edge).max(edge);
        var span = (hi - lo).max(0);
        var best = lo, bestRms = -1;
        if ((frames < (sr * 0.5)) or: { need < 1 }) {
            ("[granola] harvest: % too short (% frames)"
                .format(PathName(src).fileName, frames)).postln;
            sf.close;
            ~grCtl.sendMsg("/gr/excerptfail", tag);
        } {
            ~grExcerptWindows.do { |k|
                var at = (lo + (span * k / (~grExcerptWindows - 1).max(1))).asInteger;
                var data = FloatArray.newClear(probe * ch), sum;
                sf.seek(at, 0);
                sf.readData(data);
                sum = ((data * data).sum / data.size.max(1)).sqrt;
                if (sum > bestRms) { bestRms = sum; best = at };
            };
            sf.close;
            if (bestRms < ~grExcerptFloor) {
                ("[granola] harvest: % is silent (rms %)"
                    .format(PathName(src).fileName, bestRms.round(0.0001))).postln;
                ~grCtl.sendMsg("/gr/excerptfail", tag);
            } {
                ~grWriteExcerpt.value(src, dst, best / sr, want, tag,
                                      20 * log10(max(1e-9, bestRms)));
            };
        };
    };
});

// Cutting the chosen region out of the ORIGINAL file, so the sample keeps the source's
// channels and rate — the analysis copy is mono and exists only to be measured.
~grWriteExcerpt = { |src, dst, startSec, want, tag, rmsDB|
    var sf = SoundFile.new;
    if (sf.openRead(src.standardizePath).not) {
        ("[granola] harvest: cannot read " ++ src).postln;
        ~grCtl.sendMsg("/gr/excerptfail", tag);
    } {
        var ch = sf.numChannels.max(1);
        var sr = sf.sampleRate.max(1).asInteger;
        var frames = sf.numFrames;
        var need = min((want * sr).asInteger, frames);
        var at = (startSec * sr).asInteger.clip(0, max(0, frames - need));
        var cut = FloatArray.newClear(need * ch);
        var out = SoundFile.new;
        var peak = 0, gain = 1, fi, fo;
        sf.seek(at, 0);
        sf.readData(cut);
        sf.close;
        // Normalise to the app's target peak, then fade the ends. Order matters:
        // normalising after the fades would undo them at the very edges.
        cut.do { |v| peak = max(peak, v.abs) };
        if (peak > 0.0001) { gain = ~grTargetPeak / peak };
        fi = (~grFadeIn * sr).asInteger.clip(1, need);
        fo = (~grFadeOut * sr).asInteger.clip(1, need);
        cut.size.do { |j|
            var frame = j div: ch;
            var env = 1;
            if (frame < fi) { env = frame / fi };
            if (frame > (need - fo)) { env = min(env, (need - frame) / fo) };
            cut[j] = (cut[j] * gain * env).clip(-1, 1);
        };
        out.headerFormat_("WAV").sampleFormat_("int16").numChannels_(ch).sampleRate_(sr);
        if (out.openWrite(dst.standardizePath).not) {
            ("[granola] harvest: cannot write " ++ dst).postln;
            ~grCtl.sendMsg("/gr/excerptfail", tag);
        } {
            out.writeData(cut);
            out.close;
            ("[granola] harvest: % <- % @ %s".format(
                PathName(dst).fileName, PathName(src).fileName, startSec.round(0.1))).postln;
            ~grCtl.sendMsg("/gr/excerpted", tag, dst, cut.size / ch / sr,
                           10.pow(rmsDB / 20));
        };
    };
};

// --- audition -------------------------------------------------------------- //
// Hold a harvested sample pad to hear it. Uses the SAME raw /b_allocRead + /sync pattern
// as the sample loader rather than Buffer's action callbacks, which do not fire reliably
// in this sclang.
//
// Buffer AND node are a small ring rather than one of each. A release fades over 60 ms, so
// moving quickly from pad to pad can start the next audition while the last is still
// sounding: reusing one node id risks a duplicate-ID failure, and reusing one buffer means
// freeing memory the releasing synth is still reading.
~grAudRing = 4;
~grAudBufBase = 400;
~grAudSlot = 0;
~grAudNode = nil;

~grStopAudition = {
    if (~grAudNode.notNil) { s.sendMsg(\n_set, ~grAudNode, \gate, 0); ~grAudNode = nil };
};

~grOSC.value("/gr/audition", { |msg|
    var path = msg[1].asString.standardizePath;
    var sf = SoundFile.new;
    ~grStopAudition.value;
    if (sf.openRead(path).not) {
        ("[granola] audition: cannot read " ++ path).postln;
    } {
        var ch = sf.numChannels.max(1).min(2);
        var id = UniqueID.next;
        var slot = ~grAudSlot;
        var buf = ~grAudBufBase + slot;
        ~grAudSlot = (slot + 1) % ~grAudRing;
        sf.close;
        s.sendMsg(\b_free, buf);
        s.sendMsg(\b_allocRead, buf, path, 0, 0);
        ~grSyncWait[id] = {
            // Auditioning goes to the MIX bus, so it is heard through the master exactly
            // like a track — and it plays whether or not the transport is running, since
            // the transport only gates the granular voices.
            //
            // It goes in the VOICES group, not the master group: granolaMaster reads the
            // mix bus and then CLEARS it, so anything writing to mix after it is silent.
            // The voices group runs first, which is exactly where a source belongs.
            //
            // Deliberately NOT routed through a track strip or the performance chain: this
            // is a "what does this sample sound like" monitor, and it should not change
            // depending on which track is routed or what chain is loaded.
            ~grAudNode = 1600 + slot;
            s.sendMsg(\s_new, if (ch >= 2) { \granolaAudition2 } { \granolaAudition1 },
                      ~grAudNode, 1, ~grGroup[\voices],
                      \buf, buf, \out, ~grBus[\mix]);
        };
        s.sendMsg(\sync, id);
    };
});

~grOSC.value("/gr/auditionoff", { ~grStopAudition.value });

// PANIC also REBUILDS the master effects.
//
// Freeing the voices does not stop a reverb or delay whose internal state has blown up:
// they hold their own feedback and will roar with a silent input, through a stopped
// transport, forever. Recreating the two synths is the only thing that clears that state,
// so panic does it — otherwise the instrument has no way back short of restarting the
// engine, which is what actually happened.
~grRebuildFx = {
    s.sendMsg(\n_free, 1200);
    s.sendMsg(\n_free, 1250);
    s.sendMsg(\s_new, \granolaReverb, 1200, 0, ~grGroup[\effects],
        \in, ~grBus[\reverb], \out, ~grBus[\performance]);
    s.sendMsg(\s_new, \granolaDelay, 1250, 0, ~grGroup[\effects],
        \in, ~grBus[\delay], \out, ~grBus[\performance]);
    // The rebuilt synths carry SynthDef defaults, so push the live values back.
    if (~grPushFx.notNil) { ~grPushFx.value };
    "[granola] panic: master effects rebuilt".postln;
};

~grOSC.value("/gr/panic", {
    ~grN.do { |i| ~grFreeVoice.value(i) };
    ~grStopAudition.value;
    ~grRebuildFx.value;
});

// --- telemetry -------------------------------------------------------------
OSCdef(\grHeadRx, { |msg|
    var i = msg[3].asInteger;
    if (i.inclusivelyBetween(0, ~grN - 1)) { ~grHead[i] = msg[4].asFloat };
}, '/granolaHead');

OSCdef(\grTrackMeter, { |msg|
    var i = msg[3].asInteger;
    if (i.inclusivelyBetween(0, ~grN - 1)) { ~grMeter[i] = msg[4].asFloat };
}, '/trackMeter');

OSCdef(\grMasterMeter, { |msg|
    ~grCtl.sendMsg("/gr/master", msg[3].asFloat, msg[4].asFloat);
}, '/masterMeter');

// --- boot ------------------------------------------------------------------
s.waitForBoot({
    (here +/+ "granola-synthdefs.scd").load;
    s.sync;
    // The 154 Airwindows effect SynthDefs. These are COMPILED defs copied from the macOS
    // build, not sources: a .scsyndef is a portable graph description, so both platforms
    // run the identical effect graph and only the UGen plugin ever needed rebuilding.
    // d_loadDir is asynchronous, hence the sync before anything can instantiate one.
    if (File.exists(~grFxDefs)) {
        s.sendMsg(\d_loadDir, ~grFxDefs);
        s.sync;
        ("[granola] airwindows: loaded % effect synthdefs".format(
            PathName(~grFxDefs).files.select { |f| f.extension == "scsyndef" }.size)).postln;
    } {
        ("[granola] airwindows: no synthdefs at " ++ ~grFxDefs ++ " — the FX view will be silent").postln;
    };
    ~grBuildEnvelopes.value;
    s.sync;
    ~grBuildGraph.value;
    s.sync;

    // Only now — server up, defs loaded, windows filled, graph built — is the engine
    // genuinely ready to be talked to.
    ~grReady = true;
    ~grCtl.sendMsg("/gr/ready");
    {
        loop {
            ~grCtl.sendMsg("/gr/cpu", s.avgCPU, s.peakCPU, s.numSynths);
            1.0.wait;
        };
    }.fork;

    // Forward the playhead and the meters to the controller at 20Hz. Both arrive here
    // from the graph via SendReply, so this loop only relays what it already has — it
    // never asks the server for anything and so can never accumulate responders.
    {
        loop {
            ~grCtl.sendMsg("/gr/head", *~grHead);
            ~grCtl.sendMsg("/gr/meter", *~grMeter);
            0.05.wait;
        };
    }.fork;

    "[granola] engine ready".postln;
    "SuperCollider 3 server ready.".postln;   // matched by run-engine.sh's boot wait
});
)
