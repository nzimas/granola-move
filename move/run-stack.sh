#!/bin/sh
# Launch the full Granola stack (engine + headless controller) non-blocking.
# Called once by the overtake ui.js. Each sub-script daemonises its processes; we
# background the launchers so host_system_cmd returns immediately.
GR=/data/UserData/granola
LOGS=$GR/logs; mkdir -p "$LOGS"
# IPC dir for control/status/heartbeat. A real directory on /data — the Schwung host can
# only read files under /data/UserData, and reads through a tmpfs symlink hang the host.
if [ -L "$GR/ipc" ]; then rm -f "$GR/ipc"; fi
mkdir -p "$GR/ipc" "$GR/samples" "$GR/state"

# Only ONE takeover runs at a time, and the SC ports are SHARED with the siblings
# (57110 server, 57120 sclang). An unclean exit from another takeover leaves its engine
# running, which both MASKS our start-guard and HOLDS those ports — the stack then
# half-starts (controller up, engine dead) and Granola is silent. Clear any FOREIGN SC
# engine first. jackd is deliberately NOT touched: it is the shared shadow server.
for p in $(pgrep -f "bin/sclang" 2>/dev/null) $(pgrep -f "bin/scsynth" 2>/dev/null) \
         $(pgrep -f "bin/supernova" 2>/dev/null); do
    case "$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)" in
        *"$GR/sc"*) ;;                               # ours (gr-boot.scd) — leave alone
        "") ;;                                       # vanished
        *) echo "[stack] clearing foreign SC engine pid $p"; kill -9 "$p" 2>/dev/null ;;
    esac
done
for p in $(pgrep -f "\.headless" 2>/dev/null); do
    case "$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null)" in
        *granola.headless*) ;;                       # ours
        "") ;;
        *) echo "[stack] clearing foreign controller pid $p"; kill -9 "$p" 2>/dev/null ;;
    esac
done
rm -f /dev/shm/SuperColliderServer_* 2>/dev/null   # a stale shm segment breaks World_New

# A HALF-DEAD STACK IS WORSE THAN A DEAD ONE, and it is the state a relaunch cannot
# escape: the server can die on its own (JACK drops a client that misses its deadline)
# while sclang and the controller survive, and a guard that only asks whether OUR sclang
# exists then skips the engine start forever. An sclang with no server under it is not a
# running engine, it is wreckage — clear it.
if pgrep -f "$GR/sc/gr-boot.scd" >/dev/null 2>&1 \
   && ! pgrep -x supernova >/dev/null 2>&1 && ! pgrep -x scsynth >/dev/null 2>&1; then
    echo "[stack] sclang is up but the server is gone — tearing the stack down first"
    sh "$GR/stop-stack.sh" >/dev/null 2>&1
    sleep 2
fi

if ! pgrep -f "$GR/sc/gr-boot.scd" >/dev/null 2>&1; then
    setsid nohup sh "$GR/run-engine.sh" > "$LOGS/stack_engine.log" 2>&1 &
fi

# Suspend-detection flag (mirrors RNBO): mark that shadow JACK is up.
echo 1 > /data/UserData/schwung/jack_running 2>/dev/null

# Controller: starts in parallel — it pings the engine until ready.
if ! pgrep -f granola.headless >/dev/null 2>&1; then
    setsid nohup sh "$GR/run-controller.sh" > "$LOGS/controller.log" 2>&1 &
fi
