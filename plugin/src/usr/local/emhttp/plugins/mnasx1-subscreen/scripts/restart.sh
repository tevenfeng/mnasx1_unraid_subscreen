#!/bin/bash
# Restart the daemon so it picks up a new settings file.

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
"$DIR/stop.sh" >/dev/null 2>&1
sleep 1
exec "$DIR/start.sh"
