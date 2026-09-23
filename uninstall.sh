#!/usr/bin/env bash
# Uninstall Mirror Backup for GNOME
set -euo pipefail

EXT_UUID="backup-monitor@petronijus"

echo "=== Mirror Backup for GNOME Uninstaller ==="

# 1. Stop and disable all backup timers/services (whatever jobs exist)
echo "Stopping backup timers..."
shopt -s nullglob
for unit in "$HOME/.config/systemd/user"/backup-*.timer "$HOME/.config/systemd/user"/backup-*.service; do
    name="$(basename "$unit")"
    systemctl --user disable --now "$name" 2>/dev/null || true
    systemctl --user stop "$name" 2>/dev/null || true
done

systemctl --user disable mirror-backup-resume.service 2>/dev/null || true

# 2. Remove systemd units
echo "Removing systemd units..."
rm -f "$HOME/.config/systemd/user"/backup-*.service "$HOME/.config/systemd/user"/backup-*.timer
rm -f "$HOME/.config/systemd/user/mirror-backup-resume.service"
rm -rf "$HOME/.config/systemd/user"/backup-*.timer.d
systemctl --user daemon-reload

# 4. Remove GNOME extension
echo "Removing GNOME extension..."
gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

# 5. Remove script (keep configs and logs)
rm -f "$HOME/.local/bin/backup-sync"

echo ""
echo "=== Uninstalled ==="
echo "Kept: ~/.config/backup-sync/ (configs) and ~/.local/share/backup-sync/ (logs/status)"
echo "Remove manually if not needed."
