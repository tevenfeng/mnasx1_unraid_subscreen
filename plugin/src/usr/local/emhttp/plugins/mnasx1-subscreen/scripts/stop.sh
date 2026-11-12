#!/bin/bash
# Stop the panel daemon. A no-op when it is not running.

. /usr/local/emhttp/plugins/mnasx1-subscreen/scripts/common.sh

if ! is_running; then
    rm -f "$PIDFILE"
    echo "not running"
    exit 0
fi

pid=$(cat "$PIDFILE")
kill -TERM "$pid" 2>/dev/null

# The daemon closes the USB interface on SIGTERM; give it a moment, then insist.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null
fi

rm -f "$PIDFILE"
logger -t "$PLUGIN" "stopped (pid $pid)"
echo "stopped (pid $pid)"
