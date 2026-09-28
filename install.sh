#!/usr/bin/env bash
# Install Mirror Backup: backup-sync, the app and its `mirror-backup` command,
# the job units, and the panel of the desktop in use — the GNOME Shell
# extension, or the Omarchy bar widget.
#
#   ./install.sh [--desktop gnome|omarchy|none]
#
# Re-running it updates an install in place. With a private overlay that holds
# jobs (private/configs/backup-sync/jobs.json), the config dir
# ~/.config/backup-sync becomes a link into it, so every install using this
# checkout — the other OS of a dual boot, say — works from one set of jobs.
# Job state lives in each destination (<destination>/.mirror-backup), which
# every install mounting it sees; see README, "Several machines, one set of jobs".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXT_UUID="backup-monitor@petronijus"
PLUGIN_ID="petronijus.mirror-backup"
BIN_DIR="$HOME/.local/bin"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_HOME="$DATA_HOME/mirror-backup"
CONFIG_DIR="$CONFIG_HOME/backup-sync"
UNIT_DIR="$CONFIG_HOME/systemd/user"
OVERLAY_CONFIG="$SCRIPT_DIR/private/configs/backup-sync"

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

# Files are never destroyed: whatever this installer replaces goes to the trash.
trash() {
    if command -v gio >/dev/null && gio trash -- "$1" 2>/dev/null; then return 0; fi
    if command -v trash-put >/dev/null && trash-put -- "$1"; then return 0; fi
    die "cannot move $1 to the trash (no gio/trash-put) — move it away by hand and re-run"
}

DESKTOP=""
while (( $# )); do
    case "$1" in
        --desktop) DESKTOP="${2:?--desktop needs gnome, omarchy or none}"; shift 2 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done
if [[ -z "$DESKTOP" ]]; then
    if command -v omarchy-shell >/dev/null || [[ -d /usr/share/omarchy/shell ]]; then
        DESKTOP=omarchy
    elif command -v gnome-shell >/dev/null; then
        DESKTOP=gnome
    else
        DESKTOP=none
    fi
fi
case "$DESKTOP" in gnome|omarchy|none) ;; *) die "--desktop must be gnome, omarchy or none" ;; esac

echo "=== Mirror Backup installer (panel: $DESKTOP) ==="

step "backup-sync and mirror-backup → $BIN_DIR"
install -Dm755 "$SCRIPT_DIR/scripts/backup-sync" "$BIN_DIR/backup-sync"
install -Dm755 "$SCRIPT_DIR/scripts/mirror-backup" "$BIN_DIR/mirror-backup"
ok "installed"

step "app → $APP_HOME"
# An installed copy of src/, rebuilt from the checkout on every run.
mkdir -p "$APP_HOME/app" "$APP_HOME/data"
rsync -a --delete --exclude=__pycache__ "$SCRIPT_DIR/src/" "$APP_HOME/app/"
install -Dm644 "$SCRIPT_DIR/data/style.css" "$APP_HOME/data/style.css"
ok "installed ($(sed -n "s/^APP_VERSION = '\(.*\)'/\1/p" "$SCRIPT_DIR/src/backup_monitor/__init__.py"))"

step "jobs → $CONFIG_DIR"
if [[ -f "$OVERLAY_CONFIG/jobs.json" ]]; then
    target="$(readlink -f "$OVERLAY_CONFIG")"
    if [[ -L "$CONFIG_DIR" && "$(readlink -f "$CONFIG_DIR")" == "$target" ]]; then
        ok "linked to the overlay ($target)"
    else
        if [[ -L "$CONFIG_DIR" ]]; then
            trash "$CONFIG_DIR"
        elif [[ -d "$CONFIG_DIR" ]]; then
            # The overlay replaces this dir. Anything in it the overlay lacks
            # would be lost to the jobs — stop rather than guess.
            extras=()
            while IFS= read -r -d '' f; do
                rel="${f#"$CONFIG_DIR"/}"
                [[ -e "$OVERLAY_CONFIG/$rel" ]] || extras+=("$rel")
            done < <(find "$CONFIG_DIR" -type f -print0)
            if (( ${#extras[@]} )); then
                printf '    %s\n' "${extras[@]}" >&2
                die "$CONFIG_DIR has files the overlay lacks (above) — move them into $OVERLAY_CONFIG or away, then re-run"
            fi
            trash "$CONFIG_DIR"
            ok "previous $CONFIG_DIR moved to the trash (the overlay has every file)"
        fi
        mkdir -p "$(dirname "$CONFIG_DIR")"
        ln -s "$target" "$CONFIG_DIR"
        ok "linked to the overlay ($target)"
    fi
else
    mkdir -p "$CONFIG_DIR"
    for f in "$SCRIPT_DIR"/config/*; do
        [[ -f "$f" && ! -e "$CONFIG_DIR/$(basename "$f")" ]] && install -Dm644 "$f" "$CONFIG_DIR/$(basename "$f")"
    done
    ok "local config dir (no overlay with jobs.json) — create jobs in the app"
fi

step "state from before 0.6 → the destinations"
LEGACY="$DATA_HOME/backup-sync"
if [[ -d "$LEGACY" ]]; then
    if "$BIN_DIR/mirror-backup" migrate-legacy --retire; then
        ok "migrated; $LEGACY is in the trash"
    else
        warn "not every job could be migrated (destination not mounted?) — $LEGACY kept; re-run later"
    fi
else
    ok "nothing to migrate"
fi

step "systemd units"
install -Dm644 "$SCRIPT_DIR/systemd/mirror-backup-resume.service" "$UNIT_DIR/mirror-backup-resume.service"
systemctl --user daemon-reload
systemctl --user enable mirror-backup-resume.service >/dev/null 2>&1
ok "mirror-backup-resume.service enabled"
"$BIN_DIR/mirror-backup" sync-units || warn "some units could not be synced (see above)"

step "desktop entry"
install -Dm644 /dev/stdin "$DATA_HOME/applications/com.github.petronijus.BackupMonitor.desktop" <<DEOF
[Desktop Entry]
Name=Mirror Backup
Comment=Monitor and manage rsync backups
Exec=$BIN_DIR/mirror-backup
Icon=drive-harddisk-symbolic
Terminal=false
Type=Application
Categories=Utility;System;
Keywords=backup;rsync;sync;monitor;
StartupNotify=true
DEOF
ok "installed"

case "$DESKTOP" in
gnome)
    step "GNOME Shell extension"
    EXT_DIR="$DATA_HOME/gnome-shell/extensions/$EXT_UUID"
    mkdir -p "$EXT_DIR"
    for f in "$SCRIPT_DIR"/gnome-extension/*; do
        install -Dm644 "$f" "$EXT_DIR/$(basename "$f")"
    done
    # Up to 0.5 the app was bundled into the extension; the installed copy in
    # $APP_HOME is used now. Release zips still bundle it, for their first run.
    [[ -d "$EXT_DIR/app" ]] && trash "$EXT_DIR/app"
    gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
    ok "installed — a newly installed extension appears after logging out and in (Wayland)"
    ;;
omarchy)
    step "Omarchy bar widget"
    PLUGIN_DIR="$CONFIG_HOME/omarchy/plugins/$PLUGIN_ID"
    mkdir -p "$PLUGIN_DIR"
    # An installed copy, like the app: the shell reloads it when files change.
    rsync -a --delete "$SCRIPT_DIR/omarchy-plugin/" "$PLUGIN_DIR/"
    if omarchy-shell shell ping >/dev/null 2>&1; then
        omarchy-shell shell rescanPlugins >/dev/null
        state="$(omarchy plugin list 2>/dev/null | awk -v id="$PLUGIN_ID" '$1 == id { print $2 }')"
        if [[ "$state" != enabled ]]; then
            omarchy plugin enable "$PLUGIN_ID" >/dev/null
        fi
        ok "installed and enabled (bar, right section; move it with: omarchy bar move $PLUGIN_ID)"
    else
        warn "omarchy-shell is not running — enable later: omarchy plugin enable $PLUGIN_ID"
    fi
    ;;
none)
    ok "no panel (--desktop none)"
    ;;
esac

echo
echo "=== Done ==="
echo "Open the app: mirror-backup    Jobs at a glance: mirror-backup status"
