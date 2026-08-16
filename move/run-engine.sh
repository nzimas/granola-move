#!/bin/sh
# Bring up the Granola SC engine on the Move: shadow JACK + sclang(gr-boot.scd).
# Run on the device. Leaves jackd + sclang (+ the server) running in the background;
# the headless controller then drives it over OSC.
#
# Granola ships NO SuperCollider bundle. Every takeover on this box already carries the
# same ~200-plugin build, and Granola's only requirement beyond core SC is JPverb
# (sc3-plugins), which those bundles have. GR_SC_HOME is resolved by probing them.
set -e
GR=/data/UserData/granola
RNBO=/data/UserData/rnbo

# --- pick an SC bundle ------------------------------------------------------
# First match wins. $GR/bin is honoured first so a private bundle can be dropped in
# later without editing this script.
if [ -z "$GR_SC_HOME" ]; then
    for cand in "$GR" /data/UserData/poundhard /data/UserData/onemanshow \
                /data/UserData/wildrider /data/UserData/interwoven; do
        if [ -x "$cand/bin/sclang" ] && [ -d "$cand/plugins" ]; then GR_SC_HOME="$cand"; break; fi
    done
fi
if [ -z "$GR_SC_HOME" ]; then
    echo "[engine] FATAL: no SuperCollider bundle found on this device."
    echo "[engine] Install one of the sibling takeovers (e.g. PoundHard) or place a"
    echo "[engine] bundle at $GR/bin + $GR/plugins + $GR/share."
    exit 1
fi
export GR_SC_HOME GR_HOME="$GR"
echo "[engine] SC bundle: $GR_SC_HOME"
# JPverb is the one non-core UGen the engine needs. Say so loudly rather than booting
# into a graph whose reverb node silently fails to instantiate.
[ -f "$GR_SC_HOME/plugins/JPverb.so" ] || echo "[engine] WARNING: JPverb.so not in $GR_SC_HOME/plugins — the reverb will not build"

# The Schwung menu launches us with HOME unset; sclang then tries to mkdir
# /.local/share/SuperCollider (filesystem root) and fails -> Server.default is nil ->
# the engine never boots. Point HOME at an ableton-writable dir.
export HOME=/data/UserData
export LD_LIBRARY_PATH=$GR_SC_HOME/lib:$RNBO/lib
# The shadow driver comes from Schwung (a hard prerequisite); fall back to RNBO's copy.
JACK_DRIVER_DIR=/data/UserData/schwung/lib/jack
[ -d "$JACK_DRIVER_DIR" ] || JACK_DRIVER_DIR=$RNBO/lib/jack
export JACK_DRIVER_DIR
export JACK_NO_AUDIO_RESERVATION=1
export SC_JACK_DEFAULT_OUTPUTS=system          # server out -> shadow playback
export SC_PLUGIN_PATH=$GR_SC_HOME/plugins      # backup to gr-boot's ugenPluginsPath

# SERVER: scsynth, NOT supernova — and this is measured, not a preference.
#
# The JPverb_supernova.so shipped in the bundles on this device does not register its
# UGen: booting supernova and loading a SynthDef that uses JPverb gives
#   "Cannot load synth <name>: Unit generator JPverbRaw not installed"
# and the node is never created, while the identical test under scsynth builds the
# synth and runs it. Granola's master reverb IS JPverb, so supernova would cost the
# reverb outright — a change to the ported sound, not a performance trade.
#
# The load is well within one core: eight GrainBuf clouds, two send effects and a
# limiter. Set GR_THREADS=3 to force supernova if you ever want the extra cores and
# can accept a silent reverb bus.
export GR_THREADS="${GR_THREADS:-0}"           # 0 = scsynth, >0 = supernova (no JPverb)
export GR_SR=44100                             # the Move shadow rate
export GR_BLOCK=128                            # match the shadow JACK period
# Telemetry / handshake target = the local headless controller. 57141, NOT PoundHard's
# 57140: only one takeover runs at a time, but a stale sibling controller must not be
# able to eat our telemetry.
export CONTROLLER_HOST=127.0.0.1
export CONTROLLER_PORT="${CONTROLLER_PORT:-57141}"
export PATH=$GR_SC_HOME/bin:$PATH
LOGS=$GR/logs; mkdir -p "$LOGS"
JACKLOG=$LOGS/jackd.log; ENGLOG=$LOGS/engine.log

echo "[engine] starting jackd -R -d shadow (realtime)"
# -R -P70 puts jackd on SCHED_FIFO; libjack then promotes the server's audio callback
# thread too. Priority 70 stays BELOW the SPI/IRQ kernel threads (chrt 90/91) so the
# DAC/display path is never starved. Whichever jackd is already RUNNING wins — the
# shadow JACK is shared with the rest of the box.
JACKBIN=$GR_SC_HOME/bin/jackd
[ -x "$JACKBIN" ] || JACKBIN=$RNBO/bin/jackd
pgrep -f "jackd -R" >/dev/null 2>&1 || { "$JACKBIN" -R -P 70 -d shadow > "$JACKLOG" 2>&1 & sleep 2; }
grep -q "attached to shared memory" "$JACKLOG" 2>/dev/null && echo "[engine] shadow attached"

echo "[engine] starting sclang (gr-boot.scd) — pinned to cores 0-2"
# setsid: detach from whatever launched us. Under the Schwung host there is no tty and
# this changes nothing, but it is also how the stack is started from an ssh session
# during development — and there SIGHUP on logout would otherwise take the engine down
# a few seconds after it came up, which looks exactly like a boot failure.
setsid taskset 0x7 "$GR_SC_HOME/bin/sclang" -l "$GR_SC_HOME/share/sclang_conf.yaml" "$GR/sc/gr-boot.scd" \
    > "$ENGLOG" 2>&1 < /dev/null &
echo "[engine] sclang pid=$!  (log: $ENGLOG)"
echo "[engine] waiting for boot ..."
i=0
while [ $i -lt 60 ]; do
    grep -q "granola. engine ready\|SuperCollider 3 server ready" "$ENGLOG" 2>/dev/null && break
    grep -qi "ERROR\|FAILURE\|Exception" "$ENGLOG" 2>/dev/null && { echo "[engine] error:"; tail -n 20 "$ENGLOG"; exit 1; }
    i=$((i+1)); sleep 1
done
echo "[engine] --- log tail ---"; tail -n 12 "$ENGLOG"

# Core pinning: audio (server + jackd) on cores 1-2, sclang on core 0, core 3 left to
# the SPI/display driver. supernova is NOT pinned here — it self-pins its DSP threads
# one per core inside sclang's inherited 0x7 mask, which is already the layout we want.
for p in $(pgrep -f "$GR_SC_HOME/bin/scsynth") $(pgrep -f "jackd -R"); do taskset -pc 1-2 "$p" >/dev/null 2>&1; done
for p in $(pgrep -f "$GR_SC_HOME/bin/sclang"); do taskset -pc 0 "$p" >/dev/null 2>&1; done

for p in $(pgrep -f "jackd -R") $(pgrep -x scsynth) $(pgrep -x supernova); do
    echo "[engine] $(cat /proc/$p/comm 2>/dev/null) sched: $(chrt -p $p 2>/dev/null | tr '\n' ' ')"
done
