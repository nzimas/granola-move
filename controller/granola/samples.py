"""Granola's sample library.

ONE FOLDER, AND IT IS GRANOLA'S OWN. The library is `/data/UserData/granola/samples`
and nothing else: not the Move's user library, not the sibling takeovers' audio. This
stack's material is what the owner puts in it — uploaded through the web UI or copied
into that folder — so the browser never offers a file that arrived from somewhere else.

Only formats libsndfile (and therefore the server) opens without help are offered; the
desktop app's AVFoundation transcode path has no equivalent here, and offering a file
the server cannot read would just produce a silent track.
"""
from __future__ import annotations

import os
from pathlib import Path

# Same list as SampleLoader.nativeExtensions.
NATIVE_EXT = {".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".caf", ".w64", ".au", ".snd"}

# Granola's library, and only Granola's. Subfolders are scanned, so the folder can be
# organised however its owner likes.
DEFAULT_ROOTS = ("/data/UserData/granola/samples",)

MAX_SCAN = 2048         # one owned folder — no reason to stop early
MIN_BYTES = 4096        # skip stubs and truncated writes


def discover(roots=DEFAULT_ROOTS, limit: int = MAX_SCAN) -> list[str]:
    """Every readable audio file under `roots`, de-duplicated, in a stable order.

    Stable matters: track 3 should hold the same sample on the next launch even if
    nothing was persisted, so the instrument comes up the way it was left.
    """
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            walker = sorted(os.walk(base), key=lambda e: e[0])
        except OSError:
            continue
        for dirpath, dirnames, filenames in walker:
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith("._") or name.startswith("."):
                    continue          # AppleDouble junk and dotfiles
                if Path(name).suffix.lower() not in NATIVE_EXT:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) < MIN_BYTES:
                        continue
                except OSError:
                    continue
                real = os.path.realpath(full)
                if real in seen:
                    continue
                seen.add(real)
                found.append(full)
                if len(found) >= limit:
                    return found
    return found


# NOTE: there is deliberately no auto-assign. Tracks start empty and stay empty until a
# sample is put on them from the web UI or the jog wheel. Filling eight columns with
# whatever eight files happened to sort first is a guess about the music, and this stack's
# content is chosen, not discovered.
