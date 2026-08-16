"""The Airwindows repertoire, and the algorithm that assembles chains from it.

A port of the macOS app's AirwindowsCatalog + GranolaModel.assembleChain. The numbers
here — parameter range, dry/wet handling, one-link-in-three modulation, chain length,
mix spread — are the app's, because they are what makes a rolled chain usually musical
rather than usually noise.

The repertoire itself comes from the build-time manifest that
`Scripts/airwindows/generate.py` emits alongside the compiled UGens, so the catalogue can
never claim an effect the plugin does not actually provide.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

# Defaults carried over from GranolaModel.
FX_WET_MIX = 0.4        # centre of a link's blend
FX_FADE_TIME = 5.0      # seconds — a performance control should swell, not snap
FX_AUTO_GAIN = True
MAX_CHAIN = 4           # links per slot
N_SLOTS = 4

# --- the CPU budget -------------------------------------------------------------
# MEASURED, not guessed. Profiling all 154 effects on the Move (one at a time, over a
# fixed source) gives a median cost of 4.6% of one core each, a mean of 5.0% and a spread
# from 0.7% (Slew) to 15.2% (Ensemble). The amp simulations are the expensive family:
# MidAmp, BigAmp, LeadAmp and FireAmp together took the box from 47% to 97%.
#
# Four slots of up to four links each is sixteen links. At the median that is ~74% ON TOP
# of a ~47% baseline, and with the expensive families it is far worse — which is exactly
# where the xruns were coming from. So chains are rolled against a budget rather than a
# blind length: the constraint that actually matters is total weighted cost, not how many
# slots exist.
FX_CPU_BUDGET = 32.0    # total % of one core the whole rack may ask for
FX_SLOT_BUDGET = 20.0   # ...and the most any single slot may take of it
FX_DEFAULT_COST = 5.0   # for an effect missing from the table


@dataclass(frozen=True)
class Effect:
    name: str
    param_count: int
    param_names: tuple[str, ...]
    defaults: tuple[float, ...]

    @property
    def synthdef(self) -> str:
        return "awfx_%s" % self.name

    @property
    def dry_wet_index(self) -> int | None:
        """Airwindows exposes every parameter as 0..1, and the last is very often
        Dry/Wet — worth knowing when randomising, so a link does not assemble itself
        with the wet signal turned down to nothing."""
        for i, n in enumerate(self.param_names):
            low = n.lower()
            if "dry" in low or low == "wet":
                return i
        return None


@dataclass
class Link:
    """One link in a chain: an effect plus the parameters it was assembled with."""
    effect: Effect
    params: list[float]
    mix: float
    lfo_target: int          # index of the parameter an LFO moves, or -1
    lfo_rate: float
    lfo_depth: float
    lfo_shape: int
    node: int = 0            # server node id, assigned when instantiated

    @property
    def modulated(self) -> bool:
        return self.lfo_target >= 0 and self.lfo_depth > 0

    @property
    def summary(self) -> str:
        return self.effect.name + ("~" if self.modulated else "")


@dataclass
class Slot:
    """One of the four FX slots. Toggling it on assembles a brand-new random chain;
    toggling it off frees that slot's links and lets the rest close up.

    Unless it is LOCKED — a Move addition the desktop app has no equivalent of. A locked
    slot keeps its chain, with the exact parameters it was rolled with, so switching it
    off and on again brings back the same sound instead of a new one. Locking is what
    turns a lucky roll into something you can perform with.
    """
    index: int
    active: bool = False
    locked: bool = False
    # The slot's dry/wet, moved by the jog while the slot pad is held. It SCALES the
    # links rather than replacing their blends: each was rolled with its own mix around
    # FX_WET_MIX, and that relative balance is part of the chain's character, so the
    # control moves them together instead of flattening them to one number.
    wet: float = FX_WET_MIX
    chain: list[Link] = field(default_factory=list)
    # Order of ACTIVATION, not slot number: the composite chain is built in the order
    # pads were pressed, and closes up when one is removed.
    order: int = 0

    def mix_of(self, link: Link) -> float:
        """A link's effective blend at this slot's wet level. 0 leaves the chain fully
        dry — which is the only honest meaning of a dry/wet control at zero."""
        scale = self.wet / FX_WET_MIX if FX_WET_MIX > 0 else 0.0
        return min(1.0, max(0.0, link.mix * scale))

    @property
    def label(self) -> str:
        return " > ".join(l.effect.name for l in self.chain) if self.chain else "—"


class Catalog:
    def __init__(self, manifest: Path, costs: Path | None = None) -> None:
        self.effects: list[Effect] = []
        self.costs: dict[str, float] = {}
        if costs is not None:
            try:
                self.costs = {k: float(v) for k, v in
                              json.loads(Path(costs).read_text()).get("costs", {}).items()}
            except (OSError, ValueError, TypeError):
                self.costs = {}
        try:
            doc = json.loads(Path(manifest).read_text())
        except (OSError, ValueError):
            return
        for e in doc:
            try:
                self.effects.append(Effect(
                    name=str(e["name"]),
                    param_count=int(e["paramCount"]),
                    param_names=tuple(e.get("paramNames") or ()),
                    defaults=tuple(float(x) for x in (e.get("defaults") or ())),
                ))
            except (KeyError, TypeError, ValueError):
                continue

    def __len__(self) -> int:
        return len(self.effects)

    @property
    def is_empty(self) -> bool:
        return not self.effects

    def cost(self, effect: Effect | str) -> float:
        name = effect if isinstance(effect, str) else effect.name
        return self.costs.get(name, FX_DEFAULT_COST)

    def chain_cost(self, chain: list[Link]) -> float:
        return sum(self.cost(l.effect) for l in chain)

    def link_for(self, effect: Effect, mix_centre: float = FX_WET_MIX) -> Link:
        """A randomly-parameterised instance of one NAMED effect."""
        params = [random.uniform(0.15, 0.9) for _ in range(effect.param_count)]
        wet = effect.dry_wet_index
        if wet is not None and wet < len(params):
            params[wet] = 1.0
        modulated = effect.param_count > 0 and random.randrange(3) == 0
        return Link(
            effect=effect,
            params=params,
            mix=min(1.0, max(0.05, mix_centre + random.uniform(-0.08, 0.08))),
            lfo_target=random.randrange(effect.param_count) if modulated else -1,
            lfo_rate=random.uniform(0.05, 6.0),
            lfo_depth=random.uniform(0.1, 0.45) if modulated else 0.0,
            lfo_shape=random.randint(0, 2),
        )

    def random_link(self, mix_centre: float = FX_WET_MIX,
                    budget: float | None = None) -> Link | None:
        if not self.effects:
            return None
        pool = self.effects
        if budget is not None:
            # Only consider what fits. Filtering the POOL rather than rejecting after the
            # roll is what keeps chains varied when the budget is tight — otherwise a
            # nearly-full rack would keep drawing expensive effects and giving up, and
            # every late slot would end up with one link.
            affordable = [e for e in pool if self.cost(e) <= budget]
            if not affordable:
                return None
            pool = affordable
        return self.link_for(random.choice(pool), mix_centre)

    def assemble_chain(self, mix_centre: float = FX_WET_MIX,
                       budget: float | None = None) -> list[Link]:
        """One to four links, no effect twice — two of the same back to back is rarely
        interesting — and within a CPU budget.

        The budget is what keeps a rack playable: a chain of four amp simulations is
        musically no more interesting than a chain of four cheap effects, but it is the
        difference between running and dropping out. Cheap effects therefore buy length
        and expensive ones cost it, which is also a better generator — the rolls come out
        varied in character rather than uniformly dense.
        """
        if self.is_empty:
            return []
        room = FX_SLOT_BUDGET if budget is None else min(budget, FX_SLOT_BUDGET)
        length = random.randint(1, MAX_CHAIN)
        chain: list[Link] = []
        used: set[str] = set()
        # Bounded rather than `while True`: with a short catalogue the no-repeat rule
        # could otherwise never be satisfied and spin forever.
        for _ in range(length * 8):
            if len(chain) >= length:
                break
            link = self.random_link(mix_centre, budget=room)
            if link is None:
                break                       # nothing affordable left
            if link.effect.name in used:
                continue
            used.add(link.effect.name)
            chain.append(link)
            room -= self.cost(link.effect)
        if not chain:
            # The rack is already full. Rather than hand back an empty slot — which reads
            # as a broken pad — give it the single cheapest effect there is, so the
            # gesture still does something audible.
            cheapest = min(self.effects, key=self.cost)
            chain = [self.link_for(cheapest, mix_centre)]
        return chain
