#!/bin/bash
# Smoke-test the Linux packages of the ai-cur desktop client on Ubuntu 22.04+ (CI: ubuntu-22.04).
#
#   scripts/smoke_linux.sh deb      dist/ai-cur-desktop_<v>_amd64.deb
#   scripts/smoke_linux.sh appimage dist/ai-cur-desktop-<v>-x86_64.AppImage
#
# Needs sudo (apt, loginctl) and a real systemd user manager for the current user; the script
# enables lingering to get one on a headless runner. Both modes assert:
#   installed: systemd user unit + XDG autostart entry exist and validate
#   running:   the tray's start path starts aicur-backend.service; /health answers on PORT and the
#              collector writes a provider_metric_samples row within TIMEOUT
#   GNOME:     with no StatusNotifier host (xvfb, no AppIndicator extension) the tray refuses to
#              run invisibly (exit 3) and prints the plain AppIndicator message
#   stopped:   systemctl --user stop leaves no backend process and a closed port
#   removed:   uninstall removes the unit, the autostart entry and the files
# Exit codes: 0 pass, 2 setup, 3 no /health, 4 no collector row, 5 processes survived,
#             6 install/uninstall state wrong, 8 GNOME no-tray-host message missing
set -euo pipefail

MODE=${1:?usage: smoke_linux.sh deb|appimage <package>}
PACKAGE=$(readlink -f "${2:?usage: smoke_linux.sh deb|appimage <package>}")
PORT=${PORT:-18765}
TIMEOUT=${TIMEOUT:-60}
UID_=$(id -u)

fail() {
  local code=$1; shift
  printf 'SMOKE FAIL (%s): %s\n' "$code" "$*" >&2
  dump_logs
  cleanup
  exit "$code"
}
# Leave the runner clean for whatever runs next (the negative control runs first).
cleanup() {
  systemctl --user stop aicur-backend.service >/dev/null 2>&1 || true
  if [ "$MODE" = deb ]; then
    sudo apt-get remove -y -qq ai-cur-desktop >/dev/null 2>&1 || true
  elif [ -x "$PACKAGE" ]; then
    "$PACKAGE" --uninstall >/dev/null 2>&1 || true
  fi
  systemctl --user reset-failed >/dev/null 2>&1 || true
}
dump_logs() {
  journalctl --user -u aicur-backend.service --no-pager -n 40 2>/dev/null >&2 || true
  for log in "$HOME"/.usage-tracker/logs/*.log; do
    [ -f "$log" ] || continue
    printf -- '--- %s (tail) ---\n' "${log##*/}" >&2
    tail -n 30 "$log" >&2 || true
  done
}
backend_pids() { pgrep -f '(/opt/ai-cur-desktop/backend|usr/lib/ai-cur-desktop/backend)/aicur-backend' || true; }

[ -f "$PACKAGE" ] || fail 2 "no such package: $PACKAGE"
if ss -ltn "sport = :$PORT" | grep -q LISTEN; then fail 2 "port $PORT already has a listener"; fi

# A real user manager on a headless runner: linger starts user@UID.service.
sudo loginctl enable-linger "$USER"
export XDG_RUNTIME_DIR=/run/user/$UID_
for _ in $(seq 1 30); do [ -S "$XDG_RUNTIME_DIR/bus" ] && break; sleep 1; done
[ -S "$XDG_RUNTIME_DIR/bus" ] || fail 2 "no user bus at $XDG_RUNTIME_DIR/bus (is dbus-user-session installed?)"
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
timeout 30 systemctl --user is-system-running --wait >/dev/null 2>&1 || true
systemctl --user show-environment >/dev/null || fail 2 "systemctl --user cannot reach the user manager"

mkdir -p "$HOME/.usage-tracker"
printf 'USAGE_TRACKER_PORT=%s\n' "$PORT" > "$HOME/.usage-tracker/config"
chmod 600 "$HOME/.usage-tracker/config"
rm -f "$HOME/.usage-tracker/no-tray-host-warned"

# ---- install ----
case "$MODE" in
  deb)
    sudo apt-get install -y -qq "$PACKAGE" >/dev/null
    UNIT=/usr/lib/systemd/user/aicur-backend.service
    AUTOSTART=/etc/xdg/autostart/ai-cur-desktop.desktop
    TRAY=(ai-cur-desktop)
    [ -L /etc/systemd/user/default.target.wants/aicur-backend.service ] \
      || fail 6 "postinst did not enable aicur-backend.service globally"
    systemctl --user daemon-reload
    ;;
  appimage)
    chmod +x "$PACKAGE"
    "$PACKAGE" --install
    UNIT=$HOME/.config/systemd/user/aicur-backend.service
    AUTOSTART=$HOME/.config/autostart/ai-cur-desktop.desktop
    TRAY=("$PACKAGE")
    ;;
  *) fail 2 "mode must be deb or appimage" ;;
esac
for path in "$UNIT" "$AUTOSTART"; do [ -f "$path" ] || fail 6 "install did not create $path"; done
desktop-file-validate "$AUTOSTART" || fail 6 "invalid autostart entry $AUTOSTART"
systemd-analyze --user verify "$UNIT" || fail 6 "systemd rejects $UNIT"
echo "installed ($MODE): $UNIT, $AUTOSTART"

# ---- the tray's start path; no tray host here, which is the recorded GNOME defect ----
set +e
TRAY_OUT=$(AICUR_SMOKE=1 xvfb-run -a "${TRAY[@]}" 2>&1)
TRAY_RC=$?
set -e
printf '%s\n' "$TRAY_OUT"
[ "$TRAY_RC" -eq 3 ] || fail 8 "tray without a tray host exited $TRAY_RC, expected 3 (refuse to run invisibly)"
grep -q "AppIndicator extension" <<<"$TRAY_OUT" || fail 8 "tray did not print the plain AppIndicator message"
grep -Eq "backend: (active|started)" <<<"$TRAY_OUT" || fail 2 "tray did not start the backend service"

# ---- running ----
DEADLINE=$((SECONDS + TIMEOUT))
until curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ok"'; do
  [ $SECONDS -lt $DEADLINE ] || fail 3 "GET 127.0.0.1:$PORT/health did not answer within ${TIMEOUT}s"
  sleep 1
done
echo "health answered after ${SECONDS}s"
DB=$HOME/.usage-tracker/claude_usage.db
ROWS=0
until [ "${ROWS:-0}" -ge 1 ]; do
  [ $SECONDS -lt $DEADLINE ] || fail 4 "collector wrote no provider_metric_samples row within ${TIMEOUT}s"
  sleep 1
  ROWS=$(python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('SELECT COUNT(*) FROM provider_metric_samples').fetchone()[0])" "$DB" 2>/dev/null || echo 0)
done
echo "collector wrote $ROWS provider_metric_samples row(s)"
[ "$(backend_pids | wc -l)" -ge 3 ] || fail 2 "expected supervisor + api + collector, saw: $(backend_pids | tr '\n' ' ')"
if ss -ltn | awk '{print $4}' | grep -E ":$PORT\$" | grep -vq '^127\.0\.0\.1:'; then
  fail 2 "port $PORT is bound beyond 127.0.0.1: $(ss -ltn | grep ":$PORT")"
fi

# ---- stopped ----
systemctl --user stop aicur-backend.service
for _ in $(seq 1 15); do [ -z "$(backend_pids)" ] && break; sleep 1; done
[ -z "$(backend_pids)" ] || fail 5 "processes survived systemctl stop: $(backend_pids | tr '\n' ' ')"
if ss -ltn "sport = :$PORT" | grep -q LISTEN; then fail 5 "port $PORT still listening after stop"; fi
echo "stopped: supervisor, api and collector all gone"

# ---- removed ----
case "$MODE" in
  deb)
    sudo apt-get remove -y -qq ai-cur-desktop >/dev/null
    for path in "$UNIT" "$AUTOSTART" /opt/ai-cur-desktop /usr/bin/ai-cur-desktop \
                /etc/systemd/user/default.target.wants/aicur-backend.service; do
      [ ! -e "$path" ] || fail 6 "apt remove left $path"
    done
    ;;
  appimage)
    "$PACKAGE" --uninstall
    for path in "$UNIT" "$AUTOSTART"; do [ ! -e "$path" ] || fail 6 "--uninstall left $path"; done
    ;;
esac
[ -z "$(backend_pids)" ] || fail 5 "backend running after uninstall"
echo "removed ($MODE)"
printf 'SMOKE PASS: %s\n' "$PACKAGE"
