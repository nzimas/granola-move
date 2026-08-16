#!/bin/sh
# Launch the Granola headless controller on the Move.
# It polls ipc/control.json (written by the Schwung ui.js), owns the parameter model,
# and drives the SC engine over OSC. It starts happily before the engine and pings
# until the engine answers /gr/ready.
GR=/data/UserData/granola
export PYTHONPATH="$GR/controller:$GR/controller/vendor"
export SC_HOST=127.0.0.1
export SC_PORT=57120                       # sclang's OSC port (the engine's API)
export CONTROLLER_PORT="${CONTROLLER_PORT:-57141}"
export GR_IPC="$GR/ipc"
export GR_SAMPLES="$GR/samples"
export GR_STATE="$GR/state"
mkdir -p "$GR_IPC" "$GR_STATE" "$GR/logs"
# core 0, beside sclang — cores 1-2 belong to the audio thread.
# NOT setsid here: run-stack.sh already detaches this script, and calling setsid from a
# process that is already a session leader is exactly the case where it fails — which
# killed the controller a few seconds after it had logged a clean startup.
exec taskset 0x1 python3 -u -m granola.headless < /dev/null
