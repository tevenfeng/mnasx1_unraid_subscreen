#!/bin/bash
# Push a single frame now, without disturbing the running daemon.
#
# The daemon holds the USB interface, so this only works while it is stopped;
# when it is running the request is refused rather than fighting over the port.

DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$DIR/common.sh"

if is_running; then
    echo "daemon is running and owns the USB device; stop it first" >&2
    exit 1
fi
if ! python_ok; then
    echo "python3 (>= 3.7) not found" >&2
    exit 1
fi

PYTHON=$(find_python)
exec "$PYTHON" "$APP_DIR/subscreen_daemon.py" once --config "$CONFIG"
