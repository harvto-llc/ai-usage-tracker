#!/bin/zsh
set -euo pipefail

APP=${1:-${0:A:h:h}/dist/Usage Tracker.app}
PLIST="$APP/Contents/Info.plist"
BINARY="$APP/Contents/MacOS/UsageMenuBar"

[[ -x "$BINARY" ]]
[[ "$(plutil -extract CFBundleIdentifier raw "$PLIST")" == "com.harvto.aicur.desktop" ]]
[[ "$(plutil -extract CFBundlePackageType raw "$PLIST")" == "APPL" ]]
[[ "$(plutil -extract LSUIElement raw "$PLIST")" == "true" ]]
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ "${RUN_ACCESSIBILITY_TESTS:-0}" == "1" ]]; then
  STATUS_ITEM_VISIBLE=0
fi

open -gja "$APP"
sleep 3
PID=$(pgrep -f "$BINARY" | head -1)
[[ -n "$PID" ]]
trap 'kill "$PID" 2>/dev/null || true' EXIT

if [[ "${RUN_ACCESSIBILITY_TESTS:-0}" == "1" ]]; then
  for _ in {1..30}; do
    STATUS_ITEM_VISIBLE=$(/usr/bin/swift -e 'import CoreGraphics
let bundleID = CommandLine.arguments[1]
let windows = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
let visible = windows.contains { window in
    guard (window[kCGWindowName as String] as? String) == bundleID,
          let bounds = window[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? Double,
          let height = bounds["Height"] as? Double else { return false }
    return width > 0 && height > 0
}
print(visible ? "1" : "0")' "$(plutil -extract CFBundleIdentifier raw "$PLIST")")
    [[ "$STATUS_ITEM_VISIBLE" == "1" ]] && break
    sleep 1
  done
  if [[ "$STATUS_ITEM_VISIBLE" != "1" ]]; then
    printf 'Usage Tracker status item was not rendered in the menu bar.\n' >&2
    exit 1
  fi
fi

printf 'macOS app verification passed: %s\n' "$APP"
