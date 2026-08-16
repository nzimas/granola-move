#!/bin/sh
# Tear the Granola stack down and release the shadow-JACK flag.
GR=/data/UserData/granola
pkill -9 -f granola.headless 2>/dev/null
killall -9 sclang    2>/dev/null
killall -9 scsynth   2>/dev/null
killall -9 supernova 2>/dev/null
killall -9 jackd     2>/dev/null
# The server's shared-memory segment must go too, or the next launch (as the same user)
# can fail in World_New if a stale one is present.
rm -f /dev/shm/SuperColliderServer_* 2>/dev/null
rm -f /data/UserData/schwung/jack_running
# Drop the hand-off files so a stale grid can't flash on relaunch.
rm -f "$GR"/ipc/*.json "$GR"/ipc/ui_hb.txt 2>/dev/null
