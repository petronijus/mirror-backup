#!/usr/bin/env bash
# Copy Mirror Backup's adw_omarchy.py (the Omarchy look for libadwaita apps)
# into another app, stamped with the hash of the source it came from.
#
#   scripts/vendor-adw-omarchy.sh <app>/adw_omarchy.py            write or update the copy
#   scripts/vendor-adw-omarchy.sh --check <app>/adw_omarchy.py    exit 1 if outdated or edited
#   scripts/vendor-adw-omarchy.sh --force <app>/adw_omarchy.py    replace an edited copy (→ trash)
#
# The copy starts with two header lines; its body is src/backup_monitor/adw_omarchy.py
# byte for byte, so the recorded hash tells an outdated copy (body matches an
# older source) from an edited one (body matches no hash at all).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/src/backup_monitor/adw_omarchy.py"
mode=write
case "${1:-}" in
    --check) mode=check; shift ;;
    --force) mode=force; shift ;;
esac
DEST="${1:?usage: vendor-adw-omarchy.sh [--check|--force] <dest>/adw_omarchy.py}"

sha() { sha256sum | cut -d' ' -f1; }
src_hash=$(sha < "$SRC")
header() {
    printf '# Vendored from mirror-backup (src/backup_monitor/adw_omarchy.py), sha256:%s.\n' "$1"
    printf '# Do not edit here: change it in mirror-backup, then run its scripts/vendor-adw-omarchy.sh.\n'
}

# state of an existing copy: current | outdated | edited
copy_state() {
    local recorded body_hash
    recorded=$(sed -n '1s/.*sha256:\([0-9a-f]\{64\}\)\..*/\1/p' "$DEST")
    body_hash=$(tail -n +3 "$DEST" | sha)
    if [[ -z "$recorded" || "$body_hash" != "$recorded" ]]; then
        echo edited
    elif [[ "$recorded" != "$src_hash" ]]; then
        echo outdated
    else
        echo current
    fi
}

if [[ $mode == check ]]; then
    [[ -f "$DEST" ]] || { echo "$DEST: missing"; exit 1; }
    state=$(copy_state)
    echo "$DEST: $state"
    [[ $state == current ]]
    exit
fi

if [[ -f "$DEST" ]]; then
    state=$(copy_state)
    if [[ $state == current ]]; then
        echo "$DEST: already current"
        exit 0
    fi
    if [[ $state == edited ]]; then
        [[ $mode == force ]] || {
            echo "$DEST was edited in place — carry the change into mirror-backup, then re-run with --force" >&2
            exit 1
        }
        gio trash -- "$DEST" 2>/dev/null || trash-put -- "$DEST" \
            || { echo "cannot move $DEST to the trash" >&2; exit 1; }
    fi
fi
tmp=$(mktemp "$(dirname "$DEST")/.adw_omarchy.XXXXXX")
{ header "$src_hash"; cat "$SRC"; } > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$DEST"
echo "$DEST: vendored sha256:${src_hash:0:12}"
