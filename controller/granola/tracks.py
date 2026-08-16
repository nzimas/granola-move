"""The Granola control model: eight tracks, each a sample plus a grain cloud.

Ported from Sources/Granola/Model/TrackModel.swift. This module is the authoritative
owner of synthesis state — the hardware UI is a *representation* of it, never its
owner. That is what lets LEDs be redrawn, a view be switched or the module be
relaunched without disturbing a single parameter.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from .params import CENTRE_DETENT, MACRO_SLOTS, SPECS, encoder_delta

N_TRACKS = 8


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else (hi if x > hi else x)


class Track:
    def __init__(self, index: int) -> None:
        self.index = index
        # --- sample ---
        self.sample_path: str | None = None
        self.sample_name: str = "—"
        self.duration: float = 0.0
        self.channels: int = 0
        self.loaded: bool = False
        # --- parameters ---
        self.values: dict[str, float] = {k: s.default for k, s in SPECS.items()}
        # --- strip state ---
        self.mute: bool = False
        # Which of the four parameter pads are lit. More than one lit turns the encoder
        # into a macro that moves all of them together. Empty = the encoder is the
        # sample scan/playhead, which is the default state.
        self.macros: set[str] = set()
        # The engine's reported scrub head (position + any free-running scan), 0..1.
        self.head: float = 0.0
        self.meter: float = 0.0

    # -- parameter access ------------------------------------------------- #
    def value(self, param: str) -> float:
        return self.values.get(param, SPECS[param].default)

    def normalized(self, param: str) -> float:
        return SPECS[param].normalized(self.value(param))

    def set_value(self, param: str, v: float) -> bool:
        """Returns True when the value actually moved, so callers can skip redundant
        OSC traffic — this is on the path of every encoder detent."""
        spec = SPECS[param]
        clamped = _clamp(v, spec.lo, spec.hi)
        if abs(self.values.get(param, float("nan")) - clamped) <= 1e-9:
            return False
        self.values[param] = clamped
        return True

    def set_normalized(self, param: str, t: float) -> bool:
        return self.set_value(param, SPECS[param].value(t))

    def nudge_normalized(self, param: str, delta: float) -> bool:
        return self.set_normalized(param, self.normalized(param) + delta)

    # -- macro handling ---------------------------------------------------- #
    def toggle_macro(self, slot: str) -> bool:
        if slot in self.macros:
            self.macros.discard(slot)
        else:
            self.macros.add(slot)
        return slot in self.macros

    @property
    def macro_params(self) -> list[str]:
        """The parameters an encoder turn should move, in a stable order."""
        return [s for s in MACRO_SLOTS if s in self.macros]

    @property
    def macro_label(self) -> str:
        params = self.macro_params
        if not params:
            return SPECS["position"].label
        if len(params) == 1:
            return SPECS[params[0]].label
        return "MACRO x%d" % len(params)

    # -- bulk operations --------------------------------------------------- #
    def reset_parameters(self) -> None:
        for k, spec in SPECS.items():
            if k != "position":
                self.values[k] = spec.default

    def randomise(self, amount: float = 1.0) -> None:
        amount = _clamp(amount, 0.0, 1.0)
        for k, spec in SPECS.items():
            if not spec.randomisable:
                continue
            current = self.normalized(k)
            target = random.random()
            self.values[k] = spec.value(current + (target - current) * amount)

    # -- persistence ------------------------------------------------------- #
    def snapshot(self) -> dict:
        values = dict(self.values)
        # THE PLAYHEAD IS PART OF THE STATE. `head` is what the engine actually reports —
        # the model's `position` plus any free-running scan — so it is the true head at
        # this instant, which is what a project has to preserve. The model's own
        # `position` only tracks the encoder and would restore the wrong place on any
        # track that is scanning.
        if self.loaded:
            values["position"] = float(self.head)
        return {
            "samplePath": self.sample_path,
            "sampleName": self.sample_name,
            "values": values,
            "mute": self.mute,
            "macros": sorted(self.macros),
        }

    def apply(self, snap: dict) -> None:
        """Restores EVERYTHING, the scrub head included.

        This used to zero the head on the way in, so that a model restored at launch
        agreed with the position bar. Granola no longer restores a model at launch — it
        starts empty — and for a PROJECT the head is exactly the kind of detail that has
        to come back: where each track was playing from is part of the sound.
        """
        for k, v in (snap.get("values") or {}).items():
            spec = SPECS.get(k)
            if spec is None:
                continue
            try:
                self.values[k] = _clamp(float(v), spec.lo, spec.hi)
            except (TypeError, ValueError):
                continue
        self.head = self.values.get("position", 0.0)
        self.mute = bool(snap.get("mute", False))
        self.macros = {m for m in (snap.get("macros") or []) if m in MACRO_SLOTS}
        path = snap.get("samplePath")
        self.sample_path = path if isinstance(path, str) else None
        self.sample_name = snap.get("sampleName") or "—"


class Model:
    """The eight tracks plus the little that is global."""

    def __init__(self) -> None:
        self.tracks = [Track(i) for i in range(N_TRACKS)]
        self.master: float = 0.8
        # "Currently focused / last-touched track": the track whose playhead the 16 step
        # buttons show. Set by touching or turning an encoder.
        self.focus: int = 0

    # -- encoder ----------------------------------------------------------- #
    def nudge_encoder(self, index: int, ticks: int) -> list[tuple[str, float]]:
        """Every encoder turn arrives here; the track's own pad state decides what it
        moves. Returns the (param, value) pairs that actually changed, so the caller
        can send one coalesced OSC message.

        With no parameter pad lit the encoder is the sample scan/playhead — the
        default state, and per-track: another column's pads cannot change this
        column's role.
        """
        if not 0 <= index < N_TRACKS:
            return []
        track = self.tracks[index]
        params = track.macro_params or ["position"]
        delta = encoder_delta(ticks)

        # A control with a musical centre catches it — but only on its own. The desktop
        # app applies the detent in nudgeSingle and not in nudgeMacro, and that is right:
        # a macro sweep that sticks whenever pan crosses centre would feel broken.
        if len(params) == 1 and params[0] in CENTRE_DETENT:
            p = params[0]
            spec = SPECS[p]
            current = track.normalized(p)
            target = current + delta
            centre = spec.normalized(0.0)
            # Only when the move passes THROUGH centre; see CENTRE_DETENT.
            if (current - centre) * (target - centre) < 0:
                target = centre
            return [(p, track.value(p))] if track.set_normalized(p, target) else []

        # The macro moves each selected parameter by the same NORMALIZED amount, then
        # each maps that back through its own curve into its own domain — so Size
        # (exponential, 2ms..2s) and Pitch (linear, ±24st) move musically together
        # rather than being handed the same raw number.
        return [(p, track.value(p)) for p in params if track.nudge_normalized(p, delta)]

    def toggle_macro(self, index: int, slot: int) -> bool | None:
        if not 0 <= index < N_TRACKS or not 0 <= slot < len(MACRO_SLOTS):
            return None
        return self.tracks[index].toggle_macro(MACRO_SLOTS[slot])

    # -- persistence ------------------------------------------------------- #
    def snapshot(self) -> dict:
        return {"version": 1, "master": self.master,
                "tracks": [t.snapshot() for t in self.tracks]}

    def apply(self, doc: dict) -> None:
        try:
            self.master = float(doc.get("master", 0.8))
        except (TypeError, ValueError):
            self.master = 0.8
        for i, snap in enumerate(doc.get("tracks") or []):
            if i < N_TRACKS and isinstance(snap, dict):
                self.tracks[i].apply(snap)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.snapshot()))
            tmp.replace(path)
        except OSError:
            pass

    def load(self, path: Path) -> bool:
        try:
            self.apply(json.loads(path.read_text()))
            return True
        except (OSError, ValueError):
            return False
