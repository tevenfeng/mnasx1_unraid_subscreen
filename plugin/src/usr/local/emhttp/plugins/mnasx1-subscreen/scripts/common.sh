#!/bin/bash
# Shared settings for every script in this plugin.
#
# Everything the daemon needs lives in two places:
#   /boot/config/plugins/mnasx1-subscreen/   persistent config (survives reboot)
#   /usr/local/lib/mnasx1-subscreen/         the program itself (RAM, reinstalled
#                                            from the .txz on every boot)

PLUGIN="mnasx1-subscreen"
# Every path can be overridden so the scripts can be exercised off-box with a
# stub daemon (see tests/test_scripts.sh); production never sets these.
PLUGIN_DIR="${SUBSYS_PLUGIN_DIR:-/usr/local/emhttp/plugins/$PLUGIN}"
APP_DIR="${SUBSYS_APP_DIR:-/usr/local/lib/$PLUGIN}"
CONFIG_DIR="${SUBSYS_CONFIG_DIR:-/boot/config/plugins/$PLUGIN}"
CONFIG="${SUBSYS_CONFIG:-$CONFIG_DIR/$PLUGIN.cfg}"
LOG="${SUBSYS_LOG:-/var/log/$PLUGIN.log}"
PIDFILE="${SUBSYS_PIDFILE:-/var/run/$PLUGIN.pid}"
LOG_MAX_BYTES=$((5 * 1024 * 1024))

# The daemon needs Python 3.7+. Unraid's base system ships none, so this is a
# real prerequisite; the settings page shows a hint when it is missing.
find_python() {
    # An explicit override wins outright: silently falling back to a different
    # interpreter than the one the admin named would be worse than failing.
    if [ -n "${SUBSYS_PYTHON:-}" ]; then
        [ -x "$SUBSYS_PYTHON" ] && printf '%s\n' "$SUBSYS_PYTHON"
        return 0
    fi
    local candidate
    for candidate in /usr/bin/python3 /usr/local/bin/python3 /opt/bin/python3; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    command -v python3 2>/dev/null
}

python_ok() {
    local python
    python=$(find_python)
    [ -n "$python" ] || return 1
    "$python" -c 'import sys, dataclasses; sys.exit(0 if sys.version_info >= (3, 7) else 1)' \
        >/dev/null 2>&1
}

rotate_log() {
    [ -f "$LOG" ] || return 0
    local size
    size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
        mv -f "$LOG" "$LOG.1" 2>/dev/null || true
    fi
}

is_running() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
