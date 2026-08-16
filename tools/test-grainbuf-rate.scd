// Does GrainBuf need BufRateScale? Measured, not assumed.
//
// Renders a 1000 Hz tone from a 48 kHz buffer on a 44.1 kHz server, once with rate 1 and
// once with rate BufRateScale, then you measure the two output files. Result on SC 3.13:
//
//   rate = 1             -> 1000 Hz   (correct: GrainBuf compensates internally)
//   rate = BufRateScale  -> 1088 Hz   (+1.46 semitones — the correction applied twice)
//
// PlayBuf is the opposite: it genuinely needs BufRateScale. That asymmetry is why the
// audition path keeps it and granolaVoice does not.
//
// Make the input first:
//   python3 -c "import wave,struct,math;sr=48000;w=wave.open('/tmp/tone48k.wav','w');\
//   w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);\
//   w.writeframes(b''.join(struct.pack('<h',int(20000*math.sin(2*math.pi*1000*i/sr))) \
//   for i in range(sr*2)));w.close()"
//
// Run:  sclang tools/test-grainbuf-rate.scd
// Then measure /tmp/out_noscale.wav and /tmp/out_scaled.wav.

(
Routine({
    var sr = 44100, dur = 2.0;
    var defA = SynthDef(\gbNoScale, { |buf=0|
        Out.ar(0, GrainBuf.ar(1, Impulse.ar(2), 0.5, buf, 1, LFSaw.ar(0.25).range(0,0.4), 2) ! 1);
    }).asBytes;
    var defB = SynthDef(\gbScaled, { |buf=0|
        Out.ar(0, GrainBuf.ar(1, Impulse.ar(2), 0.5, buf, BufRateScale.kr(buf),
            LFSaw.ar(0.25).range(0,0.4), 2) ! 1);
    }).asBytes;
    [["noscale", defA, \gbNoScale], ["scaled", defB, \gbScaled]].do { |p|
        Score([
            [0.0, [\d_recv, p[1]]],
            [0.0, [\b_allocRead, 0, "/tmp/tone48k.wav", 0, 0]],
            [0.01, [\s_new, p[2], 1000, 0, 0, \buf, 0]],
            [dur, [\c_set, 0, 0]]
        ]).recordNRT("/tmp/nrt_" ++ p[0] ++ ".osc", "/tmp/out_" ++ p[0] ++ ".wav",
            sampleRate: sr, headerFormat: "WAV", sampleFormat: "int16",
            options: ServerOptions.new.numOutputBusChannels_(1));
        2.wait;
    };
    "NRT DONE".postln;
    0.exit;
}).play;
)
