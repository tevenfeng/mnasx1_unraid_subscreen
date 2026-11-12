#!/bin/bash
# Exercise the plugin's start/stop/restart scripts against a stub daemon.
#
# The point is not to test the panel: it is to prove these scripts always
# return quickly. They are invoked synchronously from a PHP request, so a hang
# here shows up as a dead settings page in the browser.
#
#   bash tests/test_scripts.sh

set -u

SRC=$(cd "$(dirname "$0")/../plugin/src/usr/local/emhttp/plugins/mnasx1-subscreen" && pwd)
SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

export SUBSYS_APP_DIR="$SANDBOX/app"
export SUBSYS_CONFIG_DIR="$SANDBOX/config"
export SUBSYS_LOG="$SANDBOX/daemon.log"
export SUBSYS_PIDFILE="$SANDBOX/daemon.pid"
export SUBSYS_PYTHON=$(command -v python3)

mkdir -p "$SUBSYS_APP_DIR" "$SUBSYS_CONFIG_DIR"
cat > "$SUBSYS_APP_DIR/subscreen_daemon.py" <<'PY'
import sys, time
command = sys.argv[1] if len(sys.argv) > 1 else "run"
if command == "run":
    time.sleep(600)
else:
    print("stub " + command)
PY

SCRIPTS="$SANDBOX/scripts"
mkdir -p "$SCRIPTS"
cp "$SRC"/scripts/*.sh "$SCRIPTS"/
chmod 755 "$SCRIPTS"/*.sh

failures=0
check() {
    local label=$1 expected=$2 actual=$3
    if [ "$expected" = "$actual" ]; then
        printf 'ok   %-42s %s\n' "$label" "$actual"
    else
        printf 'FAIL %-42s expected=%s actual=%s\n' "$label" "$expected" "$actual"
        failures=$((failures + 1))
    fi
}

# `timeout` turns a hang into a test failure instead of a stuck run.
run_bounded() {
    local limit=$1; shift
    timeout "$limit" "$@" 2>&1
}

echo "== start =="
out=$(run_bounded 10 "$SCRIPTS/start.sh"); rc=$?
check "start exits 0" 0 "$rc"
check "start reports started" 1 "$(echo "$out" | grep -c started)"
check "pidfile written" 1 "$([ -s "$SUBSYS_PIDFILE" ] && echo 1 || echo 0)"
pid=$(cat "$SUBSYS_PIDFILE" 2>/dev/null)
check "process alive" 1 "$(kill -0 "$pid" 2>/dev/null && echo 1 || echo 0)"

echo "== start again (idempotent) =="
out=$(run_bounded 10 "$SCRIPTS/start.sh"); rc=$?
check "second start exits 0" 0 "$rc"
check "second start is a no-op" 1 "$(echo "$out" | grep -c 'already running')"
check "same pid" "$pid" "$(cat "$SUBSYS_PIDFILE")"

echo "== stop =="
out=$(run_bounded 15 "$SCRIPTS/stop.sh"); rc=$?
check "stop exits 0" 0 "$rc"
check "process gone" 1 "$(kill -0 "$pid" 2>/dev/null && echo 0 || echo 1)"
check "pidfile removed" 0 "$([ -f "$SUBSYS_PIDFILE" ] && echo 1 || echo 0)"

echo "== stop when already stopped =="
out=$(run_bounded 10 "$SCRIPTS/stop.sh"); rc=$?
check "stop idle exits 0" 0 "$rc"
check "stop idle says not running" 1 "$(echo "$out" | grep -c 'not running')"

echo "== restart =="
start=$(date +%s)
out=$(run_bounded 25 "$SCRIPTS/restart.sh"); rc=$?
elapsed=$(( $(date +%s) - start ))
check "restart exits 0" 0 "$rc"
check "restart under 10s" 0 "$([ "$elapsed" -lt 10 ] && echo 0 || echo 1)"
pid=$(cat "$SUBSYS_PIDFILE" 2>/dev/null)
check "restart left a live process" 1 "$(kill -0 "$pid" 2>/dev/null && echo 1 || echo 0)"
run_bounded 15 "$SCRIPTS/stop.sh" >/dev/null

echo "== missing python =="
start=$(date +%s)
out=$(SUBSYS_PYTHON=/nonexistent/python3 PATH=/usr/bin:/bin \
      run_bounded 10 "$SCRIPTS/start.sh"); rc=$?
elapsed=$(( $(date +%s) - start ))
check "start without python fails fast" 0 "$([ "$elapsed" -lt 5 ] && echo 0 || echo 1)"
check "start without python is non-zero" 1 "$([ "$rc" -ne 0 ] && echo 1 || echo 0)"
check "message names python" 1 "$(echo "$out" | grep -ci python)"

echo
if [ "$failures" -eq 0 ]; then
    echo "test_scripts: PASS"
    exit 0
fi
echo "test_scripts: $failures failure(s)"
exit 1
