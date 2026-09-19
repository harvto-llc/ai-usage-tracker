#!/bin/bash
# Smoke-test a macOS build of the ai-cur desktop client: a .dmg, a .zip or a .app.
#
#   scripts/smoke_macos.sh dist/ai-cur-desktop-0.2.0.dmg
#
# Copies the app out of the dmg/zip, launches it headless (AICUR_SMOKE=1) with a throwaway
# HOME, then asserts within TIMEOUT seconds that
#   1. GET 127.0.0.1:$PORT/health answers, and
#   2. the collector wrote at least one row to the bundled database
#      (provider_metric_samples, written by /cc/report on every collector cycle),
# then quits the app with SIGTERM and asserts no process from the copied app survives, then
# launches it again, force-quits it with SIGKILL and asserts the same (the supervisor notices
# its parent is gone and stops the API and the collector).
#
# PORT (default 18765, deliberately not 8000) is written into the throwaway config as
# USAGE_TRACKER_PORT, so the test never collides with a source install on 8000.
#
# Exit codes are distinct so CI can require the RIGHT failure from the negative control:
#   0 pass   2 setup error (bad input, port busy, app did not start)
#   3 backend /health never answered   4 no collector row   5 processes survived quit
set -euo pipefail

INPUT=${1:?usage: smoke_macos.sh <app.dmg|app.zip|App.app>}
PORT=${PORT:-18765}
TIMEOUT=${TIMEOUT:-30}
QUIT_TIMEOUT=${QUIT_TIMEOUT:-15}
# LAUNCH_ARCH=x86_64 runs the universal app under Rosetta, so it picks backend/x86_64.
LAUNCH_ARCH=${LAUNCH_ARCH:-}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/aicur-smoke.XXXXXX")
# Canonical path (no "//" from a trailing-slash TMPDIR, /var -> /private/var).
WORK=$(cd "$WORK" && pwd -P)
# Match processes on the unique work-dir NAME, not the full path: the same file shows up as
# /private/var/... (pwd -P) in some argv and /var/... in others (Foundation standardises the
# bundle path, dropping /private), and CI run 35425372263 counted 3 of 4 processes because of it.
MARK="${WORK##*/}/Applications/"
MOUNT=""
APP_PID=""

fail() { local code=$1; shift; printf 'SMOKE FAIL (%s): %s\n' "$code" "$*" >&2; exit "$code"; }

cleanup() {
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then kill -KILL "$APP_PID" 2>/dev/null || true; fi
  pgrep -f "$MARK" 2>/dev/null | xargs kill -KILL 2>/dev/null || true
  # Leave the port free for whatever runs next.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
  if [ -n "$MOUNT" ]; then hdiutil detach -quiet "$MOUNT" 2>/dev/null || true; fi
  if [ -d "$WORK/home/.usage-tracker/logs" ]; then
    for log in "$WORK"/home/.usage-tracker/logs/*.log; do
      [ -f "$log" ] || continue
      printf '\n--- %s (tail) ---\n' "${log##*/}" >&2
      tail -n 40 "$log" >&2 || true
    done
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

[ -e "$INPUT" ] || fail 2 "no such file: $INPUT"
mkdir -p "$WORK/Applications" "$WORK/home"

case "$INPUT" in
  *.dmg)
    MOUNT="$WORK/mnt"
    mkdir -p "$MOUNT"
    hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$INPUT" >/dev/null
    SRC_APP=$(find "$MOUNT" -maxdepth 1 -name '*.app' -print -quit)
    [ -n "$SRC_APP" ] || fail 2 "no .app at the top of $INPUT"
    [ -L "$MOUNT/Applications" ] || fail 2 "dmg has no Applications symlink"
    ditto "$SRC_APP" "$WORK/Applications/${SRC_APP##*/}"
    ;;
  *.zip)
    ditto -x -k "$INPUT" "$WORK/Applications"
    ;;
  *.app)
    ditto "$INPUT" "$WORK/Applications/${INPUT##*/}"
    ;;
  *) fail 2 "expected .dmg, .zip or .app: $INPUT" ;;
esac

APP=$(find "$WORK/Applications" -maxdepth 1 -name '*.app' -print -quit)
[ -n "$APP" ] || fail 2 "no .app after extracting $INPUT"
EXE_NAME=$(plutil -extract CFBundleExecutable raw "$APP/Contents/Info.plist")
EXE="$APP/Contents/MacOS/$EXE_NAME"
[ -x "$EXE" ] || fail 2 "not executable: $EXE"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  fail 2 "port $PORT already has a listener; stop it first so the test measures this app"
fi

export HOME="$WORK/home"
mkdir -p "$HOME/.usage-tracker"
printf 'USAGE_TRACKER_PORT=%s\n' "$PORT" > "$HOME/.usage-tracker/config"
chmod 600 "$HOME/.usage-tracker/config"

launch() {
  if [ -n "$LAUNCH_ARCH" ]; then
    AICUR_SMOKE=1 arch "-$LAUNCH_ARCH" "$EXE" >>"$WORK/app.out" 2>&1 &
  else
    AICUR_SMOKE=1 "$EXE" >>"$WORK/app.out" 2>&1 &
  fi
  APP_PID=$!
  sleep 1
  kill -0 "$APP_PID" 2>/dev/null || { cat "$WORK/app.out" >&2; fail 2 "app exited immediately"; }
}

wait_health() {
  local deadline=$((SECONDS + TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ok"' && return 0
    sleep 1
  done
  return 1
}

# Asserts every process started from the copied app is gone and the port is closed.
assert_all_gone() {
  local how=$1 deadline=$((SECONDS + QUIT_TIMEOUT)) left
  while [ $SECONDS -lt $deadline ] && pgrep -f "$MARK" >/dev/null; do sleep 1; done
  left=$(pgrep -f "$MARK" | tr '\n' ' ' || true)
  [ -z "$left" ] || fail 5 "processes survived $how after ${QUIT_TIMEOUT}s: $left"
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail 5 "port $PORT still listening after $how"; fi
  printf '%s: app, supervisor, api and collector all gone\n' "$how"
}

launch

DB="$HOME/.usage-tracker/claude_usage.db"
DEADLINE=$((SECONDS + TIMEOUT))
HEALTH=0
ROWS=0
while [ $SECONDS -lt $DEADLINE ]; do
  if [ $HEALTH -eq 0 ] && curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ok"'; then
    HEALTH=1
    printf 'health answered after %ss\n' "$SECONDS"
  fi
  if [ $HEALTH -eq 1 ] && [ -f "$DB" ]; then
    # Plain open, SELECT only: a read-only (mode=ro) open of the live WAL database fails with
    # "unable to open database file (14)" while the API holds it.
    ROWS=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM provider_metric_samples' 2>/dev/null || echo 0)
    if [ "${ROWS:-0}" -ge 1 ]; then
      printf 'collector wrote %s provider_metric_samples row(s) after %ss\n' "$ROWS" "$SECONDS"
      break
    fi
  fi
  sleep 1
done
[ $HEALTH -eq 1 ] || fail 3 "GET 127.0.0.1:$PORT/health did not answer within ${TIMEOUT}s (no backend)"
[ "${ROWS:-0}" -ge 1 ] || fail 4 "collector wrote no provider_metric_samples row within ${TIMEOUT}s"

# Every process started from the copied app: the app, the supervisor, the API, the collector.
BEFORE=$(pgrep -f "$MARK" | tr '\n' ' ' || true)
printf 'processes from the app before quit: %s\n' "$BEFORE"
[ "$(echo "$BEFORE" | wc -w)" -ge 4 ] || fail 2 "expected app + supervisor + api + collector, saw: $BEFORE"
if [ -n "$LAUNCH_ARCH" ]; then
  pgrep -fl "$MARK.*/backend/$LAUNCH_ARCH/" >/dev/null \
    || fail 2 "LAUNCH_ARCH=$LAUNCH_ARCH but no backend/$LAUNCH_ARCH process is running"
fi

kill -TERM "$APP_PID"
assert_all_gone "quit (SIGTERM)"

# Force-quit: no applicationWillTerminate, no signal handler. Only the supervisor's parent
# watch (and the children's own watch on the supervisor) can clean up.
launch
wait_health || fail 3 "relaunch: /health did not answer within ${TIMEOUT}s"
[ "$(pgrep -f "$MARK" | wc -l)" -ge 4 ] || fail 2 "relaunch: backend processes missing"
kill -KILL "$APP_PID"
assert_all_gone "force quit (SIGKILL)"
APP_PID=""

printf 'SMOKE PASS: %s\n' "$INPUT"
