"""Granola web UI (stdlib only, Python 3.10 on the Move).

Served on http://move.local:7135. It runs INSIDE the controller process and holds a
reference to it, so a change made in a browser and the same change made on the hardware
go through one code path — there is no second copy of the state to drift out of step.
(OneManShow needs a file spool for this because its engine is sclang; here the model is
already Python, in this process.)

The page is a schematic of the Move: eight columns of one encoder over four pads, the
sixteen step buttons underneath, the four Track buttons and the Menu button down the
left. Pad and step colours are read from the Move's OWN LED palette so what the browser
shows is what the hardware shows.

Routes
    GET  /                      the page
    GET  /api/state             live machine + projects + sample pool
    GET  /api/samples           the discovered sample pool
    POST /api/track/sample      {track, path|null}   assign / clear a track's sample
    POST /api/track/macro       {track, slot}        toggle a parameter pad
    POST /api/upload?name=X.wav raw body             drop a file into granola/samples
    POST /api/project/save      {slot, name?}
    POST /api/project/load      {slot}
    POST /api/project/rename    {slot, name}
    POST /api/project/delete    {slot}
    POST /api/transport         {running?}           start / stop every granulator
    POST /api/fx/slot           {slot, on?}          toggle a chain slot (ON = roll a chain)
    POST /api/fx/reroll         {slot}               re-roll a running slot
    POST /api/fx/lock           {slot, locked?}      lock a slot's chain against re-rolling
    POST /api/fx/wet            {slot, wet}          the slot's dry/wet, across its chain
    POST /api/fx/track          {track, on?}         route a track through the chain
    POST /api/harvest           {count?}             start a harvest batch
    POST /api/harvest/duration  {min, max}           excerpt length range, 3-20 s
    POST /api/harvest/assign    {sample, track}      put a harvested sample on a track
    POST /api/rescan
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .airwindows import FX_CPU_BUDGET
from .params import MACRO_SLOTS, SPECS, VIEW_ROWS, format_value
from .projects import N_SLOTS
from .samples import NATIVE_EXT
from .tracks import N_TRACKS

CONSTANTS = "/data/UserData/move-anything/shared/constants.mjs"
SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,48}$")
MAX_UPLOAD = 256 * 1024 * 1024

# The eight track hues, as [lit, dark] palette indices. Must match TRACK_COL in ui.js —
# this is the one place the web page and the hardware could disagree about identity, so
# the indices are resolved to real hex through the Move's own palette table below.
TRACK_COL = [(1, 66), (3, 75), (7, 78), (8, 79), (12, 87), (16, 95), (20, 104), (23, 110)]


def load_palette() -> dict[int, str]:
    """Parse the Move's LED palette (index -> hex) out of the framework constants, so a
    swatch in the browser is the colour the hardware actually lights."""
    pal: dict[int, str] = {}
    try:
        with open(CONSTANTS) as f:
            for line in f:
                m = re.match(r"\s*(\d+)\s*:\s*#([0-9A-Fa-f]{6})\b", line)
                if m:
                    idx = int(m.group(1))
                    if 0 <= idx <= 127 and idx not in pal:
                        pal[idx] = "#" + m.group(2).upper()
    except OSError:
        pass
    return pal


PALETTE = load_palette()


def hexfor(idx: int, fallback: str = "#444444") -> str:
    return PALETTE.get(idx, fallback)


TRACK_HEX = [[hexfor(a), hexfor(b)] for a, b in TRACK_COL]


class Handler(BaseHTTPRequestHandler):
    controller = None            # injected by serve()
    samples_dir = "/data/UserData/granola/samples"

    # -- plumbing ---------------------------------------------------------- #
    def log_message(self, *_a) -> None:
        pass                     # the controller's log is for the instrument, not for HTTP

    def _send(self, code: int, payload, ctype: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    # -- GET --------------------------------------------------------------- #
    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, self._state())
        if u.path == "/api/samples":
            return self._send(200, {"samples": self._sample_list()})
        return self._send(404, {"error": "not found"})

    def _sample_list(self) -> list[dict]:
        c = self.controller
        out = []
        for p in c.pool:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            out.append({"path": p, "name": Path(p).name,
                        "dir": str(Path(p).parent), "size": size})
        return out

    def _state(self) -> dict:
        c = self.controller
        with c._lock:
            tracks = []
            for t in c.model.tracks:
                tracks.append({
                    "i": t.index,
                    "name": t.sample_name,
                    "path": t.sample_path,
                    "loaded": t.loaded,
                    "dur": round(t.duration, 3),
                    "channels": t.channels,
                    "mute": t.mute,
                    "sel": [s in t.macros for s in MACRO_SLOTS],
                    # Same rule as status.json: a track with no voice has no engine
                    # playhead, so fall back to the model's position. The page and the
                    # hardware must never disagree about where a track is.
                    "head": round(t.head if t.loaded else t.value("position"), 4),
                    "label": t.macro_label,
                    "vals": {s: round(t.normalized(s), 4) for s in MACRO_SLOTS},
                    "shown": {s: format_value(s, t.value(s)) for s in MACRO_SLOTS},
                })
            projects = [
                {"slot": i, "filled": c.projects.filled[i],
                 "meta": c.projects.meta(i)}
                for i in range(N_SLOTS)
            ]
            return {
                "ready": c.bridge.ready,
                "engine": c.bridge.connected,
                "cpu": round(c.bridge.cpu.get("avg", 0.0), 1),
                "focus": c.model.focus,
                "master": round(c.model.master, 3),
                "running": c.running,
                # Post-limiter output level, so the page can show that the instrument is
                # actually making sound rather than only that it thinks it is.
                "level": round(max(c.bridge.master_amp), 3),
                # Both channels, not just the peak: stereo placement is half of what a
                # granulator does, and a single number cannot show it.
                "levelLR": [round(v, 4) for v in c.bridge.master_amp],
                "poolSize": len(c.pool),
                "tracks": tracks,
                "projects": projects,
                "projCur": c.projects.current,
                "fxSlots": [sl.active for sl in c.fx_slots],
                "fxLocked": [sl.locked for sl in c.fx_slots],
                "fxWet": [round(sl.wet, 3) for sl in c.fx_slots],
                "fxLabels": [sl.label for sl in c.fx_slots],
                "fxChains": [[{"name": l.effect.name, "mix": round(l.mix, 3),
                               "mod": l.modulated} for l in sl.chain] for sl in c.fx_slots],
                "fxTracks": sorted(c.fx_tracks),
                "fxSummary": c.fx_summary,
                "fxLive": bool(c.fx_tracks),
                "harvest": c.harvest_state,
                "fxCount": len(c.catalog),
                "fxCost": round(c.fx_cost, 1),
                "fxBudget": FX_CPU_BUDGET,
                "slots": MACRO_SLOTS,
                # The pad rows each view exposes, so the page groups them the way the
                # hardware does rather than hard-coding a second copy of the layout.
                "views": {str(v): list(rows) for v, rows in VIEW_ROWS.items()},
                "colors": TRACK_HEX,
            }

    # -- POST -------------------------------------------------------------- #
    def do_POST(self) -> None:
        u = urllib.parse.urlparse(self.path)
        c = self.controller
        if c is None:
            return self._send(503, {"error": "controller not ready"})

        if u.path == "/api/upload":
            return self._upload(urllib.parse.parse_qs(u.query))

        b = self._body()

        if u.path == "/api/track/sample":
            track = int(b.get("track", -1))
            path = b.get("path")
            ok = c.set_track_sample(track, path if path else None)
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/track/macro":
            track, slot = int(b.get("track", -1)), int(b.get("slot", -1))
            with c._lock:
                res = c.model.toggle_macro(track, slot)
            c._dirty = True
            return self._send(200, {"ok": res is not None, "on": res})

        if u.path == "/api/project/save":
            name = b.get("name")
            if name is not None and not SAFE_NAME.match(str(name)):
                return self._send(400, {"error": "bad name"})
            ok = c.save_project(int(b.get("slot", -1)), name)
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/project/load":
            ok = c.load_project(int(b.get("slot", -1)))
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/project/rename":
            name = str(b.get("name", ""))
            if not SAFE_NAME.match(name):
                return self._send(400, {"error": "bad name"})
            ok = c.projects.rename(int(b.get("slot", -1)), name)
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/project/delete":
            ok = c.projects.delete(int(b.get("slot", -1)))
            c._dirty = True
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/transport":
            want = b.get("running")
            with c._lock:
                c.running = (not c.running) if want is None else bool(want)
                c.bridge.run(c.running)
            c._dirty = True
            return self._send(200, {"ok": True, "running": c.running})

        if u.path == "/api/fx/slot":
            i = int(b.get("slot", -1))
            want = b.get("on")
            if not 0 <= i < len(c.fx_slots):
                return self._send(400, {"error": "bad slot"})
            c.set_fx_slot(i, (not c.fx_slots[i].active) if want is None else bool(want))
            return self._send(200, {"ok": True, "on": c.fx_slots[i].active,
                                    "chain": c.fx_slots[i].label})

        if u.path == "/api/fx/reroll":
            i = int(b.get("slot", -1))
            if not 0 <= i < len(c.fx_slots):
                return self._send(400, {"error": "bad slot"})
            # A refusal is not a malformed request: the caller asked something valid and
            # the answer is no. 400 here made "locked" indistinguishable from "no such
            # slot", and raised on clients that treat 4xx as an error.
            slot = c.fx_slots[i]
            if slot.locked:
                return self._send(200, {"ok": False, "reason": "locked",
                                        "chain": slot.label})
            if not slot.active:
                return self._send(200, {"ok": False, "reason": "inactive"})
            c.reroll_fx_slot(i)
            return self._send(200, {"ok": True, "chain": slot.label})

        if u.path == "/api/fx/lock":
            i = int(b.get("slot", -1))
            want = b.get("locked")
            ok = c.set_fx_lock(i, None if want is None else bool(want))
            return self._send(200 if ok else 400,
                              {"ok": ok, "locked": ok and c.fx_slots[i].locked})

        if u.path == "/api/fx/wet":
            i = int(b.get("slot", -1))
            ok = c.set_fx_wet(i, value=float(b.get("wet", 0.4)))
            return self._send(200 if ok else 400,
                              {"ok": ok, "wet": c.fx_slots[i].wet if ok else None})

        if u.path == "/api/fx/track":
            t = int(b.get("track", -1))
            want = b.get("on")
            ok = c.set_fx_track(t, (t not in c.fx_tracks) if want is None else bool(want))
            return self._send(200 if ok else 400, {"ok": ok, "tracks": sorted(c.fx_tracks)})

        if u.path == "/api/harvest/duration":
            lo, hi = c.set_harvest_dur(b.get("min", 4), b.get("max", 9))
            return self._send(200, {"ok": True, "min": lo, "max": hi})

        if u.path == "/api/harvest":
            n = b.get("count")
            ok = c.start_harvest(None if n is None else int(n))
            return self._send(200, {"ok": ok, "harvest": c.harvest_state})

        if u.path == "/api/harvest/assign":
            ok = c.assign_harvested(int(b.get("sample", -1)), int(b.get("track", -1)))
            return self._send(200 if ok else 400, {"ok": ok})

        if u.path == "/api/rescan":
            return self._send(200, {"ok": True, "count": c.rescan_samples()})

        return self._send(404, {"error": "not found"})

    def _upload(self, q: dict) -> None:
        name = os.path.basename((q.get("name") or [""])[0])
        if not name or Path(name).suffix.lower() not in NATIVE_EXT:
            return self._send(400, {"error": "unsupported format"})
        if not SAFE_NAME.match(name):
            return self._send(400, {"error": "bad name"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._send(400, {"error": "empty"})
        if n > MAX_UPLOAD:
            return self._send(413, {"error": "too large"})
        dest = Path(self.samples_dir) / name
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            remaining = n
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            # Atomic: a rescan can never see a half-written file and offer it as a sample.
            os.replace(tmp, dest)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self._send(500, {"error": str(e)})
        self.controller.rescan_samples()
        return self._send(200, {"ok": True, "path": str(dest), "name": name})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(controller, port: int = 7135):
    """Start the web UI in a daemon thread. Never blocks the controller."""
    Handler.controller = controller
    Handler.samples_dir = os.environ.get("GR_SAMPLES", Handler.samples_dir)
    srv = _Server(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


PAGE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Granola — Move</title>
<style>
 :root{--bg:#0b0c0f;--panel:#14161c;--panel2:#1a1d25;--line:#272c37;--txt:#e9edf2;
       --dim:#818c9c;--faint:#525b69;--acc:#c8f04a;--r:12px}
 *{box-sizing:border-box;margin:0;padding:0}
 body{background:var(--bg);color:var(--txt);font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;
      -webkit-font-smoothing:antialiased;padding:18px;max-width:1240px;margin:0 auto}
 h1{font-size:15px;letter-spacing:.3em;text-transform:uppercase;font-weight:700}
 h1 span{color:var(--acc)}
 h2{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--faint);
    font-weight:600;margin-bottom:9px}
 .top{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:12px;flex-wrap:wrap}
 .stat{display:flex;gap:14px;align-items:center;font-size:12px;color:var(--dim)}
 .dot{width:8px;height:8px;border-radius:50%;background:#d0342c;display:inline-block;margin-right:6px}
 .dot.on{background:var(--acc);box-shadow:0 0 8px #c8f04a80}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px;margin-bottom:14px}
 button{font:inherit;font-size:12px;background:var(--panel2);color:var(--txt);border:1px solid var(--line);
        border-radius:8px;padding:7px 12px;cursor:pointer}
 button:hover{border-color:#4a5364}
 button.acc{background:var(--acc);color:#0b0c0f;border-color:var(--acc);font-weight:700}
 button.run{background:#56bf13;color:#0b0c0f;border-color:#56bf13;font-weight:700}
 .meter{display:inline-block;width:64px;height:7px;border-radius:4px;background:#1a1d25;
        border:1px solid var(--line);overflow:hidden;vertical-align:middle}
 .meter i{display:block;height:100%;width:0;background:var(--acc);transition:width .12s}
 /* ---- the machine ---- */
 .machine{display:grid;grid-template-columns:74px 1fr;gap:12px}
 .side{display:flex;flex-direction:column;gap:6px;padding-top:64px}
 .sbtn{border:1px solid var(--line);border-radius:7px;background:var(--panel2);color:var(--faint);
       font-size:9px;letter-spacing:.1em;padding:7px 4px;text-align:center;cursor:pointer;font-weight:600}
 .sbtn.on{background:#eef2f7;color:#0b0c0f;border-color:#eef2f7}
 .sbtn.res{opacity:.4;cursor:default}
 .cols{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}
 .col{display:flex;flex-direction:column;gap:6px}
 .enc{position:relative;aspect-ratio:1;border-radius:50%;border:2px solid var(--line);
      background:radial-gradient(circle at 50% 38%,#242832,#12141a);display:flex;
      align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--dim)}
 .enc.focus{border-color:#eef2f7;color:#eef2f7}
 .enc .ring{position:absolute;inset:-2px;border-radius:50%;pointer-events:none}
 .pad{aspect-ratio:1.35;border-radius:8px;border:1px solid #22252d;background:#111319;cursor:pointer;
      display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;
      letter-spacing:.06em;color:#6c7684;position:relative;transition:transform .06s}
 .pad:active{transform:scale(.95)}
 /* View 2's row, set apart so the page does not imply five rows of pads on the hardware */
 /* First row of a view group: a gap above it, and labelled once on the leftmost column —
  * the row is one thing, not eight things. */
 .pad.grp{margin-top:9px;position:relative}
 .pad.grpfirst::before{content:attr(data-grp);position:absolute;top:-13px;left:1px;
                 font-size:7px;letter-spacing:.14em;color:var(--faint);font-weight:600;
                 white-space:nowrap}
 .pad .lbl{position:relative;z-index:2;text-shadow:0 1px 2px #000a}
 .pad .val{position:absolute;bottom:2px;right:4px;font-size:8px;font-weight:500;opacity:.85;z-index:2}
 .slot{border:1px solid var(--line);border-radius:8px;background:var(--panel2);padding:7px;
       min-height:60px;display:flex;flex-direction:column;gap:4px;cursor:pointer}
 .slot:hover{border-color:#4a5364}
 .slot .nm{font-size:10px;line-height:1.25;word-break:break-word;max-height:38px;overflow:hidden}
 .slot .meta{font-size:9px;color:var(--faint)}
 .slot.empty .nm{color:var(--faint);font-style:italic}
 .slot.loaded{border-color:#3c4a2a}
 .steps{display:grid;grid-template-columns:repeat(16,1fr);gap:4px;margin-top:10px}
 .step{height:22px;border-radius:4px;border:1px solid #20232b;background:#101218}
 .lg{display:flex;gap:14px;font-size:10px;color:var(--faint);margin-top:8px;flex-wrap:wrap}
 /* ---- projects ---- */
 .pgrid{display:grid;grid-template-columns:repeat(8,1fr);gap:6px}
 .pj{border:1px solid var(--line);border-radius:8px;background:#101218;padding:7px 6px;min-height:54px;
     cursor:pointer;display:flex;flex-direction:column;justify-content:space-between}
 .pj .n{font-size:9px;color:var(--faint)}
 .pj .t{font-size:10px;font-weight:600;line-height:1.2;word-break:break-word}
 .pj.filled{background:#131a26;border-color:#2b4468}
 .pj.filled .t{color:#a8c8f0}
 .pj.cur{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc) inset}
 .pj.cur .t{color:var(--acc)}
 .pjbar{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
 /* ---- fx ---- */
 .sub{font-size:10px;color:var(--faint);letter-spacing:.1em;text-transform:uppercase;margin-bottom:7px}
 .fxwrap{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 @media(max-width:820px){.fxwrap{grid-template-columns:1fr}}
 .fxtracks{display:grid;grid-template-columns:repeat(8,1fr);gap:6px}
 .fxt{aspect-ratio:1.3;border-radius:8px;border:1px solid #22252d;background:#111319;cursor:pointer;
      display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700}
 .fxslots{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
 .fxs{border:1px solid var(--line);border-radius:8px;background:#111319;padding:7px 6px;min-height:58px;
      cursor:pointer;display:flex;flex-direction:column;gap:3px;justify-content:space-between}
 .fxs .n{font-size:9px;color:var(--faint)}
 .fxs .c{font-size:9px;line-height:1.25;word-break:break-word;color:var(--dim)}
 .fxs.on{background:#241a06;border-color:#FF9900}
 .fxs.on .c{color:#ffcc80}
 .fxs.on .n{color:#FF9900}
 .fxs .wet{width:100%;height:3px;accent-color:#FF9900;cursor:pointer;margin:2px 0}
 .fxs.lk .wet{accent-color:#00FFFF}
 .fxs .row{display:flex;justify-content:space-between;align-items:center}
 .fxs .roll,.fxs .lock{font-size:10px;color:var(--faint);cursor:pointer}
 .fxs .roll:hover,.fxs .lock:hover{color:var(--txt)}
 /* Locked slots move to the CYAN family, exactly as on the hardware, so the page and the
  * pads never disagree about which chains are pinned. */
 .fxs.lk{border-color:#00FFFF;background:#04211f}
 .fxs.lk .n{color:#00FFFF}
 .fxs.lk .c{color:#8fe9e4}
 .fxs.lk .roll{opacity:.3}
 .chain{margin-top:12px;font-size:11px;color:var(--dim);border-top:1px solid var(--line);
        padding-top:10px;word-break:break-word}
 /* ---- sample browser ---- */
 .modal{position:fixed;inset:0;background:#000000c4;display:none;align-items:center;justify-content:center;z-index:50}
 .modal.on{display:flex}
 .sheet{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(680px,94vw);
        max-height:82vh;display:flex;flex-direction:column;overflow:hidden}
 .sheet header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;
               justify-content:space-between;align-items:center;gap:10px}
 .sheet .body{overflow:auto;padding:8px}
 .srch{width:100%;background:#0d0f14;border:1px solid var(--line);border-radius:8px;color:var(--txt);
       font:inherit;font-size:13px;padding:9px 11px;margin:8px 0}
 .item{padding:9px 11px;border-radius:8px;cursor:pointer;display:flex;justify-content:space-between;gap:10px}
 .item:hover{background:var(--panel2)}
 .item .p{font-size:10px;color:var(--faint);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46%}
 .drop{border:1.5px dashed var(--line);border-radius:10px;padding:14px;text-align:center;color:var(--dim);
       font-size:12px;margin:8px}
 .drop.hot{border-color:var(--acc);color:var(--acc)}
 .hvrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.hvrow label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.hvrow input{width:66px;background:#0c0c0e;border:1px solid #2a2a30;color:var(--fg);
  border-radius:6px;padding:5px 7px;font:inherit;font-size:13px}
.hvhint{font-size:11px;color:var(--faint);margin-bottom:8px}
.hvbar{height:6px;background:#0c0c0e;border:1px solid #2a2a30;border-radius:4px;overflow:hidden}
.hvbar i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
.hvlist{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.hvchip{display:flex;gap:6px;align-items:baseline;background:#141418;border:1px solid #2a2a30;
  border-radius:6px;padding:4px 8px;font-size:12px}
.hvchip b{font-weight:500}
.hvchip span{color:var(--faint);font-variant-numeric:tabular-nums}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#eef2f7;color:#0b0c0f;
        font-size:12px;font-weight:700;padding:9px 16px;border-radius:8px;opacity:0;transition:opacity .2s;
        pointer-events:none;z-index:60}
 .toast.on{opacity:1}
</style></head><body>

<div class="top">
  <h1>GRAN<span>OLA</span></h1>
  <div class="stat">
    <span><i class="dot" id="d-eng"></i><span id="t-eng">connecting</span></span>
    <span id="t-cpu">–</span>
    <span id="t-pool">–</span>
    <span class="meter"><i id="m-fill"></i></span>
    <button id="b-play" onclick="transport()">▶ PLAY</button>
    <button onclick="rescan()">Rescan samples</button>
  </div>
</div>

<div class="card">
  <h2>View 1 — granular performance</h2>
  <div class="machine">
    <div class="side">
      <div class="sbtn on">TRK 1</div>
      <div class="sbtn res">TRK 2</div>
      <div class="sbtn res">TRK 3</div>
      <div class="sbtn res">TRK 4</div>
      <div class="sbtn" style="margin-top:10px" onclick="document.getElementById('projects').scrollIntoView({behavior:'smooth'})">MENU</div>
    </div>
    <div>
      <div class="cols" id="cols"></div>
      <div class="steps" id="steps"></div>
      <div class="lg">
        <span>Play starts / stops every granulator</span>
        <span>· Encoder with no pad lit = sample scan / playhead</span>
        <span>· View 3 = volume, pan, delay send, reverb send</span>
        <span>· Pads toggle Size / Density / Jitter / Pitch</span>
        <span>· Several lit = macro over all of them</span>
        <span>· Step bar follows the focused track</span>
      </div>
    </div>
  </div>
</div>

<div class="card" id="fx">
  <h2>View 4 — performance FX <span id="fx-n" style="color:var(--faint)"></span></h2>
  <div class="fxwrap">
    <div>
      <div class="sub">Tracks routed through the chain</div>
      <div class="fxtracks" id="fxtracks"></div>
    </div>
    <div>
      <div class="sub">Chain slots — click to roll · 🔓 locks a chain · ↻ re-rolls</div>
      <div class="fxslots" id="fxslots"></div>
    </div>
  </div>
  <div class="chain" id="fxsummary">no chain</div>
</div>

<div class="card" id="harvester">
  <h2>Sample harvester <span id="hv-stage" style="color:var(--faint)"></span></h2>
  <div class="hvrow">
    <label>Excerpt length</label>
    <input type="number" id="hv-min" min="3" max="20" step="0.5" onchange="setDur()">
    <span style="color:var(--dim)">to</span>
    <input type="number" id="hv-max" min="3" max="20" step="0.5" onchange="setDur()">
    <span style="color:var(--dim)">seconds</span>
    <span style="flex:1"></span>
    <label>Batch</label>
    <input type="number" id="hv-count" min="1" max="16" step="1" value="8">
    <button onclick="harvest()" id="hv-go">Harvest</button>
  </div>
  <div class="hvhint" id="hv-hint">Every harvested sample lands between these lengths. Range 3–20 s.</div>
  <div class="hvbar"><i id="hv-fill"></i></div>
  <div class="hvlist" id="hv-list"></div>
</div>

<div class="card" id="projects">
  <h2>Projects — 32 slots</h2>
  <div class="pgrid" id="pgrid"></div>
  <div class="pjbar">
    <span style="font-size:11px;color:var(--dim)" id="pjhint">Click a slot to load · Save writes the live machine</span>
    <span style="flex:1"></span>
    <input class="srch" style="width:190px;margin:0" id="pjname" placeholder="name (optional)">
    <button class="acc" onclick="saveInto()">Save to slot…</button>
  </div>
</div>

<div class="modal" id="modal"><div class="sheet">
  <header><strong id="m-title">Sample</strong><button onclick="closeModal()">Close</button></header>
  <div style="padding:0 8px"><input class="srch" id="q" placeholder="Filter samples…" oninput="renderList()"></div>
  <div class="drop" id="drop">Drop an audio file here to upload it to the Move</div>
  <div class="body" id="list"></div>
</div></div>

<div class="toast" id="toast"></div>

<script>
let S = null, target = -1, samples = [], saveArm = false, stale = false;

const $ = (id) => document.getElementById(id);
function toast(t){ const e=$('toast'); e.textContent=t; e.classList.add('on'); setTimeout(()=>e.classList.remove('on'),1600); }
async function api(path, body){
  const o = body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {};
  try { const r = await fetch(path,o); return await r.json(); } catch(e){ return null; }
}

/* ---- render ---- */
function build(){
  const cols = $('cols'); cols.innerHTML='';
  for(let t=0;t<8;t++){
    const c=document.createElement('div'); c.className='col';
    const e=document.createElement('div'); e.className='enc'; e.id='enc'+t; e.textContent=(t+1);
    c.appendChild(e);
    /* Eleven selectors, grouped by the view that owns them — the page shows every
     * parameter at once, but never implies the hardware has eleven rows of pads. */
    for(let s=0;s<11;s++){
      const p=document.createElement('div');
      const head = (s===4) ? 2 : (s===7 ? 3 : 0);
      p.className='pad'+(head?' grp'+(t===0?' grpfirst':''):'');
      if(head) p.dataset.grp='VIEW '+head; p.id='pad'+t+'_'+s;
      p.innerHTML='<span class="lbl"></span><span class="val"></span>';
      p.onclick=()=>api('/api/track/macro',{track:t,slot:s}).then(refresh);
      c.appendChild(p);
    }
    const sl=document.createElement('div'); sl.className='slot'; sl.id='slot'+t;
    sl.onclick=()=>openModal(t);
    c.appendChild(sl);
    cols.appendChild(c);
  }
  const st=$('steps'); st.innerHTML='';
  for(let i=0;i<16;i++){ const d=document.createElement('div'); d.className='step'; d.id='st'+i; st.appendChild(d); }
  const ft=$('fxtracks'); ft.innerHTML='';
  for(let t=0;t<8;t++){
    const d=document.createElement('div'); d.className='fxt'; d.id='fxt'+t; d.textContent=(t+1);
    d.onclick=()=>api('/api/fx/track',{track:t}).then(refresh);
    ft.appendChild(d);
  }
  const fs=$('fxslots'); fs.innerHTML='';
  for(let i=0;i<4;i++){
    const d=document.createElement('div'); d.className='fxs'; d.id='fxs'+i;
    d.innerHTML='<div class="n">SLOT '+(i+1)+'</div><div class="c"></div>'
      +'<input class="wet" type="range" min="0" max="1" step="0.01" title="dry / wet">'
      +'<div class="row"><span class="lock" title="lock this chain">&#128275;</span>'
      +'<span class="roll" title="re-roll">&#8635;</span></div>';
    d.querySelector('.wet').oninput=(e)=>{
      e.stopPropagation();
      api('/api/fx/wet',{slot:i, wet:parseFloat(e.target.value)});
    };
    d.querySelector('.wet').onclick=(e)=>e.stopPropagation();
    d.onclick=(e)=>{
      if(e.target.classList.contains('roll')) api('/api/fx/reroll',{slot:i}).then(refresh);
      else if(e.target.classList.contains('lock')) api('/api/fx/lock',{slot:i}).then(refresh);
      else api('/api/fx/slot',{slot:i}).then(refresh);
    };
    fs.appendChild(d);
  }
  const pg=$('pgrid'); pg.innerHTML='';
  for(let i=0;i<32;i++){
    const d=document.createElement('div'); d.className='pj'; d.id='pj'+i;
    d.innerHTML='<div class="n">'+(i+1)+'</div><div class="t"></div>';
    d.onclick=()=>clickSlot(i);
    pg.appendChild(d);
  }
}

function paint(){
  /* A poll that fails (the stack restarting under us) used to leave the header reading
   * "undefined" over pads still showing the last good values — half a picture, and the
   * worse half. Keep the last known state on screen, and say plainly that it is stale. */
  if(!S || !Array.isArray(S.tracks)) return;
  $('d-eng').className = 'dot' + (S.ready && !stale ? ' on':'');
  $('t-eng').textContent = stale ? 'reconnecting…'
      : (S.ready ? 'engine ready' : (S.engine ? 'booting' : 'offline'));
  $('t-cpu').textContent = 'CPU ' + (S.cpu==null?'–':S.cpu) + '%';
  $('t-pool').textContent = S.poolSize + (S.poolSize===1 ? ' sample' : ' samples');
  const pb=$('b-play');
  pb.textContent = S.running ? '■ STOP' : '▶ PLAY';
  pb.className = S.running ? 'run' : '';
  $('m-fill').style.width = Math.min(100, Math.round((S.level||0)*140)) + '%';
  const names = ['SIZE','DENS','JIT','PITCH','DRIFT','SPRD','SHAPE','VOL','PAN','DLY','RVB'];
  for(let t=0;t<8;t++){
    const tr = S.tracks[t], col = S.colors[t];
    const e = $('enc'+t);
    e.className = 'enc' + (S.focus===t?' focus':'');
    e.style.borderColor = S.focus===t ? '#eef2f7' : (col[0] + '55');
    for(let s=0;s<11;s++){
      const p=$('pad'+t+'_'+s), on=tr.sel[s];
      p.style.background = on ? col[0] : col[1];
      p.style.borderColor = on ? col[0] : (col[0] + '66');
      p.querySelector('.lbl').textContent = names[s];
      p.querySelector('.lbl').style.color = on ? '#0b0c0f' : '#c9d2de';
      const v=p.querySelector('.val');
      v.textContent = tr.shown[S.slots[s]];
      if(s===6) v.style.fontSize='7px';
      v.style.color = on ? '#0b0c0fbb' : '#8f9aa8';
    }
    const sl=$('slot'+t);
    sl.className='slot'+(tr.path?(tr.loaded?' loaded':''):' empty');
    sl.innerHTML = '<div class="nm">'+(tr.path? esc(tr.name) : 'empty — click to load')+'</div>'+
      '<div class="meta">'+(tr.path ? (tr.loaded ? (tr.dur.toFixed(2)+'s · '+tr.channels+'ch') : 'loading…') : 'track '+(t+1))+'</div>';
  }
  // step bar = the focused track's playhead, in that track's colour
  const f = Math.max(0,Math.min(7,S.focus)), fc = S.colors[f];
  const seg = Math.floor(Math.min(0.99999, S.tracks[f].head)*16);
  for(let i=0;i<16;i++){
    const d=$('st'+i);
    d.style.background = i<=seg ? fc[0] : fc[1];
    d.style.borderColor = i<=seg ? fc[0] : (fc[0] + '55');
  }
  for(let i=0;i<32;i++){
    const p=S.projects[i], d=$('pj'+i);
    d.className='pj'+(p.filled?' filled':'')+(S.projCur===i?' cur':'');
    d.querySelector('.t').textContent = p.filled ? p.meta.name : '';
  }
  $('fx-n').textContent = '· ' + (S.fxCount||0) + ' airwindows effects · rack '
    + (S.fxCost||0).toFixed(1) + '% / ' + (S.fxBudget||0) + '% cpu budget';
  for(let t=0;t<8;t++){
    const on=(S.fxTracks||[]).indexOf(t)>=0, col=S.colors[t], d=$('fxt'+t);
    d.style.background = on ? col[0] : col[1];
    d.style.borderColor = on ? col[0] : (col[0]+'66');
    d.style.color = on ? '#0b0c0f' : '#c9d2de';
  }
  for(let i=0;i<4;i++){
    const d=$('fxs'+i), on=(S.fxSlots||[])[i], lk=(S.fxLocked||[])[i];
    d.className = 'fxs' + (on?' on':'') + (lk?' lk':'');
    const links=(S.fxChains||[])[i]||[];
    d.querySelector('.lock').textContent = lk ? '\u{1F512}' : '\u{1F513}';
    d.querySelector('.lock').title = lk ? 'locked — click to free' : 'lock this chain';
    d.querySelector('.wet').value = (S.fxWet||[])[i] != null ? S.fxWet[i] : 0.4;
    d.querySelector('.c').textContent = links.length
      ? links.map(l=>l.name+(l.mod?'~':'')).join(' > ')
      : (lk ? 'locked — roll one to pin it' : 'empty — click to roll');
  }
  $('fxsummary').textContent = (S.fxLive ? 'Signal order: ' : 'SILENT — no track routed · ')
    + (S.fxSummary || 'no chain');
  $('fxsummary').style.color = S.fxLive ? 'var(--dim)' : '#c88';
  $('pjhint').textContent = saveArm ? 'Pick a slot to SAVE into…'
    : 'Click a slot to load · ' + (S.projCur>=0 ? ('project '+(S.projCur+1)+' loaded') : 'nothing loaded');
  paintHarvest(S.harvest);
}
function esc(s){ return (s||'').replace(/[<>&]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])); }

/* ---- projects ---- */
function saveInto(){ saveArm = !saveArm; paint(); }
async function clickSlot(i){
  if(saveArm){
    const nm = $('pjname').value.trim();
    const r = await api('/api/project/save',{slot:i, name: nm || undefined});
    saveArm=false; toast(r.ok?('Saved to slot '+(i+1)):'Save failed');
  } else {
    if(!S.projects[i].filled){ toast('Slot '+(i+1)+' is empty'); return; }
    const r = await api('/api/project/load',{slot:i});
    toast(r.ok?('Loaded slot '+(i+1)):'Load failed');
  }
  refresh();
}

/* ---- sample browser ---- */
async function openModal(t){
  target=t; $('m-title').textContent='Track '+(t+1)+' — choose a sample';
  $('modal').classList.add('on'); $('q').value='';
  const r = await api('/api/samples'); samples = r.samples||[]; renderList(); $('q').focus();
}
function closeModal(){ $('modal').classList.remove('on'); }
function renderList(){
  const q=$('q').value.toLowerCase(), L=$('list'); L.innerHTML='';
  const clr=document.createElement('div'); clr.className='item';
  clr.innerHTML='<span style="color:var(--dim)">— clear this track —</span>';
  clr.onclick=()=>pick(null); L.appendChild(clr);
  let n=0;
  for(const s of samples){
    if(q && s.name.toLowerCase().indexOf(q)<0 && s.dir.toLowerCase().indexOf(q)<0) continue;
    if(++n>400) break;
    const d=document.createElement('div'); d.className='item';
    d.innerHTML='<span>'+esc(s.name)+'</span><span class="p">'+esc(s.dir)+'</span>';
    d.onclick=()=>pick(s.path); L.appendChild(d);
  }
  if(!n){ const d=document.createElement('div'); d.className='item'; d.textContent='no matches'; L.appendChild(d); }
}
async function pick(path){
  const r = await api('/api/track/sample',{track:target, path:path});
  toast(r.ok ? (path?'Loading…':'Cleared') : 'Failed'); closeModal(); refresh();
}

/* ---- upload ---- */
const drop=$('drop');
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hot');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hot');}));
drop.addEventListener('drop', async ev=>{
  const f = ev.dataTransfer.files[0]; if(!f) return;
  await upload(f);
});
drop.addEventListener('click', ()=>{
  const inp=document.createElement('input'); inp.type='file';
  inp.onchange=()=>{ if(inp.files[0]) upload(inp.files[0]); }; inp.click();
});
async function upload(f){
  toast('Uploading '+f.name+'…');
  const r = await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});
  let j={}; try{ j=await r.json(); }catch(e){}
  if(j.ok){ toast('Uploaded'); const s=await api('/api/samples'); samples=s.samples||[];
            if(target>=0) pick(j.path); else renderList(); }
  else toast(j.error||'Upload failed');
}
/* ---- harvester ---- */
let durTouched = 0;
async function setDur(){
  const lo = parseFloat(document.getElementById('hv-min').value);
  const hi = parseFloat(document.getElementById('hv-max').value);
  durTouched = Date.now();            /* stop the poll overwriting the field mid-edit */
  const r = await api('/api/harvest/duration', {min: lo, max: hi});
  if(r && r.ok){
    document.getElementById('hv-min').value = r.min;
    document.getElementById('hv-max').value = r.max;
    toast('Excerpt length ' + r.min + '–' + r.max + ' s');
  }
}
async function harvest(){
  const n = parseInt(document.getElementById('hv-count').value) || 8;
  const r = await api('/api/harvest', {count: n});
  toast(r && r.ok ? 'Harvesting ' + n + '…' : 'Harvest already running');
  refresh();
}
function paintHarvest(h){
  if(!h) return;
  if(Date.now() - durTouched > 2000){
    if(h.durMin != null) document.getElementById('hv-min').value = h.durMin;
    if(h.durMax != null) document.getElementById('hv-max').value = h.durMax;
  }
  const stage = document.getElementById('hv-stage');
  stage.textContent = h.running ? h.stage + ' — ' + h.note.substring(0,40)
                    : (h.stage === 'failed' ? 'failed: ' + h.note : '');
  document.getElementById('hv-fill').style.width = (h.running ? h.progress*100 : 0) + '%';
  document.getElementById('hv-go').disabled = !!h.running;
  const list = document.getElementById('hv-list');
  const sig = JSON.stringify(h.samples || []);
  if(list.dataset.sig === sig) return;
  list.dataset.sig = sig;
  list.innerHTML = '';
  (h.samples || []).forEach((x, i) => {
    const d = document.createElement('div');
    d.className = 'hvchip';
    d.innerHTML = '<b>S' + (i+1) + '</b> ' + esc(x.name) +
                  ' <span>' + (x.seconds||0).toFixed(1) + 's</span>';
    list.appendChild(d);
  });
}

async function transport(){ await api('/api/transport',{}); refresh(); }
async function rescan(){ const r=await api('/api/rescan',{}); toast(r.count+' samples'); refresh(); }

/* ---- poll ---- */
async function refresh(){
  const next = await api('/api/state');
  if(next && Array.isArray(next.tracks)){ S = next; stale = false; }
  else { stale = true; }          /* keep the last good state rather than blanking */
  paint();
}
build(); refresh(); setInterval(refresh, 700);
</script></body></html>
"""
