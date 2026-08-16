"""Project slots.

A project is one complete Granola machine at an instant: eight tracks, each with its
sample, its whole parameter set including THE PLAYHEAD, and its four pad toggles; the
master level; the transport; and the entire FX rack — every slot's active and locked
state, its dry/wet, and the exact chain with the parameters each link was rolled with.

The chain is stored rather than re-rolled. A slot's chain is random when it is created,
so re-rolling on load would hand back a different sound under the same project name,
which is the one thing a saved project must never do.

It is `Model.snapshot()` plus a `machine` block for the parts that do not live in the
track model, so there is still no second representation of a track to keep in step.

Slots are files rather than one index file: a half-written index would take every
project with it, and 32 small files can be listed as cheaply as one can be parsed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

N_SLOTS = 32


def _slot_path(root: Path, slot: int) -> Path:
    return root / ("%02d.json" % (slot + 1))


class ProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Which slot is loaded. -1 = the live state came from nowhere in particular
        # (a fresh machine, or edits since the last load).
        self.current: int = -1
        self._meta: list[dict | None] = [None] * N_SLOTS
        self.rescan()

    # -- listing ------------------------------------------------------------ #
    def rescan(self) -> None:
        for i in range(N_SLOTS):
            self._meta[i] = self._read_meta(i)

    def _read_meta(self, slot: int) -> dict | None:
        p = _slot_path(self.root, slot)
        try:
            doc = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        tracks = doc.get("tracks") or []
        return {
            "name": doc.get("name") or ("PROJECT %d" % (slot + 1)),
            "saved": doc.get("saved", 0),
            # enough for the web UI to show a slot without reading every file again
            "samples": [
                (Path(t.get("samplePath")).name if t.get("samplePath") else None)
                for t in tracks
            ],
        }

    @property
    def filled(self) -> list[bool]:
        return [m is not None for m in self._meta]

    def names(self) -> list[str]:
        return [(m["name"] if m else "") for m in self._meta]

    def meta(self, slot: int) -> dict | None:
        return self._meta[slot] if 0 <= slot < N_SLOTS else None

    # -- save / load -------------------------------------------------------- #
    def save(self, slot: int, snapshot: dict, name: str | None = None) -> bool:
        if not 0 <= slot < N_SLOTS:
            return False
        # Keep the existing name when saving over a slot without giving a new one, so
        # re-saving a named project does not silently rename it back to a number.
        if not name:
            existing = self._meta[slot]
            name = existing["name"] if existing else ("PROJECT %d" % (slot + 1))
        doc = dict(snapshot)
        doc["name"] = name
        doc["saved"] = int(time.time())
        p = _slot_path(self.root, slot)
        tmp = p.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(doc))
            tmp.replace(p)
        except OSError:
            return False
        self._meta[slot] = self._read_meta(slot)
        self.current = slot
        return True

    def load(self, slot: int) -> dict | None:
        if not 0 <= slot < N_SLOTS:
            return None
        try:
            doc = json.loads(_slot_path(self.root, slot).read_text())
        except (OSError, ValueError):
            return None
        self.current = slot
        return doc

    def rename(self, slot: int, name: str) -> bool:
        doc = self.load(slot)
        if doc is None:
            return False
        return self.save(slot, doc, name)

    def delete(self, slot: int) -> bool:
        if not 0 <= slot < N_SLOTS:
            return False
        try:
            _slot_path(self.root, slot).unlink()
        except OSError:
            return False
        self._meta[slot] = None
        if self.current == slot:
            self.current = -1
        return True
