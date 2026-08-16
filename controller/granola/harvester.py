"""The sample harvester — a port of SampleHarvester's HARVEST layer to the Move.

The desktop tool is Swift plus a JVM helper (NewPipeExtractor), ffmpeg and yt-dlp. None of
that exists on this device: no JVM, no ffmpeg, no pip, no numpy — stdlib Python 3.10 and
whatever ships with the takeover. So the pipeline is the same shape, built from what the
Move actually has:

    discover   yt-dlp (pure Python, vendored) searching YouTube
    acquire    a BOUNDED PREFIX of the Opus stream — a harvest wants an excerpt, not a
               two-hour upload, and the device is on wifi
    decode     none needed: itag 251 is already Opus, so the packets are re-framed from
               WebM into Ogg by webm_opus.py and libsndfile (1.0.31) reads Ogg Opus
    region     the ENGINE picks the excerpt (granola-engine.scd, /gr/excerpt) — the audio
               is already decoded into a buffer there, and SC is better at this than
               numpy-less Python
    render     the excerpt is written as a plain wav into Granola's own sample library

The SETTINGS are SampleHarvester's own GenerationConfig defaults — sample count, duration
buckets, source-duration bounds, queries per run, candidates per query, one source per
uploader, fades, target loudness, boundary avoidance — with the term pools and query shapes
taken from TermPools.default and QueryGenerator. The single deliberate departure, as asked,
is that harmonisation is OFF and there is no harmonic target, so nothing is pitch-shifted.

Nothing here filters on CONTENT. The only rejections are technical: a source too short to
excerpt, or a region that is silence.
"""
from __future__ import annotations

import os
import urllib.parse
import random
import tempfile
import re
import shutil
import threading
import time
import traceback

# --- the app's own defaults ---------------------------------------------------
# These are SampleHarvester's GenerationConfig defaults, not invented ones. Where this
# port cannot honour a setting it says so rather than substituting a guess.
BATCH_MIN, BATCH_MAX = 1, 16
BATCH_DEFAULT = 8              # sampleCount: 8

# minDuration 3 / maxDuration 30, drawn through the app's weighted duration buckets, so a
# batch comes back with a spread of lengths rather than sixteen identical clips.
# Excerpt length. The app draws from weighted buckets spanning 2-60 s; here it is a plain
# range, because on this instrument a harvested sample is grain fodder rather than a clip —
# four to nine seconds is long enough to scan through and short enough to stay one gesture.
# Configurable from the web UI, clamped to DUR_FLOOR..DUR_CEIL.
DUR_DEFAULT_MIN = 4.0
DUR_DEFAULT_MAX = 9.0
DUR_FLOOR = 3.0
DUR_CEIL = 20.0

MIN_SOURCE_SECONDS = 45        # minSourceDuration: 45
MAX_SOURCE_SECONDS = 60 * 40   # maxSourceDuration: 40 minutes
QUERIES_PER_RUN = 6            # queriesPerRun: 6
CANDIDATES_PER_QUERY = 20      # candidatesPerQuery: 20
MAX_PER_UPLOADER = 1           # maxSourcesPerUploader: 1
# How many candidates one sample may burn before giving up on it. Not an app constant:
# the app's pipeline walks its whole ranked list, which is the same behaviour with no
# fixed bound. A bound here keeps a pathological run from stalling the batch on screen.
CANDIDATE_ATTEMPTS = 6
DIVERSITY = 0.75               # diversity: 0.75
RANDOMNESS = 0.5               # randomness: 0.5
FADE_IN_MS = 10.0              # fadeInMilliseconds: 10
FADE_OUT_MS = 20.0             # fadeOutMilliseconds: 20
TARGET_LOUDNESS_DB = -1.0      # targetLoudnessDB: -1.0
BOUNDARY_AVOIDANCE = 0.05      # boundaryAvoidance: 0.05, avoidBoundaries: true
# excerptSeconds: 240 — how much of each recording to FETCH and analyse. At the ~131 kbps
# of itag 251 that is about 4 MB, which is what the prefix download is sized to.
FETCH_SECONDS = 240
PREFIX_BYTES = int(FETCH_SECONDS * 131000 / 8)
NET_TIMEOUT = 30

# HARMONISATION IS OFF AND THERE IS NO HARMONIC TARGET — the one deliberate departure from
# the app's defaults (which are rootAlignment onto pitch class 0), as asked for. Nothing is
# pitch-shifted, so a harvested sample is the source material untouched apart from being
# cut out of it, faded and normalised.

# The app's own term pools (TermPools.default) and query shapes (QueryGenerator.Shape).
# There is no canned query list in either: each run recombines the pools, which is what
# keeps discovery unpredictable across sessions while staying inside the repertoire.
POOL_GENRE = [
    "electroacoustic", "acousmatic", "musique concrete", "tape music", "spectral music",
    "ambient", "drone", "experimental classical", "contemporary classical", "sound art",
    "soundscape composition", "abstract electronic", "minimalism",
    "field recording composition", "microsound", "computer music", "electronic studies",
    "avant-garde composition",
]
POOL_INSTRUMENTAL = [
    "bowed metal", "prepared piano", "extended cello", "processed voice", "tape loops",
    "electronics", "string quartet", "resonant objects", "bowed cymbal", "glass harmonica",
    "modular synthesizer", "pipe organ", "double bass harmonics",
    "bass clarinet multiphonics", "waterphone", "singing bowls", "feedback system",
    "contact microphone", "prepared guitar", "ondes martenot", "shortwave radio",
]
POOL_DESCRIPTIVE = [
    "abstract", "sparse", "sustained", "textural", "sound mass", "cluster", "microscopic",
    "unstable", "evolving", "granular", "shimmering", "static", "resonant", "diffuse",
    "spectral", "hovering", "corroded", "subterranean", "glacial",
]
POOL_FORMAT = ["composition", "concert", "work", "piece", "album", "recording", "study", "etude"]


# --- DiscoveryEngine (ported) ----------------------------------------------- #
#
# THE SEARCH IS THE YOUTUBE MUSIC CATALOGUE, NOT YOUTUBE.
#
# SampleHarvester's provider contract is `YouTubeMusicSourceProvider`, and DiscoveryEngine
# asks it for `SearchFilters(catalogue: .musicSongs, ...)` — the Songs tab of YouTube
# Music, which only contains catalogued music tracks. The first version of this port used
# yt-dlp's plain `ytsearch`, which searches ALL of YouTube: podcasts, tutorials, lectures
# and news are in that index, and no amount of downstream filtering makes up for drawing
# from the wrong pool to begin with. music.youtube.com's `#songs` section is the same
# catalogue the app's NewPipe helper queries.
#
# Songs only, deliberately: the multi-catalogue default on SearchFilters is never what
# discovery uses — it calls the SINGLE-catalogue initialiser, and album hits are playlist
# entries carrying no streams.
MUSIC_SEARCH = "https://music.youtube.com/search?q=%s#songs"

# --- THE FILTER. This is now the ONLY thing keeping a batch musical ------------ #
#
# There was a full port of the app's audio analysis behind this — speech/rhythmicity/
# silence gates on the decoded audio. It worked, but it cost 40-75 s of near-100% CPU per
# source on a device that has no headroom for it, and the Move hard-reset under the load.
# It is gone. Everything below runs on METADATA, costs nothing, and happens before a single
# byte of audio is fetched — which is also the only place a rejection is free.
#
# The layers, cheapest first:
#   1. the YouTube Music SONGS catalogue, which contains catalogued music and nothing else
#   2. negative terms   — the wrong repertoire, matched on title and channel
#   3. negative patterns — shapes that terms cannot catch (episode numbers, timestamps)
#   4. channel patterns  — the uploader is often a better signal than the title
#   5. positive terms + rank(), which ORDER what survives

# Copied from DiscoveryEngine.negativeTerms, then extended. The additions are marked; they
# are the spoken-word and non-music shapes that actually turned up in harvested batches
# here, not speculation.
NEGATIVE_TERMS = [
    # --- the app's own list ---
    "official video", "music video", "lyric", "lyrics", "remix", "bootleg", "dj set", "mixtape",
    "beat tape", "type beat", "trap", "drill", "house mix", "techno set", "edm", "workout",
    "study beats", "lofi", "lo-fi hip hop", "podcast", "interview", "tutorial", "lesson",
    "how to", "masterclass", "reaction", "review", "asmr", "sleep music", "meditation music",
    "karaoke", "cover version", "live stream", "gameplay", "audiobook", "sermon", "news",
    # --- added: spoken word in all its other guises ---
    "explained", "explainer", "documentary", "lecture", "seminar", "webinar", "keynote",
    "conference", "panel", "discussion", "conversation", "commentary", "narration",
    "narrated", "audio drama", "radio show", "talk show", "q&a", "ama", "vlog", "unboxing",
    "walkthrough", "guide", "tips", "tricks", "course", "class ", "lesson", "training",
    "meditation", "hypnosis", "affirmation", "prayer", "devotional", "bible", "quran",
    "story time", "storytime", "bedtime story", "fairy tale", "poem", "poetry reading",
    "speech", "debate", "testimony", "briefing", "press", "report", "analysis",
    # --- added: self-help / motivational spoken word ---
    "mindset", "self help", "self-help", "productivity", "life hack", "success habits",
    "motivational", "personal growth", "life advice", "wellness tips",
    # --- added: technical and hobbyist uploads (these really do surface) ---
    "raspberry pi", "arduino", "linux", "ham radio", "qrp", " cw ", "vlf", "shortwave receiver",
    "circuit", "soldering", "teardown", "firmware", "benchmark", "overclock",
    # --- added: not-music uploads that reach the music catalogue anyway ---
    "sound effect", "sfx", "ringtone", "white noise", "brown noise", "pink noise",
    "rain sounds", "ocean sounds", "nature sounds", "8 hours", "10 hours", "1 hour",
    "compilation", "top 10", "top 20", "best of", "playlist", "mix 20", "megamix",
    "full album", "greatest hits", "tier list", "trailer", "soundtrack from",
    "instrumental version", "backing track", "acapella", "a cappella version",
    "slowed", "reverb version", "sped up", "nightcore", "8d audio", "bass boosted",
]

# Shapes a word list cannot express. Matched against title + channel, lowercased.
NEGATIVE_PATTERNS = [
    re.compile(r"\bep\.?\s*\d+", re.I),          # "Ep 42", "Ep. 42" — serial spoken word
    re.compile(r"\bepisode\s*\d+", re.I),
    re.compile(r"\bpart\s*\d+\s*of\s*\d+", re.I),
    # "#312" — the podcast numbering habit. NOT \b#: "#" is not a word character, so \b
    # before it demands a word character immediately to its left, which means the common
    # "Experience #1999" (space before the hash) never matched.
    re.compile(r"(?<!\w)#\d{1,4}\b"),
    re.compile(r"\bs\d+e\d+\b", re.I),           # "S02E14"
    re.compile(r"\b\d{1,2}:\d{2}\b"),            # timestamps in a title = a talk index
    re.compile(r"\bvol\.?\s*\d+\s*[-|]\s*\d+", re.I),
    re.compile(r"\b(feat|ft)\.?\s*(dr|prof|rev)\b", re.I),
    re.compile(r"\bwith\s+(dr|prof|rev|sen|rep)\.?\s", re.I),
    re.compile(r"\b(how|why|what|when|who)\s+(i|we|to|you|they)\b", re.I),  # explainer titles
    # A professional title attached to a person is almost never a piece of music — it is a
    # guest credit. Caught "Change Your Brain: Neuroscientist Dr. Andrew Huberman", which
    # every word-list rule above let through.
    re.compile(r"\b(dr|prof|professor|rev|sen|rep)\.?\s+[a-z]", re.I),
    re.compile(r"\b(neuroscientist|psychologist|psychiatrist|nutritionist|therapist|"
               r"author|ceo|founder|entrepreneur|investor|expert|coach|journalist)\b", re.I),
    # Second-person self-help imperatives: "Change Your Brain", "Fix Your Sleep".
    re.compile(r"\b(change|fix|improve|boost|master|transform|heal|rewire|unlock)\s+your\b", re.I),
]

# The uploader is often a cleaner signal than the title: a channel that is a talk channel
# publishes talks whatever any individual title says.
NEGATIVE_CHANNEL_PATTERNS = [
    re.compile(r"\b(podcast|radio|fm|tv|news|media|talks?|show)\b", re.I),
    re.compile(r"\b(academy|university|college|institute|school|church|ministry)\b", re.I),
    re.compile(r"\b(gaming|gamer|reacts?|reviews?)\b", re.I),
]

# Terms that suggest the material is in scope (spec §3), plus additions in the same spirit.
POSITIVE_TERMS = [
    "electroacoustic", "acousmatic", "musique concrete", "concr\u00e8te", "tape", "spectral",
    "drone", "ambient", "soundscape", "field recording", "improvisation", "extended technique",
    "prepared", "experimental", "avant-garde", "avant garde", "contemporary", "microtonal",
    "sound art", "installation", "study", "etude", "composition", "for orchestra", "quartet",
    "electronic music", "computer music", "granular", "texture", "resonance",
    # --- added ---
    "sonata", "nocturne", "prelude", "fugue", "cantata", "requiem", "chamber",
    "ensemble", "octet", "sextet", "trio", "solo", "suite", "movement", "opus",
    "modular", "synthesizer", "analog", "oscillator", "feedback", "harmonics",
    "overtone", "polyphony", "counterpoint", "minimalism", "serialism", "aleatoric",
    "raga", "gamelan", "gagaku", "throat singing", "choir", "organ", "harpsichord",
]


def passes_metadata_filter(entry) -> bool:
    """Every metadata rejection, applied before any audio is fetched.

    Duration is only checked when it is KNOWN: the flat music-search listing does not carry
    one, and the app likewise skips the check at `duration > 0` rather than rejecting an
    unknown.
    """
    dur = entry.get("duration") or 0
    if dur > 0 and not (MIN_SOURCE_SECONDS <= dur <= MAX_SOURCE_SECONDS):
        return False
    title = (entry.get("title") or "").lower()
    chan = (entry.get("uploader") or entry.get("channel") or "").lower()
    hay = title + " " + chan
    if any(t in hay for t in NEGATIVE_TERMS):
        return False
    if any(p.search(hay) for p in NEGATIVE_PATTERNS):
        return False
    if chan and any(p.search(chan) for p in NEGATIVE_CHANNEL_PATTERNS):
        return False
    return True


def rank_candidate(entry, query: str, uploader_counts: dict) -> float:
    """DiscoveryEngine.rank. Ranking is the difference between a pool of music and a good
    pick from it — the first version shuffled the candidates and took the first that fit,
    which throws this away entirely."""
    hay = " ".join([entry.get("title") or "",
                    entry.get("uploader") or entry.get("channel") or "",
                    entry.get("description") or ""]).lower()
    score = float(sum(1.0 for t in POSITIVE_TERMS if t in hay))
    # The query that surfaced the item is itself evidence; weighted lightly so a single
    # lucky keyword cannot dominate.
    for w in query.lower().split():
        if len(w) > 3 and w in hay:
            score += 0.35
    dur = entry.get("duration") or 0
    if dur > 300:
        score += 0.8
    elif dur > 120:
        score += 0.4
    if dur > 1800:
        score -= 0.3
    who = entry.get("channel_id") or entry.get("uploader") or entry.get("channel")
    if who:
        score -= uploader_counts.get(who, 0) * 0.5
    return score


# Words that carry no material identity, so they must not make two titles look alike.
_STOP_TOKENS = {"live", "part", "version", "feat", "with", "from", "the", "and", "for",
                "opus", "op", "no", "movement", "album", "music", "song", "track"}


def make_query(rng: random.Random) -> str:
    """One query, composed the way QueryGenerator composes them."""
    g = lambda: rng.choice(POOL_GENRE)
    i = lambda: rng.choice(POOL_INSTRUMENTAL)
    d = lambda: rng.choice(POOL_DESCRIPTIVE)
    f = lambda: rng.choice(POOL_FORMAT)
    shape = rng.randrange(7)
    if shape == 0:
        return g()
    if shape == 1:
        return "%s %s" % (g(), f())
    if shape == 2:
        return "%s %s" % (g(), i())
    if shape == 3:
        return "%s %s" % (d(), i())
    if shape == 4:
        return "%s %s" % (d(), g())
    if shape == 5:
        return "%s %s" % (i(), f())
    return "%s %s %s" % (d(), g(), i())          # tripleStack


def pick_duration(rng: random.Random, lo: float = DUR_DEFAULT_MIN,
                  hi: float = DUR_DEFAULT_MAX) -> float:
    """A length from the configured range."""
    lo = max(DUR_FLOOR, min(DUR_CEIL, float(lo)))
    hi = max(lo, min(DUR_CEIL, float(hi)))
    return rng.uniform(lo, hi)


def safe_name(text: str, limit: int = 40) -> str:
    """A filename that survives this filesystem and reads as itself in the browser."""
    text = re.sub(r"[^A-Za-z0-9 _-]+", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return (text[:limit] or "harvest").strip("_")


class Harvest:
    """One batch. Runs on its own thread; the controller only reads its state."""

    def __init__(self, controller, count: int, out_dir: str, work_dir: str) -> None:
        self.c = controller
        self.count = max(BATCH_MIN, min(BATCH_MAX, count))
        self.out_dir = out_dir
        self.work_dir = work_dir
        self.samples: list[dict] = []      # what came back: {path, name, seconds, source}
        self.done = 0                      # how many of `count` are finished (ok or not)
        self._pool: dict[str, tuple[float, dict]] = {}   # ranked candidates, id -> (score, entry)
        self.stage = "idle"
        self.note = ""
        self.running = False
        self.cancelled = False
        self._thread: threading.Thread | None = None
        self._rng = random.Random()
        self._seen: set[str] = set()       # video ids used in THIS batch — no repeats
        self._uploaders: dict[str, int] = {}   # maxSourcesPerUploader: 1

    # -- lifecycle ---------------------------------------------------------- #
    @property
    def progress(self) -> float:
        return 0.0 if not self.count else min(1.0, self.done / float(self.count))

    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        self.cancelled = False
        self.done = 0
        self.samples = []
        self.stage = "searching"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        self.cancelled = True

    # -- the pipeline ------------------------------------------------------- #
    def _run(self) -> None:
        try:
            # A scratch dir that cannot be created must not end the harvest: fall back to
            # wherever this user CAN write. The configured path lives under a directory
            # the runtime user does not own on some installs, and the first version of
            # this simply raised on mkdir and reported nothing.
            try:
                os.makedirs(self.work_dir, exist_ok=True)
                probe = os.path.join(self.work_dir, ".w")
                open(probe, "wb").close()
                os.remove(probe)
            except OSError:
                self.work_dir = tempfile.mkdtemp(prefix="granola-harvest-")
                print("[granola] harvest: scratch dir not writable, using %s"
                      % self.work_dir, flush=True)
            os.makedirs(self.out_dir, exist_ok=True)
            import yt_dlp as _ytdlp
            self.stage = "searching"
            self.c._dirty = True
            # The previous batch stays visible until this one produces its first sample,
            # so starting a harvest never empties the pads before there is a replacement.
            self._discover(_ytdlp)
            for i in range(self.count):
                if self.cancelled:
                    break
                self.stage = "harvest %d/%d" % (i + 1, self.count)
                try:
                    got = self._one()
                    if got:
                        self.samples.append(got)
                        # Published as it lands, not at the end: a batch interrupted
                        # halfway still leaves every sample it did gather reachable.
                        self.c._last_batch = list(self.samples)
                        self.c._save_batch()
                except Exception:
                    # One bad source must not end the batch: a harvest is a lottery and
                    # some tickets are duds — a private video, a dead stream, a silent
                    # upload. Log it and go to the next.
                    print("[granola] harvest item failed", flush=True)
                    traceback.print_exc()
                self.done = i + 1
                self.c._dirty = True
            self.stage = "done" if not self.cancelled else "cancelled"
        except Exception as ex:
            self.stage = "failed"
            self.note = str(ex)[:40]
            self.c._notify("HARVEST FAILED")
            traceback.print_exc()
        finally:
            self.running = False
            self.c._dirty = True
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def _one(self) -> dict | None:
        """Produce one sample, trying candidates until one works.

        A dud candidate costs a candidate, not a sample. Sources fail for ordinary reasons
        — a 403 on the stream, a private upload, an item too short to excerpt — and the
        app's pipeline simply moves down its ranked list. Returning after the first
        failure, as the first version did, is what turned a batch of six into a batch of
        one.
        """
        import yt_dlp
        for _ in range(CANDIDATE_ATTEMPTS):
            if self.cancelled:
                return None
            if not self._pool:
                if self._discover(yt_dlp) == 0:
                    return None
            entry = self._next_candidate()
            if entry is None:
                return None
            got = self._try_candidate(yt_dlp, entry)
            if got:
                return got
        return None

    def _try_candidate(self, yt_dlp, entry) -> dict | None:
        from . import webm_opus

        vid = entry.get("id") or ""
        title = entry.get("title") or "harvest"
        self.note = title[:38]
        self.c._dirty = True

        raw = self._download_prefix(yt_dlp, vid)
        if not raw:
            return None

        self.stage = self.stage.split(" ")[0] + " %d/%d remux" % (self.done + 1, self.count)
        ogg = os.path.join(self.work_dir, "%s.opus" % safe_name(vid, 20))
        info = webm_opus.remux(raw, ogg)

        # The min-source gate, applied HERE because the music catalogue's flat listing
        # carries no duration — this is the first point at which the real length is known.
        # A short upload yields a thin, edge-heavy excerpt, which is what the gate exists
        # to prevent. Only the MINIMUM is enforced: the maximum exists in the app to bound
        # acquisition cost, and the bounded prefix fetch already does that.
        if info.get("seconds", 0) < MIN_SOURCE_SECONDS:
            print("[granola] harvest: %s too short (%.0fs), skipping"
                  % (vid, info.get("seconds", 0)), flush=True)
            try:
                os.remove(ogg)
            except OSError:
                pass
            return None

        # The engine decodes and chooses the excerpt; this waits for its reply.
        seconds = pick_duration(self._rng, *self.c.harvest_dur)
        name = "%s_%s.wav" % (safe_name(title, 32), vid[:6])
        dst = os.path.join(self.out_dir, name)
        # The stage says ANALYSING because this step is now tens of seconds of DSP, not a
        # quick cut. Without it the progress bar sits still and the harvester looks hung.
        self.stage = self.stage.split(" ")[0] + " %d/%d analysing" % (self.done + 1, self.count)
        self.c._dirty = True
        # No timeout override here: the controller's own EXCERPT_TIMEOUT is the one that
        # has to clear the analysis. A short override was what let a second request start
        # while the engine was still busy with the first.
        ok = self.c.excerpt_blocking(ogg, dst, seconds)
        try:
            os.remove(ogg)
        except OSError:
            pass
        if not ok:
            return None
        return {"path": dst, "name": os.path.splitext(name)[0][:22],
                "seconds": round(seconds, 1), "source": title[:60], "id": vid}

    def _discover(self, yt_dlp) -> int:
        """Run every query ONCE and build a ranked candidate pool.

        This mirrors DiscoveryEngine.discover, which runs for the whole generation rather
        than per sample. Searching per sample — what the first version did — is both
        slower and far more fragile: one throttled or empty search then costs a whole
        sample instead of one candidate out of a hundred.
        """
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "socket_timeout": NET_TIMEOUT, "playlistend": CANDIDATES_PER_QUERY,
                "extract_flat": "in_playlist", "ignoreerrors": True}
        rejected = 0
        added = 0
        for _ in range(QUERIES_PER_RUN):
            if self.cancelled:
                break
            query = make_query(self._rng)
            self.note = query
            self.c._dirty = True
            url = MUSIC_SEARCH % urllib.parse.quote_plus(query)
            try:
                with yt_dlp.YoutubeDL(opts) as y:
                    res = y.extract_info(url, download=False)
            except Exception as ex:
                print("[granola] harvest: search failed (%s): %s"
                      % (query, str(ex)[:80]), flush=True)
                continue
            entries = [x for x in (res or {}).get("entries") or [] if x]
            if not entries:
                print("[granola] harvest: search returned nothing for '%s'" % query,
                      flush=True)
            for e in entries:
                vid = e.get("id")
                if not vid or vid in self._seen or vid in self._pool:
                    continue
                if not passes_metadata_filter(e):
                    rejected += 1
                    continue
                self._pool[vid] = (rank_candidate(e, query, self._uploaders), e)
                added += 1
        # Logged unconditionally: the first version printed this only when the pool came
        # out non-empty, so the one case that most needed explaining — nothing found —
        # was the one case that said nothing.
        print("[granola] harvest: discovery added %d candidate(s), %d filtered out, "
              "pool now %d" % (added, rejected, len(self._pool)), flush=True)
        return added

    def _next_candidate(self) -> dict | None:
        """Draw one candidate by QUALIFIED RANDOMNESS rather than taking the top rank.

        RegionEngine.select is the model: weight each qualified item by
        `(quality * (1 - diversityPenalty)) ** (6 * (1 - randomness))` and draw. At
        randomness 0 the best item nearly always wins; at 1 every item is equally likely.
        The app's default of 0.5 gives exponent 3 — the ranking still dominates, but the
        run is not deterministic.

        This matters more than it looks. Taking the strict top rank produced a technically
        correct batch that was six variations of one idea (measured: four of six were
        cello), because ranking rewards the same terms every time. Qualified randomness is
        what makes a batch a spread rather than a cluster.

        ONE DELIBERATE DEVIATION: the app computes its diversity penalty from 10-dimensional
        audio descriptor vectors, which exist only after the analysis engine has run. There
        is no analysis layer here, so the penalty below is computed from TITLE TOKEN
        OVERLAP against what the batch already holds. It is a weaker proxy and is not
        claimed to be the same thing — but it is what stops the batch converging on one
        instrument, which is the failure it exists to prevent.
        """
        if not self._pool:
            return None
        items = []
        for vid, (score, e) in self._pool.items():
            who = e.get("channel_id") or e.get("uploader") or e.get("channel") or vid
            over = self._uploaders.get(who, 0) >= MAX_PER_UPLOADER
            # Uploader overflow is kept at the tail rather than dropped, exactly as
            # enforceUploaderDiversity does, so a batch still completes when a run has
            # drawn from few channels.
            quality = max(0.001, score) * (0.05 if over else 1.0)
            quality *= max(0.05, 1.0 - self._diversity_penalty(e))
            exponent = 6.0 * (1.0 - min(1.0, max(0.0, RANDOMNESS)))
            items.append((vid, quality ** exponent))
        total = sum(w for _, w in items)
        if total <= 0:
            vid = max(self._pool, key=lambda k: self._pool[k][0])
        else:
            r = self._rng.uniform(0, total)
            vid = items[-1][0]
            for k, w in items:
                if r <= w:
                    vid = k
                    break
                r -= w
        _score, e = self._pool.pop(vid)
        who = e.get("channel_id") or e.get("uploader") or e.get("channel") or vid
        self._uploaders[who] = self._uploaders.get(who, 0) + 1
        self._seen.add(vid)
        return e

    @staticmethod
    def _tokens(text: str) -> set:
        return {w for w in re.split(r"[^a-z0-9]+", (text or "").lower())
                if len(w) > 3 and w not in _STOP_TOKENS}

    def _diversity_penalty(self, entry) -> float:
        """0 = nothing like the batch so far, 1 = indistinguishable from something in it."""
        if DIVERSITY <= 0 or not self.samples:
            return 0.0
        mine = self._tokens(entry.get("title") or "")
        if not mine:
            return 0.0
        nearest = 0.0
        for s in self.samples:
            theirs = self._tokens(s.get("source") or "")
            if not theirs:
                continue
            union = mine | theirs
            nearest = max(nearest, len(mine & theirs) / float(len(union)))
        return nearest * DIVERSITY

    def _download_prefix(self, yt_dlp, vid: str) -> bytes | None:
        """Fetch only the head of the audio stream.

        yt-dlp does the fetching rather than urllib: YouTube binds its stream URLs to the
        request context that produced them, and a hand-rolled GET with the same headers
        gets a flat 403 (measured). Aborting from a progress hook leaves a .part file,
        which is exactly the WebM prefix wanted — the remuxer expects truncation.
        """
        target = os.path.join(self.work_dir, "src")

        class Enough(Exception):
            pass

        def hook(d):
            if self.cancelled:
                raise Enough()
            if d.get("status") == "downloading" and (d.get("downloaded_bytes") or 0) >= PREFIX_BYTES:
                raise Enough()

        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "socket_timeout": NET_TIMEOUT, "format": "251/250/249",
                "outtmpl": target + ".%(ext)s", "http_chunk_size": 262144,
                "progress_hooks": [hook], "retries": 1, "fragment_retries": 1,
                "ignoreerrors": True}
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                y.download(["https://www.youtube.com/watch?v=%s" % vid])
        except Exception:
            pass                                  # the abort is the normal exit here
        best, size = None, 0
        for f in os.listdir(self.work_dir):
            if f.startswith("src"):
                p = os.path.join(self.work_dir, f)
                n = os.path.getsize(p)
                if n > size:
                    best, size = p, n
        if not best or size < 60000:
            return None
        with open(best, "rb") as fh:
            data = fh.read()
        try:
            os.remove(best)
        except OSError:
            pass
        return data
