"""OSC bridge: controller -> SC engine (/gr/...) and engine -> controller telemetry.

Sends are no-ops if the client can't be built, so the controller runs headless (no
engine) for development. Liveness is a heartbeat: `connected` is true while telemetry
arrives within a timeout, and `ready` additionally requires the /gr/ready handshake.
"""
from __future__ import annotations

import threading
import time
import traceback

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient


class EngineBridge:
    def __init__(self, sc_host: str, sc_port: int,
                 listen_host: str = "127.0.0.1", listen_port: int = 57141,
                 n_tracks: int = 8, heartbeat_timeout: float = 5.0) -> None:
        self.sc_host, self.sc_port = sc_host, sc_port
        self.listen_host, self.listen_port = listen_host, listen_port
        self.n_tracks = n_tracks
        self.heartbeat_timeout = heartbeat_timeout
        self._client: SimpleUDPClient | None = None
        self._server: BlockingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None
        self._last_beat = 0.0
        self._ready = False
        self._on_ready = None
        self.cpu = {"avg": 0.0, "peak": 0.0, "nodes": 0}
        self.heads = [0.0] * n_tracks
        self.meters = [0.0] * n_tracks
        self.master_amp = (0.0, 0.0)
        self._send_failed: set[str] = set()
        # Chain-wide FX settings, held here so every fx_add carries the same ones.
        self.fx_fade = 5.0
        self.fx_auto_gain = True
        self.on_loaded = None      # (track, duration, channels)
        self.on_loadfail = None    # (track,)
        # Harvest excerpts: tag -> result, filled by the engine's reply.
        self._excerpts: dict[int, dict | None] = {}
        self._excerpt_lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def start(self, on_ready=None) -> None:
        self._on_ready = on_ready
        try:
            self._client = SimpleUDPClient(self.sc_host, self.sc_port)
        except Exception:
            # A client that could not be built means every outbound message is dropped
            # for the life of the process — the engine boots, the UI looks alive and
            # nothing ever sounds. Say so.
            self._client = None
            print("[granola] FATAL: cannot open OSC client to %s:%d"
                  % (self.sc_host, self.sc_port), flush=True)
            traceback.print_exc()
        disp = Dispatcher()
        disp.map("/gr/ready", self._h_ready)
        disp.map("/gr/cpu", self._h_cpu)
        disp.map("/gr/head", self._h_head)
        disp.map("/gr/meter", self._h_meter)
        disp.map("/gr/master", self._h_master)
        disp.map("/gr/loaded", self._h_loaded)
        disp.map("/gr/loadfail", self._h_loadfail)
        disp.map("/gr/excerpted", self._h_excerpted)
        disp.map("/gr/excerptfail", self._h_excerptfail)
        try:
            # Blocking (single-threaded) server: the handlers are trivial, so we avoid
            # spawning a thread per incoming telemetry datagram (20Hz x 2).
            self._server = BlockingOSCUDPServer((self.listen_host, self.listen_port), disp)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        except Exception:
            self._server = None

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return (time.monotonic() - self._last_beat) < self.heartbeat_timeout

    @property
    def ready(self) -> bool:
        return self._ready and self.connected

    # -- inbound telemetry ------------------------------------------------- #
    # Handlers run on the single-threaded OSC server. A raising handler would kill that
    # thread and silently take out every playhead update, so they all stay guarded.
    def _beat(self) -> None:
        self._last_beat = time.monotonic()

    def _h_ready(self, _addr, *_a) -> None:
        self._beat()
        was = self._ready
        self._ready = True
        if not was and self._on_ready:
            try:
                self._on_ready()
            except Exception:
                traceback.print_exc()

    def _h_cpu(self, _addr, *a) -> None:
        self._beat()
        if len(a) >= 3:
            self.cpu = {"avg": float(a[0]), "peak": float(a[1]), "nodes": int(a[2])}

    def _h_head(self, _addr, *a) -> None:
        self._beat()
        for i in range(min(self.n_tracks, len(a))):
            try:
                self.heads[i] = float(a[i]) % 1.0
            except (TypeError, ValueError):
                pass

    def _h_meter(self, _addr, *a) -> None:
        for i in range(min(self.n_tracks, len(a))):
            try:
                self.meters[i] = float(a[i])
            except (TypeError, ValueError):
                pass

    def _h_master(self, _addr, *a) -> None:
        if len(a) >= 2:
            self.master_amp = (float(a[0]), float(a[1]))

    def _h_loaded(self, _addr, *a) -> None:
        self._beat()
        if len(a) >= 3 and self.on_loaded:
            try:
                self.on_loaded(int(a[0]), float(a[1]), int(a[2]))
            except Exception:
                traceback.print_exc()

    def _h_loadfail(self, _addr, *a) -> None:
        if a and self.on_loadfail:
            try:
                self.on_loadfail(int(a[0]))
            except Exception:
                traceback.print_exc()

    # -- outbound ---------------------------------------------------------- #
    def _send(self, addr: str, args: list) -> None:
        if self._client is None:
            if "**noclient**" not in self._send_failed:
                self._send_failed.add("**noclient**")
                print("[granola] OSC send dropped: no client", flush=True)
            return
        try:
            self._client.send_message(addr, args)
        except Exception:
            # Report the FIRST failure per address and then stay quiet. Swallowing these
            # silently makes a dead engine indistinguishable from a working one: every
            # gesture appears to be delivered and nothing ever sounds.
            if addr not in self._send_failed:
                self._send_failed.add(addr)
                print("[granola] OSC send failed for %s" % addr, flush=True)
                traceback.print_exc()

    def ping(self) -> None:
        self._send("/gr/ping", [])

    def load(self, track: int, path: str) -> None:
        self._send("/gr/load", [int(track), str(path)])

    def params(self, track: int, pairs) -> None:
        """One message per gesture: the macro encoder moves up to four values a tick."""
        flat: list = [int(track)]
        for key, value in pairs:
            flat.append(str(key))
            flat.append(float(value))
        if len(flat) > 1:
            self._send("/gr/param", flat)

    def free(self, track: int) -> None:
        """Silence a track and forget its buffer — a project whose slot is empty must
        leave that column silent rather than keeping the previous project's sample."""
        self._send("/gr/free", [int(track)])

    def mute(self, track: int, muted: bool) -> None:
        self._send("/gr/mute", [int(track), 1.0 if muted else 0.0])

    def run(self, running: bool) -> None:
        """Transport: every granulator starts and stops together."""
        self._send("/gr/run", [1 if running else 0])

    def route(self, track: int, through_fx: bool) -> None:
        # Crossfade a track into (or out of) the performance chain.
        self._send("/gr/route", [int(track), 1.0 if through_fx else 0.0])

    def jump(self, track: int, pos: float) -> None:
        self._send("/gr/jump", [int(track), float(pos)])

    def panic(self) -> None:
        self._send("/gr/panic", [])

    def audition(self, path: str) -> None:
        self._send("/gr/audition", [str(path)])

    def audition_off(self) -> None:
        self._send("/gr/auditionoff", [])

    def excerpt(self, src: str, dst: str, seconds: float, tag: int) -> None:
        self._send("/gr/excerpt", [str(src), str(dst), float(seconds), int(tag)])

    def excerpt_result(self, tag: int):
        with self._excerpt_lock:
            return self._excerpts.pop(tag, "pending")

    def _h_excerpted(self, _addr, *a) -> None:
        if len(a) >= 3:
            with self._excerpt_lock:
                self._excerpts[int(a[0])] = {"path": str(a[1]), "seconds": float(a[2]),
                                             "rms": float(a[3]) if len(a) > 3 else 0.0}

    def _h_excerptfail(self, _addr, *a) -> None:
        if a:
            with self._excerpt_lock:
                self._excerpts[int(a[0])] = None

    def fx_mix(self, node: int, mix: float) -> None:
        # One link's dry/wet blend.
        self._send("/gr/fxmix", [int(node), float(mix)])

    def fx_add(self, node: int, link, mix: float | None = None) -> None:
        # Instantiate one chain link. The engine inserts it before the terminator, so
        # the order links are added IS the signal order. `mix` overrides the link's own
        # blend, so a slot brought back up carries the wet level it was left at.
        args: list = [int(node), link.effect.synthdef,
                      float(link.mix if mix is None else mix), float(self.fx_fade),
                      1.0 if self.fx_auto_gain else 0.0,
                      float(link.lfo_target), float(link.lfo_rate),
                      float(link.lfo_depth), float(link.lfo_shape)]
        # Up to 10 parameters, named a..j by the generated SynthDefs.
        args += [float(v) for v in link.params[:10]]
        self._send("/gr/fxadd", args)

    def fx_free(self, node: int) -> None:
        self._send("/gr/fxfree", [int(node), float(self.fx_fade)])

    def fx_gain(self, on: bool) -> None:
        self._send("/gr/fxgain", [1.0 if on else 0.0])

    def master(self, amp: float) -> None:
        self._send("/gr/master", [float(amp)])

    def reverb(self, pairs) -> None:
        flat: list = []
        for key, value in pairs:
            flat += [str(key), float(value)]
        if flat:
            self._send("/gr/reverb", flat)

    def delay(self, pairs) -> None:
        flat: list = []
        for key, value in pairs:
            flat += [str(key), float(value)]
        if flat:
            self._send("/gr/delay", flat)

    def panic(self) -> None:
        self._send("/gr/panic", [])
