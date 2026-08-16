#!/usr/bin/env python3
"""Regression test for the harvester's metadata filter.

The filter is now the ONLY thing keeping a batch musical — the audio analysis that used to
back it up was removed because this device cannot afford it. That makes these patterns
load-bearing, and load-bearing text rules drift: every term added to catch one bad title
risks blocking a real piece of music. Both directions are checked here.

Run: python3 tools/test-filter.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "controller/granola/harvester.py")).read()
ns = {}
exec(compile(src.split("class Harvest")[0], "harvester", "exec"), ns)
passes = ns["passes_metadata_filter"]

MUST_REJECT = [
    ("Tom Dwan Conversation | Poker Life Podcast", "PokerGO"),
    ("Change Your Brain: Neuroscientist Dr. Andrew Huberman", "Impact Theory"),
    ("Raspberry Pi - Remote VLF CW QRP", "Ham Shack"),
    ("Ep. 312 — the future of AI", "Some Channel"),
    ("How I Built This", "NPR"),
    ("10 hours of rain sounds", "Relax"),
    ("The Joe Rogan Experience #1999", "JRE"),
    ("Guided Meditation for Sleep", "Calm"),
    ("Beethoven Symphony No. 5 - Full Album", "Classical"),
    ("Lecture 3: Fourier Analysis", "MIT OpenCourseWare"),
    ("S02E14 Recap", "Recap Channel"),
    ("Type Beat 2024 [FREE]", "Beats"),
    ("Interview with Prof. Smith", "Talks"),
    ("Bible Study: Romans 8", "Grace Church"),
    ("Top 10 Ambient Tracks", "Listicle"),
    ("Mindset SECRETS From The World's Best Ultra Runner", "Performance"),
]

MUST_KEEP = [
    ("Sonatas & Interludes for Prepared Piano", "Naxos"),
    ("Spectral Decomposition", "Editions Mego"),
    ("Bowed Metal and Resin", "Room40"),
    ("Study for Cymbals", "Sub Rosa"),
    ("Granular Rain", "Hesus"),
    ("Metamorphosis and Resonances for Bass Clarinet", "Kairos"),
    ("Multiphonics Etude", "Wergo"),
    ("Hovering Resonance", "Touch"),
    ("Orchestral Drone F", "Erased Tapes"),
    ("Computer Music (2020 Remaster)", "Warp"),
    ("Tape Loops", "Hugh Hardie"),
    ("12 Pieces for Organ, Op. 7", "Naxos"),
    ("Nocturne in E-flat", "DG"),
    ("Modular Ambient in Cminor", "Bandcamp"),
    ("Musique Concrete", "INA GRM"),
    ("Shortwave radio", "Mille Plateaux"),
    ("Leiyla and the Poet", "Alga Marghen"),
    ("Sacred Spectrum", "Glacial Movements"),
    ("Concert Study N.1", "Naxos"),
    ("Prepared Piano Improvisation No. 4", "Another Timbre"),
]


def main() -> int:
    leaked = [t for t, u in MUST_REJECT if passes({"title": t, "uploader": u})]
    blocked = [t for t, u in MUST_KEEP if not passes({"title": t, "uploader": u})]
    print("reject set: %d/%d" % (len(MUST_REJECT) - len(leaked), len(MUST_REJECT)))
    for t in leaked:
        print("   LEAKED  %s" % t)
    print("keep set:   %d/%d" % (len(MUST_KEEP) - len(blocked), len(MUST_KEEP)))
    for t in blocked:
        print("   BLOCKED %s" % t)
    ok = not leaked and not blocked
    print("FILTER OK" if ok else "FILTER FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
