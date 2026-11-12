#!/bin/bash
# USBDEVFS_RESET the panel's port: the first aid when the screen stops
# responding to touches. Stop the daemon first, it holds the interface open.

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$DIR/common.sh"

if is_running; then
    echo "daemon is running; stop it before resetting the port" >&2
    exit 1
fi
if ! python_ok; then
    echo "python3 (>= 3.7) not found" >&2
    exit 1
fi

PYTHON=$(find_python)
exec "$PYTHON" "$APP_DIR/subscreen_daemon.py" reset
