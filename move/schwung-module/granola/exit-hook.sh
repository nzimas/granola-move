#!/bin/sh
# Granola overtake exit cleanup — called by the Schwung shim on clean exit.
sh /data/UserData/granola/stop-stack.sh 2>/dev/null
