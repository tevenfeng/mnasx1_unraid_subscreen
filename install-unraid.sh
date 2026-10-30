#!/bin/sh
# install-unraid.sh - install the HarborOS front-panel screen daemon on Unraid.
#
#   ./install-unraid.sh              install files, rc script and boot hook
#   ./install-unraid.sh uninstall    stop and remove rc script + boot hook
#   ./install-unraid.sh uninstall --purge
#                                    also delete /boot/config/plugins/subscreen
#
# The script is idempotent and only touches:
#   /boot/config/plugins/subscreen/   (daemon files)
#   /etc/rc.d/rc.subscreen            (start/stop helper)
#   /boot/config/go                   (one appended startup line)
#   /var/run/subscreen.pid, /var/log/subscreen.log (created by the service)

set -eu

PLUGIN_DIR="/boot/config/plugins/subscreen"
# The rc script must live on /boot: /etc/rc.d is tmpfs and is recreated on
# every Unraid boot, so a script written only there would be gone by the time
# /boot/config/go runs.
RC_PERSIST="$PLUGIN_DIR/rc.subscreen"
RC_SCRIPT="/etc/rc.d/rc.subscreen"
GO_FILE="/boot/config/go"
GO_LINE="sh $RC_PERSIST start"
OLD_GO_LINE="/etc/rc.d/rc.subscreen start"
PURGE=0

usage() {
    cat <<'EOF'
Usage: install-unraid.sh [install|uninstall] [--purge]

  install              copy the daemon files, install /etc/rc.d/rc.subscreen,
                       add the startup line to /boot/config/go and start it
  uninstall            stop the daemon and remove the rc script and boot hook
  uninstall --purge    also remove /boot/config/plugins/subscreen
EOF
}

COMMAND="install"
for arg in "$@"; do
    case "$arg" in
        install|uninstall)
            COMMAND="$arg"
            ;;
        --purge)
            PURGE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "subscreen: unknown argument: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "subscreen: must be run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

copy_files() {
    for name in subscreen_protocol.py subscreen_daemon.py; do
        src="$SCRIPT_DIR/$name"
        dst="$PLUGIN_DIR/$name"
        if [ ! -f "$src" ]; then
            echo "subscreen: error: missing $src" >&2
            exit 1
        fi
        if [ "$src" = "$dst" ]; then
            echo "subscreen: $dst already in place"
        else
            cp -f "$src" "$dst"
            echo "subscreen: copied $name to $PLUGIN_DIR"
        fi
        chmod 0644 "$dst" 2>/dev/null || true
    done
}

write_rc_script() {
    # /etc/rc.d is a convenience copy for manual use; the persistent script
    # under /boot is what actually survives a reboot. Do not fail if /etc is
    # read-only (e.g. when testing the installer in a sandbox).
    mkdir -p /etc/rc.d 2>/dev/null || true
    cat > "$RC_PERSIST" <<'RC_EOF'
#!/bin/sh
# rc.subscreen - manage the HarborOS front panel daemon.
DAEMON="/boot/config/plugins/subscreen/subscreen_daemon.py"
PIDFILE="/var/run/subscreen.pid"
LOGFILE="/var/log/subscreen.log"

is_running() {
    [ -f "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null || return 1
    # Guard against a recycled pid from an earlier crash.
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "subscreen_daemon.py" || return 1
    fi
    return 0
}

start() {
    if is_running; then
        echo "subscreen: already running (pid $(cat "$PIDFILE"))"
        return 0
    fi
    rm -f "$PIDFILE"
    if [ ! -f "$DAEMON" ]; then
        echo "subscreen: error: missing $DAEMON" >&2
        return 1
    fi
    nohup python3 /boot/config/plugins/subscreen/subscreen_daemon.py run >>"$LOGFILE" 2>&1 &
    pid=$!
    echo "$pid" > "$PIDFILE"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "subscreen: started (pid $pid)"
        return 0
    fi
    rm -f "$PIDFILE"
    echo "subscreen: failed to start; see $LOGFILE" >&2
    return 1
}

stop() {
    if ! is_running; then
        rm -f "$PIDFILE"
        echo "subscreen: not running"
        return 0
    fi
    pid=$(cat "$PIDFILE")
    kill -TERM "$pid" 2>/dev/null || true
    i=0
    while [ "$i" -lt 10 ] && kill -0 "$pid" 2>/dev/null; do
        sleep 1
        i=$((i + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
        echo "subscreen: killed (pid $pid)"
    else
        echo "subscreen: stopped (pid $pid)"
    fi
    rm -f "$PIDFILE"
    return 0
}

status() {
    if is_running; then
        echo "subscreen: running (pid $(cat "$PIDFILE"))"
        return 0
    fi
    echo "subscreen: not running"
    return 1
}

case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        start
        ;;
    status)
        status
        ;;
    *)
        echo "usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac
RC_EOF
    chmod 0755 "$RC_PERSIST" 2>/dev/null || true
    # Also expose it under /etc/rc.d for manual use during this boot. The
    # persistent copy above is what /boot/config/go invokes after a reboot.
    cp -f "$RC_PERSIST" "$RC_SCRIPT" 2>/dev/null || true
    chmod 0755 "$RC_SCRIPT" 2>/dev/null || true
    echo "subscreen: installed $RC_PERSIST (and $RC_SCRIPT)"
}

add_go_hook() {
    mkdir -p "$(dirname "$GO_FILE")"
    [ -f "$GO_FILE" ] || : > "$GO_FILE"
    # Migrate/remove the old (non-persistent) hook if a previous install used it.
    if grep -qxF "$OLD_GO_LINE" "$GO_FILE" 2>/dev/null; then
        tmp=$(mktemp /tmp/subscreen-go.XXXXXX)
        grep -vxF "$OLD_GO_LINE" "$GO_FILE" > "$tmp" || true
        cat "$tmp" > "$GO_FILE"
        rm -f "$tmp"
        echo "subscreen: removed obsolete startup line from $GO_FILE"
    fi
    if grep -qxF "$GO_LINE" "$GO_FILE" 2>/dev/null; then
        echo "subscreen: startup line already present in $GO_FILE"
        return 0
    fi
    # Make sure the existing last line is terminated before appending.
    if [ -s "$GO_FILE" ] && [ "$(tail -c 1 "$GO_FILE")" != "" ]; then
        printf '\n' >> "$GO_FILE"
    fi
    printf '%s\n' "$GO_LINE" >> "$GO_FILE"
    echo "subscreen: added startup line to $GO_FILE"
}

remove_go_hook() {
    if [ ! -f "$GO_FILE" ]; then
        echo "subscreen: startup line not present in $GO_FILE"
        return 0
    fi
    if ! grep -qxF "$GO_LINE" "$GO_FILE" 2>/dev/null \
       && ! grep -qxF "$OLD_GO_LINE" "$GO_FILE" 2>/dev/null; then
        echo "subscreen: startup line not present in $GO_FILE"
        return 0
    fi
    tmp=$(mktemp /tmp/subscreen-go.XXXXXX)
    # Keep every other line (including duplicates of unrelated lines).
    grep -vxF "$GO_LINE" "$GO_FILE" | grep -vxF "$OLD_GO_LINE" > "$tmp" || true
    cat "$tmp" > "$GO_FILE"
    rm -f "$tmp"
    echo "subscreen: removed startup line from $GO_FILE"
}

install_service() {
    mkdir -p "$PLUGIN_DIR"
    copy_files
    write_rc_script
    add_go_hook
    sh "$RC_PERSIST" start
    echo "subscreen: install complete"
}

uninstall_service() {
    if [ -f "$RC_PERSIST" ]; then
        sh "$RC_PERSIST" stop || true
    fi
    rm -f "$RC_SCRIPT" "$RC_PERSIST"
    echo "subscreen: removed $RC_SCRIPT and $RC_PERSIST"
    remove_go_hook
    if [ "$PURGE" -eq 1 ]; then
        rm -rf "$PLUGIN_DIR"
        echo "subscreen: purged $PLUGIN_DIR"
    else
        echo "subscreen: kept $PLUGIN_DIR (use --purge to remove)"
    fi
    echo "subscreen: uninstall complete"
}

if [ "$COMMAND" = "uninstall" ]; then
    uninstall_service
else
    install_service
fi
