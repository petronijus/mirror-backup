#!/usr/bin/env bash
# Uninstall Mirror Backup. Everything removed goes to the trash.
#
# Kept: the jobs (~/.config/backup-sync, or the overlay it links to) and each
# job's state in its destination (<destination>/.mirror-backup) — both are
# data, and other installs sharing them may still be using them.
set -euo pipefail

EXT_UUID="backup-monitor@petronijus"
PLUGIN_ID="petronijus.mirror-backup"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_HOME/systemd/user"

trash() {
    [[ -e "$1" || -L "$1" ]] || return 0
    if command -v gio >/dev/null && gio trash -- "$1" 2>/dev/null; then return 0; fi
    if command -v trash-put >/dev/null && trash-put -- "$1"; then return 0; fi
    echo "  ! cannot move $1 to the trash — left in place" >&2
}

echo "=== Mirror Backup uninstaller ==="

echo "Stopping backup timers and runs..."
shopt -s nullglob
for unit in "$UNIT_DIR"/backup-*.timer "$UNIT_DIR"/backup-*.service; do
    systemctl --user disable --now "$(basename "$unit")" 2>/dev/null || true
done
systemctl --user disable mirror-backup-resume.service 2>/dev/null || true

echo "Removing systemd units..."
for unit in "$UNIT_DIR"/backup-*.service "$UNIT_DIR"/backup-*.timer "$UNIT_DIR"/backup-*.timer.d \
            "$UNIT_DIR/mirror-backup-resume.service"; do
    trash "$unit"
done
systemctl --user daemon-reload

echo "Removing panels..."
gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
trash "$DATA_HOME/gnome-shell/extensions/$EXT_UUID"
if command -v omarchy >/dev/null; then
    omarchy plugin disable "$PLUGIN_ID" >/dev/null 2>&1 || true
fi
trash "$CONFIG_HOME/omarchy/plugins/$PLUGIN_ID"

echo "Removing the app and commands..."
trash "$DATA_HOME/mirror-backup"
trash "$DATA_HOME/applications/com.github.petronijus.BackupMonitor.desktop"
trash "$HOME/.local/bin/backup-sync"
trash "$HOME/.local/bin/mirror-backup"

echo ""
echo "=== Uninstalled ==="
echo "Kept: $CONFIG_HOME/backup-sync (jobs) and <destination>/.mirror-backup (job state)."
