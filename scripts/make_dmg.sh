#!/bin/bash
# Build dist/ai-cur-desktop-<version>.dmg: "Usage Tracker.app" (the Swift menu bar app with
# the frozen backend for arm64 and x86_64 inside) plus an Applications symlink. hdiutil only.
#
#   BACKEND_ARM64=dist/backend-arm64/aicur-backend \
#   BACKEND_X86_64=dist/backend-x86_64/aicur-backend \
#   VERSION=0.2.0 scripts/make_dmg.sh
#
# Inputs (environment):
#   VERSION, BUILD_NUMBER        default 0.1.0 / 1
#   OUTPUT_DIR                   default <repo>/dist
#   BACKEND_ARM64, BACKEND_X86_64  PyInstaller one-dir folders (packaging/backend/build_backend.py).
#                                Both are required: the app is universal.
#   SIGNING_IDENTITY             default "-" (ad hoc). A Developer ID identity turns on the
#                                hardened runtime and a secure timestamp.
#   NOTARY_PROFILE               notarytool keychain profile. Requires a real identity: the script
#                                refuses to start when it is set with the ad hoc identity.
#   APP_BINARY                   use this prebuilt menu bar executable instead of `swift build`
#                                (local testing only).
#
# All argument checks run before anything is built, so a bad invocation fails in milliseconds.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${VERSION:-0.1.0}
BUILD_NUMBER=${BUILD_NUMBER:-1}
OUTPUT_DIR=${OUTPUT_DIR:-$ROOT/dist}
SIGNING_IDENTITY=${SIGNING_IDENTITY:--}
NOTARY_PROFILE=${NOTARY_PROFILE:-}
BACKEND_ARM64=${BACKEND_ARM64:-}
BACKEND_X86_64=${BACKEND_X86_64:-}
APP_BINARY=${APP_BINARY:-}
APP_NAME="Usage Tracker.app"
VOLUME_NAME="ai-cur desktop client"
DMG="$OUTPUT_DIR/ai-cur-desktop-$VERSION.dmg"
ENTITLEMENTS="$ROOT/packaging/macos/backend.entitlements"

die() { printf 'make_dmg: %s\n' "$*" >&2; exit 1; }

# ---- argument checks (no side effects above this line) ----
if [[ -n "$NOTARY_PROFILE" && "$SIGNING_IDENTITY" == "-" ]]; then
  die "NOTARY_PROFILE requires a Developer ID signing identity; SIGNING_IDENTITY is ad hoc (-)"
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || die "VERSION is not a version: $VERSION"
[[ "$BUILD_NUMBER" =~ ^[0-9]+$ ]] || die "BUILD_NUMBER must be an integer: $BUILD_NUMBER"
for var in BACKEND_ARM64 BACKEND_X86_64; do  # bash 3.2 (macOS): no ${x^^}, use ${!var}
  dir=${!var}
  [[ -n "$dir" ]] || die "$var is required (the app ships a backend for both arm64 and x86_64)"
  [[ -x "$dir/aicur-backend" ]] || die "$var=$dir has no executable aicur-backend"
done
if [[ -n "$APP_BINARY" && ! -x "$APP_BINARY" ]]; then die "APP_BINARY is not executable: $APP_BINARY"; fi
for tool in hdiutil codesign plutil ditto; do
  command -v "$tool" >/dev/null || die "$tool not found (this script runs on macOS)"
done

# ---- build ----
if [[ -z "$APP_BINARY" ]]; then
  swift build -c release --arch arm64 --arch x86_64 --package-path "$ROOT/UsageMenuBar"
  BIN_DIR=$(swift build -c release --arch arm64 --arch x86_64 --package-path "$ROOT/UsageMenuBar" --show-bin-path)
  APP_BINARY="$BIN_DIR/UsageMenuBar"
fi

APP="$OUTPUT_DIR/$APP_NAME"
rm -rf "$APP" "$DMG"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/backend"
cp "$APP_BINARY" "$APP/Contents/MacOS/UsageMenuBar"
cp "$ROOT/UsageMenuBar/Info.plist" "$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$BUILD_NUMBER" "$APP/Contents/Info.plist"
ditto "$BACKEND_ARM64" "$APP/Contents/Resources/backend/arm64"
ditto "$BACKEND_X86_64" "$APP/Contents/Resources/backend/x86_64"

# ---- sign, inside out: every Mach-O in the backends, then the app ----
sign() {
  if [[ "$SIGNING_IDENTITY" == "-" ]]; then
    codesign --force --sign - "$@"
  else
    codesign --force --options runtime --timestamp --sign "$SIGNING_IDENTITY" "$@"
  fi
}
while IFS= read -r -d '' file; do
  if file -b "$file" | grep -q 'Mach-O'; then
    if [[ "$SIGNING_IDENTITY" == "-" ]]; then
      sign "$file"
    else
      sign --entitlements "$ENTITLEMENTS" "$file"
    fi
  fi
done < <(find "$APP/Contents/Resources/backend" -type f -print0)
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# ---- dmg ----
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/aicur-dmg.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT
ditto "$APP" "$STAGING/$APP_NAME"
ln -s /Applications "$STAGING/Applications"
hdiutil create -quiet -volname "$VOLUME_NAME" -srcfolder "$STAGING" -ov -format UDZO "$DMG"
if [[ "$SIGNING_IDENTITY" != "-" ]]; then
  codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG"
fi
if [[ -n "$NOTARY_PROFILE" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi
hdiutil verify -quiet "$DMG"

SHA256=$(shasum -a 256 "$DMG" | awk '{print $1}')
printf 'App: %s\nDMG: %s\nSHA-256: %s\n' "$APP" "$DMG" "$SHA256"
