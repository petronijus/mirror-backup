#!/usr/bin/env bash
# Build release artifacts:
#   1. GNOME Extension zip (self-contained, installable via gnome-extensions install)
#   2. Source tarball (installable via ./install.sh)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_UUID="backup-monitor@petronijus"
VERSION="${1:-$(grep -oP "APP_VERSION = '\K[^']+" "$SCRIPT_DIR/src/backup_monitor/__init__.py")}"
BUILD_DIR="$SCRIPT_DIR/build"

echo "=== Building Mirror Backup v${VERSION} ==="

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ── 1. GNOME Extension zip ──
echo "Building extension zip..."
EXT_BUILD="$BUILD_DIR/ext"
mkdir -p "$EXT_BUILD"

# Extension core files
cp "$SCRIPT_DIR"/gnome-extension/* "$EXT_BUILD/"

# Bundle the desktop app (the extension copies it to ~/.local/share/mirror-backup
# on its first run, where the mirror-backup command expects it)
cp -r "$SCRIPT_DIR/src" "$EXT_BUILD/app"

# Bundle data (CSS, desktop file)
mkdir -p "$EXT_BUILD/data"
cp "$SCRIPT_DIR/data/style.css" "$EXT_BUILD/data/"
cp "$SCRIPT_DIR/data/com.github.petronijus.BackupMonitor.desktop" "$EXT_BUILD/data/"

# Bundle backup-sync script
mkdir -p "$EXT_BUILD/scripts"
cp "$SCRIPT_DIR/scripts/backup-sync" "$SCRIPT_DIR/scripts/mirror-backup" "$EXT_BUILD/scripts/"

# Bundle the resume unit (installed and enabled by the extension on first run)
mkdir -p "$EXT_BUILD/systemd"
cp "$SCRIPT_DIR/systemd/mirror-backup-resume.service" "$EXT_BUILD/systemd/"

# Bundle config templates
mkdir -p "$EXT_BUILD/config"
cp "$SCRIPT_DIR"/config/* "$EXT_BUILD/config/" 2>/dev/null || true

# Remove __pycache__
find "$EXT_BUILD" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
find "$EXT_BUILD" -name '*.pyc' -delete 2>/dev/null || true

# Create zip (must have metadata.json at root)
ZIP_FILE="$BUILD_DIR/${EXT_UUID}.zip"
(cd "$EXT_BUILD" && zip -r "$ZIP_FILE" . -x '*.pyc' '*__pycache__*')

echo "  ✔ $ZIP_FILE ($(du -h "$ZIP_FILE" | cut -f1))"

# ── 2. Source tarball ──
echo "Building source tarball..."
TARBALL="$BUILD_DIR/mirror-backup-v${VERSION}.tar.gz"
git archive --format=tar.gz --prefix="mirror-backup-v${VERSION}/" HEAD -o "$TARBALL"
echo "  ✔ $TARBALL ($(du -h "$TARBALL" | cut -f1))"

# ── Done ──
echo ""
echo "=== Build complete ==="
echo ""
echo "Artifacts in $BUILD_DIR/:"
ls -lh "$BUILD_DIR/"*.{zip,tar.gz} 2>/dev/null
echo ""
echo "Install extension:"
echo "  gnome-extensions install --force $ZIP_FILE"
echo "  # Then log out/in to activate"
echo ""
echo "Install from tarball:"
echo "  tar xzf $TARBALL && cd mirror-backup-v${VERSION} && ./install.sh"
