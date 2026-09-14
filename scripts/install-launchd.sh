#!/bin/bash
# Render the launchd templates for THIS checkout and THIS user, install them under
# ~/Library/LaunchAgents, and start the jobs. Safe to re-run: an already-loaded job is
# booted out and back in so it picks up the new plist.
#
#   scripts/install-launchd.sh            # api + collector (+ menubar if it is built)
#   scripts/install-launchd.sh --uninstall
#
# Nothing here needs sudo and nothing leaves ~/Library/LaunchAgents and ~/.usage-tracker.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/.usage-tracker/logs"
PYTHON="${PYTHON:-$(command -v python3)}"
UVICORN="${UVICORN:-$(command -v uvicorn || true)}"
if [ -z "$UVICORN" ]; then
  # uvicorn was installed for this python but is not on PATH; run it as a module.
  UVICORN="$PYTHON -m uvicorn"
fi
SEARCH_PATH="$(dirname "$PYTHON"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LABELS=(com.harvto.usage-tracker.api com.harvto.usage-tracker.collector)
if [ -x "$REPO/UsageMenuBar/.build/release/UsageMenuBar" ]; then
  LABELS+=(com.harvto.usage-tracker.menubar)
fi

bootout() {
  launchctl bootout "gui/$(id -u)" "$AGENTS/$1.plist" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--uninstall" ]; then
  for label in "${LABELS[@]}" com.harvto.usage-tracker.menubar; do
    bootout "$label"
    rm -f "$AGENTS/$label.plist"
  done
  echo "removed launchd jobs; ~/.usage-tracker left in place"
  exit 0
fi

mkdir -p "$AGENTS" "$LOGS"
for label in "${LABELS[@]}"; do
  template="$REPO/launchd/$label.plist.template"
  [ -f "$template" ] || { echo "missing $template" >&2; exit 1; }
  bootout "$label"
  sed -e "s|__REPO__|$REPO|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__UVICORN__|$UVICORN|g" \
      -e "s|__PATH__|$SEARCH_PATH|g" \
      "$template" > "$AGENTS/$label.plist"
  plutil -lint "$AGENTS/$label.plist" >/dev/null
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "started $label"
done
echo "logs: $LOGS"
