"""Every automatable per-track control, in one table.

A direct port of the macOS app's Sources/Granola/Model/Parameters.swift. Ranges,
defaults, curves and OSC key names are IDENTICAL — this is what makes a Granola patch
mean the same thing on the Move as it does on the desktop, and what lets the encoder
macro stay generic: nothing that drives a parameter needs to know what it *means*,
only how to map 0..1 onto it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

LINEAR = "linear"
EXPONENTIAL = "exponential"
TOGGLE = "toggle"

# `granolaStrip` owns level, mute and the two sends; everything else is the voice.
STRIP_PARAMS = frozenset({"level", "reverbSend", "delaySend"})


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


@dataclass(frozen=True)
class ParamSpec:
    ident: str
    label: str
    short: str
    lo: float
    hi: float
    default: float
    curve: str = LINEAR
    unit: str = ""
    randomisable: bool = True

    @property
    def osc_key(self) -> str:
        """The control name in the granolaVoice / granolaStrip SynthDef."""
        if self.ident == "position":
            return "pos"
        if self.ident == "level":
            return "amp"
        return self.ident

    @property
    def is_strip(self) -> bool:
        return self.ident in STRIP_PARAMS

    def value(self, t: float) -> float:
        """Map a normalized 0..1 encoder position onto the parameter's domain."""
        t = _clamp(t, 0.0, 1.0)
        if self.curve == LINEAR:
            return self.lo + (self.hi - self.lo) * t
        if self.curve == EXPONENTIAL:
            lo = max(self.lo, 1e-6)
            return lo * ((self.hi / lo) ** t)
        return 1.0 if t >= 0.5 else 0.0

    def normalized(self, value: float) -> float:
        v = _clamp(value, self.lo, self.hi)
        if self.curve == LINEAR:
            return (v - self.lo) / (self.hi - self.lo) if self.hi > self.lo else 0.0
        if self.curve == EXPONENTIAL:
            lo = max(self.lo, 1e-6)
            return math.log(max(v, lo) / lo) / math.log(self.hi / lo)
        return 1.0 if v >= 0.5 else 0.0

    def format(self, value: float) -> str:
        if self.curve == TOGGLE:
            return "ON" if value >= 0.5 else "OFF"
        mag = abs(value)
        digits = 0 if mag >= 100 else (1 if mag >= 10 else 2)
        text = f"%.{digits}f" % value
        return f"{text}{self.unit}" if self.unit else text


SPECS: dict[str, ParamSpec] = {
    s.ident: s
    for s in (
        ParamSpec("position", "Position", "POS", 0, 1, 0, randomisable=False),
        ParamSpec("grainSize", "Grain Size", "SIZE", 0.002, 2.0, 0.12, EXPONENTIAL, "s"),
        ParamSpec("density", "Density", "DENS", 0.2, 200, 20, EXPONENTIAL, "/s"),
        ParamSpec("jitter", "Jitter", "JIT", 0, 2.0, 0.01, EXPONENTIAL, "s"),
        ParamSpec("pitch", "Pitch", "PITCH", -24, 24, 0, LINEAR, "st"),
        ParamSpec("pitchJitter", "Pitch Spray", "P.SPR", 0, 24, 0, LINEAR, "st"),
        ParamSpec("pitchQuant", "Quantise Pitch", "QUANT", 0, 1, 0, TOGGLE),
        ParamSpec("scan", "Scan", "SCAN", -2, 2, 0, LINEAR, "x"),
        ParamSpec("drift", "Drift", "DRIFT", 0, 1, 0.1),
        ParamSpec("spread", "Stereo Spread", "SPRD", 0, 1, 0.6),
        ParamSpec("pan", "Pan", "PAN", -1, 1, 0, randomisable=False),
        ParamSpec("lpf", "Low Pass", "LPF", 100, 20000, 18000, EXPONENTIAL, "Hz"),
        ParamSpec("hpf", "High Pass", "HPF", 20, 8000, 20, EXPONENTIAL, "Hz"),
        ParamSpec("resonance", "Resonance", "RES", 0, 0.95, 0),
        ParamSpec("filter", "DJ Filter", "FILT", -1, 1, 0, randomisable=False),
        ParamSpec("reverse", "Reverse", "REV", 0, 1, 0, TOGGLE),
        ParamSpec("freeze", "Freeze", "FRZ", 0, 1, 0, TOGGLE),
        ParamSpec("posLag", "Glide", "GLIDE", 0.001, 2.0, 0.05, EXPONENTIAL, "s",
                  randomisable=False),
        ParamSpec("level", "Level", "LEVEL", 0, 1.4, 0.7, randomisable=False),
        ParamSpec("reverbSend", "Reverb Send", "RVB", 0, 1, 0, randomisable=False),
        ParamSpec("delaySend", "Delay Send", "DLY", 0, 1, 0, randomisable=False),
        # The grain window as a CONTINUUM rather than the app's four discrete cases: 0 is
        # Gaussian and 1 is Reverse, travelling through Percussive and Plateau on the way.
        # The engine renders the interpolated windows into a buffer table at boot and this
        # value picks one; see granola-engine.scd. Default 0 = Gaussian, the app's default.
        ParamSpec("envMorph", "Grain Shape", "SHAPE", 0, 1, 0),
    )
}

# The morph path, in order. Only used for the readout — the engine owns the actual
# crossfade — but it has to agree with ~grShapeSeq in granola-engine.scd.
ENV_SHAPES: tuple[str, ...] = ("GAUSS", "PERC", "PLATEAU", "REVERSE")


def env_morph_label(v: float) -> str:
    """Name the window rather than showing a bare 0..1.

    "0.42" says nothing about what you are about to hear; "PERC>PLAT 26%" says where you
    are on the path and which way you are heading.
    """
    seg = _clamp(v, 0.0, 1.0) * (len(ENV_SHAPES) - 1)
    i = min(int(seg), len(ENV_SHAPES) - 2)
    f = seg - i
    if f < 0.02:
        return ENV_SHAPES[i]
    if f > 0.98:
        return ENV_SHAPES[i + 1]
    return "%s>%s %d%%" % (ENV_SHAPES[i][:4], ENV_SHAPES[i + 1][:4], round(f * 100))


def pan_label(v: float) -> str:
    """L/C/R rather than a signed decimal. On a 128x64 screen "L45" is read at a glance
    and "-0.45" is not; the desktop app had a whole channel strip to make it obvious."""
    if abs(v) < 0.01:
        return "C"
    return ("R%d" if v > 0 else "L%d") % round(abs(v) * 100)


def format_value(ident: str, value: float) -> str:
    """The one place a parameter turns into text for a screen."""
    if ident == "envMorph":
        return env_morph_label(value)
    if ident == "pan":
        return pan_label(value)
    return SPECS[ident].format(value)


# Parameters whose centre is a musical home worth catching. Ported from the desktop app's
# nudgeSingle(centreDetent:) — and, as there, the catch happens only when the move passes
# THROUGH centre. A detent defined as "within X of centre" is wider than a single encoder
# tick, so it swallows every tick and the control appears dead.
CENTRE_DETENT: frozenset[str] = frozenset({"pan", "filter"})

# The parameters a column's encoder can be pointed at, in pad order.
#
# 0-3 are the macOS app's four macro slots — the SMC-Mixer's per-strip buttons, and the
# four pads under each encoder in View 1. Slot 4 onward are revealed in View 2, one per
# pad row: the set GROWS rather than being replaced, so a track lit for Size in View 1 and
# Drift in View 2 has an encoder that macros over both. That is the whole point of the
# multi-select model — the views decide what is reachable, not what is on.
#
# Slot 5 is `spread`, the classical granular stereo spread, which was ALREADY in the
# ported engine: granolaVoice scatters every grain's pan between -spread and +spread and
# Rotate2 maps that onto +/-90 degrees, so 0 stacks the cloud at the centre and 1 throws
# it across the whole field. Its 0.6 default is the app's own — mid-to-wide, which is
# where a granulator wants to sit before anyone touches it.
# Slots 7-10 are View 3: the mixer and the two master-effect sends. `level` and the sends
# belong to granolaStrip and `pan` to granolaVoice, but nothing here needs to know that —
# ParamSpec.is_strip routes them, and the engine splits one message into the right nodes.
MACRO_SLOTS: tuple[str, ...] = ("grainSize", "density", "jitter", "pitch",
                                "drift", "spread", "envMorph",
                                "level", "pan", "delaySend", "reverbSend")

# Which slots each view's pad rows expose, top row first. -1 = a row with nothing on it
# yet, left unlit rather than given something invented.
VIEW_ROWS: dict[int, tuple[int, ...]] = {
    1: (0, 1, 2, 3),          # size, density, jitter, pitch
    2: (4, 5, 6, -1),         # drift, spread, grain shape
    3: (7, 8, 9, 10),         # volume, pan, delay send, reverb send
}


def encoder_delta(ticks: int) -> float:
    """Larger sweeps for faster spins, without losing fine control.

    Ported verbatim from GranolaModel.encoderDelta. The shadow framework already
    batches encoder ticks (decodeDelta returns an accumulated count), so a fast spin
    arrives here as a large `ticks` and gets the same acceleration the desktop app
    applied to a fast fader move.
    """
    magnitude = abs(ticks)
    sign = (ticks > 0) - (ticks < 0)
    return sign * 0.004 * magnitude * (1 + magnitude * 0.35)
