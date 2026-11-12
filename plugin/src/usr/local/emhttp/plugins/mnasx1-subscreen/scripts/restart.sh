#!/bin/bash
# Restart the daemon so it picks up a new settings file.

DIR=/usr/local/emhttp/plugins/mnasx1-subscreen/scripts
"$DIR/stop.sh" >/dev/null 2>&1
sleep 1
exec "$DIR/start.sh"
