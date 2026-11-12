#!/bin/bash
# One-screen summary: used by the settings page and by hand.

. /usr/local/emhttp/plugins/mnasx1-subscreen/scripts/common.sh

if is_running; then
    echo "daemon: running (pid $(cat "$PIDFILE"))"
else
    echo "daemon: stopped"
fi

if python_ok; then
    echo "python: $(find_python) ($(find_python | xargs -r -n1 sh -c '"$0" --version 2>&1' | head -1))"
else
    echo "python: MISSING (needs >= 3.7)"
fi

if [ -f "$APP_DIR/VERSION" ]; then
    echo "version: $(cat "$APP_DIR/VERSION")"
fi

if [ -f "$CONFIG" ]; then
    echo "config: $CONFIG"
else
    echo "config: $CONFIG (defaults, file not created yet)"
fi
