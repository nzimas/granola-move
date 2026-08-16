// Granola — SynthDefs, ported from the macOS app's Scripts/synthdefs.scd.
//
// The DSP is UNCHANGED. The only difference is delivery: the macOS app compiled
// these ahead of time with sclang and had scsynth load the .scsyndef files, because
// the app never runs sclang. On the Move sclang IS the engine host (that is how every
// other takeover on this box works), so the same sources are simply `.add`ed here.
//
// Signal flow (identical to the app):
//   granolaVoice   -> trackBus[n]      grain cloud, one per track
//   granolaStrip   -> mixBus or perfBus  level/mute/meter + per-track sends
//   granolaReverb  -> perfBus            master effects run BEFORE the chain,
//   granolaDelay   -> perfBus            so a performance chain mangles them too
//   granolaPerfIn/Out                    the performance chain's head/tail
//   granolaMaster  -> hardware out       gain, limiter, meter, clears mixBus
//
// The Airwindows links that live between PerfIn and PerfOut on macOS are NOT here:
// their .scx is an x86/arm64 macOS build. The performance group is still constructed,
// so links can be inserted later without touching this file.

(
// A guard for anything that feeds a FEEDBACK network.
//
// The master reverb and delay both hold internal state across blocks. One non-finite
// sample entering either of them poisons that state permanently: the output pins to full
// scale, ignores its input, ignores the transport — because the transport only gates the
// grain clocks — and the only cure is freeing the synth. That is exactly the failure
// observed on the device, with the voice and strip groups already empty and the reverb and
// delay still roaring. NaNs cost nothing to reject and everything to let through, so every
// feedback path is sanitised and bounded before it can circulate.
var safe = { |sig|
    sig.asArray.collect { |ch|
        Select.ar(CheckBadValues.ar(ch, 0, 0) > 0, [ch, DC.ar(0)]).clip(-8, 8);
    };
};

// ---------------------------------------------------------------------------
// Track voice: one granular cloud per track.
//
// `pos` is the scrub head, 0..1 through the buffer — on the Move this is what the
// eight encoders drive when no parameter pad is lit. Everything else shapes the
// cloud around that head.
// ---------------------------------------------------------------------------
// GrainBuf only reads mono buffers, so a stereo sample is granulated as two
// mono clouds sharing one trigger and one set of per-grain random values. The
// grains stay sample-aligned, and `pan`/`spread` rotate each grain in the
// stereo field instead of collapsing the source to mono.
SynthDef(\granolaVoice, { |out = 0, bufL = 0, bufR = 0, gate = 1,
    pos = 0.0, posLag = 0.05, scan = 0.0,
    grainSize = 0.12, density = 20.0, jitter = 0.0,
    pitch = 0.0, pitchJitter = 0.0, pitchQuant = 0.0,
    pan = 0.0, spread = 0.6,
    lpf = 18000, hpf = 20, resonance = 0.0,
    envBuf = -1, reverse = 0.0, overlapLimit = 512,
    freeze = 0.0, drift = 0.0, gain = 1.0, filter = 0.0,
    index = 0, run = 1 |

    var trig, dur, head, posSig, rate, sig, jit, semis, bufDur, gateEnv, clock, grainPan;
    var filterAmount, filterMag, filterRq;

    bufDur = max(BufDur.kr(bufL), 0.001);

    // --- trigger clock -----------------------------------------------------
    // `drift` smears the grain clock toward Dust-like irregularity without
    // ever fully losing the pulse.
    clock = density * LFNoise1.kr(density.clip(0.1, 30) * 0.5).range(1.0 - drift, 1.0 + drift);
    // `run` is the transport (the Play button), and it stops the CLOCK rather than
    // muting the output: no new grains are scheduled, and the ones already in flight
    // finish their envelopes instead of being chopped. The amp fade below then catches
    // anything long enough to outlast a musical stop.
    trig = Impulse.ar(clock.max(0.01)) * run;

    // --- scrub head --------------------------------------------------------
    // Encoder position, de-zippered, plus an optional free-running scan so a
    // cloud can drift through the sample on its own. `scan` is in buffers/sec.
    head = Lag.kr(pos, posLag);
    head = head + (LFSaw.kr((scan / bufDur), 1).range(0, 1) * scan.sign);
    head = head.wrap(0, 1);

    // The scrub head is PUSHED to the language, the same way the strip pushes its
    // meter — so the controller can report the TRUE playhead (position plus any
    // free-running scan) to the 16 step buttons rather than the encoder's last raw
    // value. Pushing beats polling here: polling it with Bus.getn at 20Hz registers a
    // one-shot OSC responder per read, and those pile up the moment replies are slow.
    SendReply.kr(Impulse.kr(20), '/granolaHead', [index, head]);

    // Position jitter is specified in seconds, converted to buffer-relative
    // units so it means the same thing regardless of sample length.
    jit = TRand.ar(-1.0, 1.0, trig) * (jitter / bufDur);
    posSig = K2A.ar(head);
    posSig = Select.ar(freeze, [posSig, Latch.ar(posSig, trig)]);
    posSig = (posSig + jit).wrap(0, 1);

    // --- pitch -------------------------------------------------------------
    semis = pitch + TRand.ar(pitchJitter.neg, pitchJitter, trig);
    semis = Select.ar(pitchQuant, [semis, semis.round(1.0)]);
    // NO BufRateScale HERE. GrainBuf already compensates for the buffer's own sample rate
    // internally, unlike PlayBuf — measured, not assumed: rendering a 1000 Hz tone from a
    // 48 kHz buffer on a 44.1 kHz server gives 1000 Hz at rate 1, and 1088 Hz (+1.46
    // semitones) at rate BufRateScale. Multiplying by it applies the correction twice, so
    // pitch 0 was NOT the sample's own pitch.
    //
    // The macOS app carries the same multiply and is right to: it runs its server at the
    // same rate as its material, where BufRateScale is 1.0 and the error is invisible.
    // Here the server is 44.1 kHz and every harvested sample is 48 kHz, so every one of
    // them came in a semitone and a half sharp. The audition path keeps BufRateScale
    // because PlayBuf genuinely needs it — verified separately by playback duration.
    rate = semis.midiratio * (1.0 - (reverse * 2.0));

    // --- grains ------------------------------------------------------------
    dur = max(grainSize, 0.001)
        * TExpRand.ar(1.0 - (drift * 0.5), 1.0 + (drift * 0.5), trig);

    // Per-grain scatter only. Track pan is NOT folded in here: Rotate2 rotates
    // the stereo field rather than moving it left or right, so using it as a
    // pan control makes hard-left and hard-right sound nearly identical.
    grainPan = TRand.ar(spread.neg, spread, trig).clip(-1, 1);

    sig = [bufL, bufR].collect { |b|
        GrainBuf.ar(
            numChannels: 1,
            trigger: trig,
            dur: dur,
            sndbuf: b,
            rate: rate,
            pos: posSig,
            interp: 4,
            pan: 0,
            envbufnum: envBuf,
            maxGrains: overlapLimit
        );
    };
    // 0.5 maps the ±1 spread control onto ±90 degrees of stereo rotation, so
    // grains scatter around the field while each keeps its own stereo content.
    sig = Rotate2.ar(sig[0], sig[1], grainPan * 0.5);

    // --- tone shaping ------------------------------------------------------
    sig = RLPF.ar(sig, lpf.clip(40, 20000), (1.0 - resonance).clip(0.05, 1.0));
    sig = HPF.ar(sig, hpf.clip(20, 18000));
    sig = LeakDC.ar(sig);

    // --- one-knob DJ filter ------------------------------------------------
    // Centre is neutral. Turning down sweeps a low-pass from 20kHz to 120Hz;
    // turning up sweeps a high-pass from 20Hz to 7kHz. Resonance rises with
    // distance from centre, so the further the knob goes the more the filter
    // sings — one control covering cutoff, resonance and mode.
    filterAmount = filter.clip(-1, 1);
    filterMag = filterAmount.abs;
    // Resonance peaks around three quarters of the travel rather than at the
    // stop: at the extremes the peak sits on top of whatever little is left in
    // the band, which reads as a level jump rather than as character.
    filterRq = (1.0 - (sin(filterMag * pi * 0.8) * 0.66)).clip(0.3, 1.0);
    sig = Select.ar(filterAmount > 0, [
        RLPF.ar(sig, LinExp.kr(filterMag, 0, 1, 20000, 120), filterRq),
        RHPF.ar(sig, LinExp.kr(filterMag, 0, 1, 20, 7000), filterRq)
    ]);
    // A resonant low-pass over bass-heavy material gets loud fast; trim the
    // low side so a downward sweep thins out instead of swelling.
    sig = sig * Select.kr(filterAmount > 0, [(1.0 - (filterMag * 0.55)), DC.kr(1)]);
    sig = LeakDC.ar(sig);

    // Track pan: equal-power stereo balance, the way a mixer channel behaves.
    sig = Balance2.ar(sig[0], sig[1], pan.clip(-1, 1));

    // Keep perceived loudness roughly constant as grains pile up.
    sig = sig * ((density * grainSize).max(1.0).sqrt.reciprocal.max(0.15));

    gateEnv = EnvGen.kr(Env.asr(0.02, 1, 0.08), gate, doneAction: 2);
    // Transport fade. Long enough not to click, short enough that Stop means stop even
    // with two-second grains ringing.
    // Sanitised at the SOURCE too. A voice pointed at a freed or zero-length buffer can
    // produce non-finite samples, and its strip feeds the reverb and delay sends.
    Out.ar(out, safe.value(sig) * gain * gateEnv * Lag.kr(run, 0.08));
}).add;

// ---------------------------------------------------------------------------
// Track strip: level / mute / reverb send / metering, and it clears the track
// bus so the voice can be rebuilt underneath without a click.
// ---------------------------------------------------------------------------
SynthDef(\granolaStrip, { |in = 0, mixOut = 16, perfOut = 40,
    reverbOut = 0, delayOut = 0, index = 0,
    amp = 0.7, mute = 0.0, reverbSend = 0.0, delaySend = 0.0,
    routing = 0.0, lag = 0.03, routeLag = 0.08|

    var sig, level, route;

    sig = In.ar(in, 2);
    level = Lag.kr(amp * (1.0 - mute), lag);
    sig = sig * level;

    SendReply.kr(Impulse.kr(15), '/trackMeter', [index, Amplitude.kr(sig.sum * 0.5)]);

    ReplaceOut.ar(in, Silent.ar(2));

    // Routing is a crossfade, not a switch. Moving a track between the mix and
    // the performance chain by reassigning its output bus changes the signal
    // in a single sample, which is audible as a click; fading between two
    // permanent outputs cannot be.
    route = Lag.kr(routing.clip(0, 1), routeLag);
    Out.ar(mixOut, sig * (1.0 - route));
    Out.ar(perfOut, sig * route);

    // Post-fader sends: muting a track mutes what it feeds the effects too.
    Out.ar(reverbOut, sig * Lag.kr(reverbSend, lag));
    Out.ar(delayOut, sig * Lag.kr(delaySend, lag));
}).add;

// ---------------------------------------------------------------------------
// Performance FX bus head/terminator. Links inserted between the two process
// the bus in place, so chain order is node order.
// ---------------------------------------------------------------------------
SynthDef(\granolaPerfIn, { |in = 40, levelBus = 8|
    Out.kr(levelBus, Amplitude.kr(In.ar(in, 2).sum * 0.5, 0.5, 1.0));
}).add;

SynthDef(\granolaPerfOut, { |in = 40, out = 16, levelBus = 8,
    ceiling = 0.95, autoGain = 1|

    var sig, inLevel, outLevel, match;
    sig = In.ar(in, 2);
    ReplaceOut.ar(in, Silent.ar(2));
    sig = LeakDC.ar(sig);

    // Match the chain's output level to what went in — LOUDNESS, not events,
    // hence long time constants and a gentle bound.
    inLevel = In.kr(levelBus);
    outLevel = Amplitude.kr(sig.sum * 0.5, 0.5, 1.0);
    match = Lag.kr(
        Select.kr(inLevel > 0.0005, [
            DC.kr(1),
            (inLevel / outLevel.max(0.0001)).clip(0.5, 2)
        ]), 1.5);
    sig = sig * Select.kr(autoGain, [DC.kr(1), match]);

    // Zero-latency safety: Limiter.ar delays by twice its lookahead, which would
    // put this path behind the direct one and comb-filter the crossfade.
    sig = (sig * ceiling.reciprocal).softclip * ceiling;
    Out.ar(out, sig);
}).add;

// ---------------------------------------------------------------------------
// Master reverb — JPverb (sc3-plugins). Verified present in the Move's SC
// bundle (plugins/JPverb.so + JPverb_supernova.so).
// ---------------------------------------------------------------------------
SynthDef(\granolaReverb, { |in = 0, out = 0, amp = 1.0,
    predelay = 0.025, decay = 3.5, size = 1.6, damp = 0.35,
    earlyDiff = 0.78, modDepth = 0.12, modRate = 1.7,
    lowMult = 1.0, midMult = 1.0, highMult = 0.55,
    lowBand = 380, highBand = 4200,
    inputCut = 90, width = 1.0 |

    var input, pre, wet, centre, sides;

    input = safe.value(In.ar(in, 2));
    input = HPF.ar(input, inputCut.clip(20, 1000));
    pre = DelayN.ar(input, 0.5, predelay.clip(0, 0.5));

    wet = JPverb.ar(
        in: pre,
        t60: decay.clip(0.1, 60),
        damp: damp.clip(0, 1),
        size: size.clip(0.5, 5),
        earlyDiff: earlyDiff.clip(0, 1),
        modDepth: modDepth.clip(0, 1),
        modFreq: modRate.clip(0, 10),
        low: lowMult.clip(0, 1),
        mid: midMult.clip(0, 1),
        high: highMult.clip(0, 1),
        lowcut: lowBand.clip(100, 6000),
        highcut: highBand.clip(1000, 10000)
    );

    centre = (wet[0] + wet[1]) * 0.5;
    sides = (wet[0] - wet[1]) * 0.5 * width.clip(0, 2);
    wet = [centre + sides, centre - sides];

    ReplaceOut.ar(in, Silent.ar(2));
    Out.ar(out, safe.value(LeakDC.ar(wet)) * Lag.kr(amp, 0.05));
}).add;

// ---------------------------------------------------------------------------
// Master stereo delay.
// ---------------------------------------------------------------------------
SynthDef(\granolaDelay, { |in = 0, out = 0, amp = 1.0,
    timeL = 0.375, timeR = 0.5, feedback = 0.45, crossFeed = 0.35,
    damp = 6500, lowCut = 120, modDepth = 0.15, modRate = 0.23,
    diffusion = 0.0, width = 1.0, freeze = 0.0, lag = 0.15 |

    var input, fb, injectL, injectR, tL, tR, sig, mid, side, fbAmount;

    input = safe.value(In.ar(in, 2)) * (1.0 - freeze);
    fb = safe.value(LocalIn.ar(2));

    // crossFeed 0 = parallel stereo delays, 1 = ping-pong.
    injectL = input[0] + (fb[0] * (1 - crossFeed) + (fb[1] * crossFeed));
    injectR = input[1] + (fb[1] * (1 - crossFeed) + (fb[0] * crossFeed));

    tL = Lag.kr(timeL.clip(0.001, 10.0), lag)
        * (1 + (LFNoise2.kr(modRate) * modDepth * 0.02));
    tR = Lag.kr(timeR.clip(0.001, 10.0), lag)
        * (1 + (LFNoise2.kr(modRate * 1.17) * modDepth * 0.02));

    sig = [
        DelayC.ar(injectL, 10.0, tL.clip(0.001, 10.0)),
        DelayC.ar(injectR, 10.0, tR.clip(0.001, 10.0))
    ];

    sig = LPF.ar(sig, damp.clip(200, 18000));
    sig = HPF.ar(sig, lowCut.clip(20, 2000));

    sig = [0.0071, 0.0113].collect { |t, i|
        AllpassC.ar(sig[i], 0.05, [t, t * 1.09], t * (1 + (diffusion.clip(0, 1) * 30))).sum * 0.5
    };
    sig = LeakDC.ar(sig);

    fbAmount = Select.kr(freeze, [feedback.clip(0, 0.98), DC.kr(1.0)]);
    // Sanitised on the way INTO the loop: this is the line that decides whether a single
    // bad sample becomes a permanent condition.
    LocalOut.ar(safe.value(sig * fbAmount));

    mid = (sig[0] + sig[1]) * 0.5;
    side = (sig[0] - sig[1]) * 0.5 * width;
    sig = [mid + side, mid - side];

    ReplaceOut.ar(in, Silent.ar(2));
    Out.ar(out, safe.value(sig) * Lag.kr(amp, 0.05));
}).add;

// ---------------------------------------------------------------------------
// Audition: play a file straight to the mix, for the harvester's hold-to-hear.
//
// Two defs rather than one because PlayBuf's channel count is fixed at build time and a
// harvested file may be mono or stereo. The gate lets a release stop it before the end,
// so holding a pad plays for exactly as long as the pad is held.
// ---------------------------------------------------------------------------
SynthDef(\granolaAudition1, { |buf = 0, out = 16, amp = 0.8, gate = 1|
    var sig = PlayBuf.ar(1, buf, BufRateScale.kr(buf), doneAction: 2);
    var env = EnvGen.kr(Env.asr(0.005, 1, 0.06), gate, doneAction: 2);
    Out.ar(out, [sig, sig] * amp * env);
}).add;

SynthDef(\granolaAudition2, { |buf = 0, out = 16, amp = 0.8, gate = 1|
    var sig = PlayBuf.ar(2, buf, BufRateScale.kr(buf), doneAction: 2);
    var env = EnvGen.kr(Env.asr(0.005, 1, 0.06), gate, doneAction: 2);
    Out.ar(out, sig * amp * env);
}).add;

// ---------------------------------------------------------------------------
// Master bus: gain, soft limiter, metering.
// ---------------------------------------------------------------------------
SynthDef(\granolaMaster, { |in = 0, out = 0, recOut = 20, amp = 0.8, ceiling = 0.95|
    var sig = In.ar(in, 2);
    sig = sig * Lag.kr(amp, 0.05);
    sig = Limiter.ar(sig, ceiling, 0.01);

    SendReply.kr(Impulse.kr(15), '/masterMeter', [
        Amplitude.kr(sig[0]), Amplitude.kr(sig[1])
    ]);

    ReplaceOut.ar(in, Silent.ar(2));
    Out.ar(out, sig);
    ReplaceOut.ar(recOut, sig);
}).add;

"[granola] synthdefs added".postln;
)
