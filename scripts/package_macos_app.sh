#!/bin/zsh
set -euo pipefail

ROOT=${0:A:h:h}
VERSION=${VERSION:-0.1.0}
BUILD_NUMBER=${BUILD_NUMBER:-1}
OUTPUT_DIR=${OUTPUT_DIR:-$ROOT/dist}
SIGNING_IDENTITY=${SIGNING_IDENTITY:--}
NOTARY_PROFILE=${NOTARY_PROFILE:-}
RELEASE_BASE_URL=${RELEASE_BASE_URL:-https://github.com/harvto-llc/ai-usage-tracker/releases/download/v$VERSION}
APP="$OUTPUT_DIR/Usage Tracker.app"
ZIP="$OUTPUT_DIR/UsageTracker-$VERSION.zip"

swift build -c release --package-path "$ROOT/UsageMenuBar"
BIN_DIR=$(swift build -c release --package-path "$ROOT/UsageMenuBar" --show-bin-path)

rm -rf "$APP" "$ZIP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN_DIR/UsageMenuBar" "$APP/Contents/MacOS/UsageMenuBar"
cp "$ROOT/UsageMenuBar/Info.plist" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "$APP/Contents/Info.plist"

if [[ "$SIGNING_IDENTITY" == "-" ]]; then
  codesign --force --sign - "$APP"
else
  codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"

ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
if [[ -n "$NOTARY_PROFILE" ]]; then
  if [[ "$SIGNING_IDENTITY" == "-" ]]; then
    printf 'NOTARY_PROFILE requires a Developer ID signing identity.\n' >&2
    exit 1
  fi
  xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  rm -f "$ZIP"
  ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
fi
SHA256=$(shasum -a 256 "$ZIP" | awk '{print $1}')

mkdir -p "$OUTPUT_DIR/homebrew"
sed \
  -e "s/__VERSION__/$VERSION/g" \
  -e "s/__SHA256__/$SHA256/g" \
  -e "s|__URL__|$RELEASE_BASE_URL/UsageTracker-$VERSION.zip|g" \
  "$ROOT/packaging/homebrew/usage-tracker.rb.template" \
  > "$OUTPUT_DIR/homebrew/usage-tracker.rb"

cat > "$OUTPUT_DIR/latest.json" <<EOF
{
  "version": "$VERSION",
  "build": "$BUILD_NUMBER",
  "url": "$RELEASE_BASE_URL/UsageTracker-$VERSION.zip",
  "sha256": "$SHA256"
}
EOF

printf 'App: %s\nArchive: %s\nSHA-256: %s\n' "$APP" "$ZIP" "$SHA256"
