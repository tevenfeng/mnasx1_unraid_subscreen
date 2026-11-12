#!/bin/bash
# Start the panel daemon. Safe to call repeatedly: it is a no-op when the
# daemon is already running. Called by event/started and by the settings page.

. /usr/local/emhttp/plugins/mnasx1-subscreen/scripts/common.sh

mkdir -p "$CONFIG_DIR"

if is_running; then
    echo "already running (pid $(cat "$PIDFILE"))"
    exit 0
fi

if ! python_ok; then
    logger -t "$PLUGIN" "python3 (>= 3.7) not found - daemon not started"
    echo "python3 (>= 3.7) not found; install NerdTools or a Python 3 plugin" >&2
    exit 1
fi

if [ ! -f "$APP_DIR/subscreen_daemon.py" ]; then
    logger -t "$PLUGIN" "missing $APP_DIR/subscreen_daemon.py"
    echo "missing $APP_DIR/subscreen_daemon.py" >&2
    exit 1
fi

rotate_log

PYTHON=$(find_python)
# exec-like: nohup detaches so emhttp_event is never blocked, and the pid we
# record is the interpreter's own.
nohup "$PYTHON" "$APP_DIR/subscreen_daemon.py" run --config "$CONFIG" \
    >>"$LOG" 2>&1 &
pid=$!
echo "$pid" > "$PIDFILE"

sleep 1
if kill -0 "$pid" 2>/dev/null; then
    logger -t "$PLUGIN" "started (pid $pid)"
    echo "started (pid $pid)"
    exit 0
fi

rm -f "$PIDFILE"
logger -t "$PLUGIN" "failed to start; see $LOG"
echo "failed to start; see $LOG" >&2
exit 1
