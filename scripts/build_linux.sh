#!/bin/bash
# Build the Linux packages of the ai-cur desktop client from a frozen backend:
#   dist/ai-cur-desktop_<version>_amd64.deb
#   dist/ai-cur-desktop-<version>-x86_64.AppImage
#
#   BACKEND_DIR=dist/linux/aicur-backend VERSION=0.2.0 scripts/build_linux.sh
#
# Inputs: VERSION, BACKEND_DIR (PyInstaller one-dir folder), OUTPUT_DIR (default dist),
# APPIMAGETOOL (path to appimagetool; default: download the pinned release and verify its
# SHA-256). Set SKIP_APPIMAGE=1 only for local .deb work; CI never sets it.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"  # the embedded Python below imports clients/ relative to the repo root
VERSION=${VERSION:?set VERSION}
BACKEND_DIR=${BACKEND_DIR:?set BACKEND_DIR to the PyInstaller aicur-backend folder}
OUTPUT_DIR=${OUTPUT_DIR:-$ROOT/dist}
APPIMAGETOOL=${APPIMAGETOOL:-}
APPIMAGETOOL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
APPIMAGETOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
PKG=ai-cur-desktop

die() { printf 'build_linux: %s\n' "$*" >&2; exit 1; }
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] || die "VERSION is not a version: $VERSION"
[[ -x "$BACKEND_DIR/aicur-backend" ]] || die "BACKEND_DIR=$BACKEND_DIR has no executable aicur-backend"
# dpkg: "-" separates the Debian revision; "~" sorts a pre-release before the release.
DEB_VERSION=${VERSION//-/\~}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUTPUT_DIR"

TRAY_FILES=("$ROOT/clients/linux_tray.py" "$ROOT/clients/tray_core.py" "$ROOT/packaging/backend/aicur_config.py")
DESCRIPTION="Claude and Codex quota and activity, collected locally
 A tray icon over a local API (127.0.0.1 only) and collector that run as the
 systemd user service aicur-backend.service. No session content leaves the machine.
 .
 GNOME: stock GNOME Shell has no tray area. Without the AppIndicator extension
 (package gnome-shell-extension-appindicator, preinstalled on Ubuntu) the icon
 is not shown; the client says so once at first start, and the backend keeps
 running."

# ---------------------------------------------------------------- .deb
DEB="$WORK/deb"
mkdir -p "$DEB/DEBIAN" "$DEB/opt/$PKG/tray" "$DEB/usr/bin" "$DEB/usr/lib/systemd/user" \
         "$DEB/etc/xdg/autostart" "$DEB/usr/share/applications"
cp -a "$BACKEND_DIR" "$DEB/opt/$PKG/backend"
cp "${TRAY_FILES[@]}" "$DEB/opt/$PKG/tray/"
sed "s|@EXEC@|/opt/$PKG/backend/aicur-backend|" "$ROOT/packaging/linux/aicur-backend.service" \
  > "$DEB/usr/lib/systemd/user/aicur-backend.service"
python3 - "$DEB" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "clients")
import linux_tray
root = Path(sys.argv[1])
entry = linux_tray.desktop_entry("ai-cur-desktop")
(root / "etc/xdg/autostart/ai-cur-desktop.desktop").write_text(entry)
(root / "usr/share/applications/ai-cur-desktop.desktop").write_text(
    entry.replace("X-GNOME-Autostart-enabled=true\n", ""))
PY
# The system interpreter on purpose: python3-gi exists only for it, and a venv or toolcache
# python3 earlier on PATH would have no gi.
cat > "$DEB/usr/bin/ai-cur-desktop" <<EOF
#!/bin/sh
exec /usr/bin/python3 -B /opt/$PKG/tray/linux_tray.py "\$@"
EOF
chmod 755 "$DEB/usr/bin/ai-cur-desktop"
cat > "$DEB/DEBIAN/control" <<EOF
Package: $PKG
Version: $DEB_VERSION
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Harvto LLC <maintainer@harvto.invalid>
Homepage: https://github.com/harvto-llc/ai-usage-tracker
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, systemd
Description: $DESCRIPTION
EOF
cat > "$DEB/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ] && command -v systemctl >/dev/null 2>&1; then
  # Every user gets the backend at their next login; the tray starts it now if needed.
  systemctl --global enable aicur-backend.service
fi
EOF
cat > "$DEB/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl --global disable aicur-backend.service || true
fi
# Stop running copies (any user) so no backend outlives its files.
pkill -f /opt/ai-cur-desktop/backend/aicur-backend || true
pkill -f /opt/ai-cur-desktop/tray/linux_tray.py || true
EOF
chmod 755 "$DEB/DEBIAN/postinst" "$DEB/DEBIAN/prerm"
DEB_OUT="$OUTPUT_DIR/${PKG}_${DEB_VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group "$DEB" "$DEB_OUT" >/dev/null
printf 'deb: %s\n' "$DEB_OUT"

# ---------------------------------------------------------------- AppImage
if [[ "${SKIP_APPIMAGE:-0}" == "1" ]]; then
  printf 'AppImage skipped (SKIP_APPIMAGE=1)\n'
  exit 0
fi
if [[ -z "$APPIMAGETOOL" ]]; then
  APPIMAGETOOL="$WORK/appimagetool"
  curl -fsSL -o "$APPIMAGETOOL" "$APPIMAGETOOL_URL"
  echo "$APPIMAGETOOL_SHA256  $APPIMAGETOOL" | sha256sum -c - >/dev/null \
    || die "appimagetool download does not match the pinned SHA-256"
  chmod +x "$APPIMAGETOOL"
fi
APPDIR="$WORK/AppDir"
mkdir -p "$APPDIR/usr/lib/$PKG/tray"
cp -a "$BACKEND_DIR" "$APPDIR/usr/lib/$PKG/backend"
cp "${TRAY_FILES[@]}" "$APPDIR/usr/lib/$PKG/tray/"
cp "$ROOT/packaging/linux/aicur-backend.service" "$APPDIR/usr/lib/$PKG/aicur-backend.service"
cp "$ROOT/packaging/linux/AppRun" "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"
python3 - "$APPDIR" <<'PY'
import struct, sys, zlib
from pathlib import Path
sys.path.insert(0, "clients")
import linux_tray
root = Path(sys.argv[1])
(root / "ai-cur-desktop.desktop").write_text(
    linux_tray.desktop_entry("ai-cur-desktop").replace("Icon=utilities-system-monitor", "Icon=ai-cur-desktop"))
# appimagetool requires an icon named after Icon=; a plain 256x256 PNG is enough.
w = h = 256
raw = b"".join(b"\x00" + bytes((32, 96, 160)) * w for _ in range(h))
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
(root / "ai-cur-desktop.png").write_bytes(png)
PY
APPIMAGE_OUT="$OUTPUT_DIR/${PKG}-${VERSION}-x86_64.AppImage"
# Extract-and-run: CI runners have no FUSE for running appimagetool itself.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$APPIMAGE_OUT" >/dev/null
chmod +x "$APPIMAGE_OUT"
printf 'AppImage: %s\n' "$APPIMAGE_OUT"
