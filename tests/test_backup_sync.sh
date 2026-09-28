#!/usr/bin/env bash
# Tests for scripts/backup-sync against throwaway directories.
#
#   tests/test_backup_sync.sh
#
# Nothing reaches the desktop or the real backups: notifications go to a bus
# that does not exist, the suspend guard to a logind that does not answer, and
# runtime files to a temporary XDG_RUNTIME_DIR.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$ROOT/scripts/backup-sync"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT   # created by this script, nothing else lives there

export XDG_RUNTIME_DIR="$TMP/run"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$TMP/no-bus"
export BACKUP_SYNC_LOGIND_BUS=session
export BACKUP_SYNC_FSTAB="$TMP/fstab"
unset TRIGGER_UNIT BACKUP_SYNC_SCHEDULE
mkdir -p "$XDG_RUNTIME_DIR"
: > "$BACKUP_SYNC_FSTAB"

PASS=0 FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ✓ %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  ✗ %s\n' "$1"; }
check() { if eval "$2"; then ok "$1"; else fail "$1"; fi; }
json() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$@"; }

new_case() {
    CASE="$TMP/$1"
    SRC="$CASE/src/" DST="$CASE/dst/"
    mkdir -p "$CASE/src/sub" "$CASE/dst"
    echo one > "$CASE/src/a.txt"
    echo two > "$CASE/src/sub/b \"quoted\" name.txt"
    printf '\n== %s\n' "$1"
}

run_sync() { "$SYNC" "test-job" "$SRC" "$DST" "" 0 >"$CASE/out" 2>&1; echo $?; }

# ── a normal run ──
new_case normal-run
rc=$(run_sync)
STATE="$CASE/dst/.mirror-backup"
check "exits 0" '[[ $rc == 0 ]]'
check "mirrors the files" '[[ -f "$CASE/dst/sub/b \"quoted\" name.txt" ]]'
check "state lives in the destination" '[[ -f $STATE/status.json && -f $STATE/history.jsonl && -f $STATE/backup.log ]]'
check "status is valid JSON, idle, 100 %" '[[ $(json "$STATE/status.json" "d[\"state\"], d[\"progress\"]") == "('"'"'idle'"'"', 100)" ]]'
check "status names host and boot" '[[ $(json "$STATE/status.json" "d[\"host\"]") == "$(uname -n)" && $(json "$STATE/status.json" "d[\"boot_id\"]") == "$(cat /proc/sys/kernel/random/boot_id)" ]]'
check "history entry carries the host" '[[ $(tail -1 "$STATE/history.jsonl" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"host\"])") == "$(uname -n)" ]]'
check "no runtime leftovers" '[[ -z $(ls -A "$XDG_RUNTIME_DIR/backup-sync" | grep -v "^queue.lock$") ]]'

# ── --delete keeps the state, removes the rest ──
echo stale > "$CASE/dst/stale.txt"
rc=$(run_sync)
check "second run exits 0" '[[ $rc == 0 ]]'
check "extraneous file deleted" '[[ ! -e "$CASE/dst/stale.txt" ]]'
check "own state survives --delete" '[[ -f $STATE/status.json && $(wc -l < "$STATE/history.jsonl") == 2 ]]'

# ── another job's state inside the source is not mirrored ──
mkdir -p "$CASE/src/.mirror-backup" && echo x > "$CASE/src/.mirror-backup/status.json"
mkdir -p "$CASE/src/sub/.mirror-backup" && echo x > "$CASE/src/sub/.mirror-backup/status.json"
rc=$(run_sync)
check "nested .mirror-backup of the source not copied" '[[ ! -e "$CASE/dst/sub/.mirror-backup" ]]'
rm -rf "$CASE/src/.mirror-backup" "$CASE/src/sub/.mirror-backup"

# ── missing destination: never created ──
new_case missing-destination
rmdir "$CASE/dst"
rc=$(run_sync)
check "exits 1" '[[ $rc == 1 ]]'
check "destination not created" '[[ ! -e "$CASE/dst" ]]'
check "says why" 'grep -q "Destination not found" "$CASE/out"'

# ── destination on an unmounted mount point ──
new_case unmounted-destination
echo "none $CASE/dst tmpfs defaults 0 0" > "$BACKUP_SYNC_FSTAB"
rc=$(run_sync)
check "exits 1" '[[ $rc == 1 ]]'
check "nothing written into the mount point" '[[ -z $(ls -A "$CASE/dst") ]]'
check "says why" 'grep -q "Destination not mounted: $CASE/dst" "$CASE/out"'
: > "$BACKUP_SYNC_FSTAB"

# ── source on an unmounted mount point ──
new_case unmounted-source
rc=$(run_sync)
echo "none $CASE/src tmpfs defaults 0 0" > "$BACKUP_SYNC_FSTAB"
rc=$(run_sync)
check "exits 1" '[[ $rc == 1 ]]'
check "mirror untouched" '[[ -f "$CASE/dst/a.txt" ]]'
check "error status in the destination" '[[ $(json "$CASE/dst/.mirror-backup/status.json" "d[\"state\"]") == error ]]'
: > "$BACKUP_SYNC_FSTAB"

# ── empty source, full mirror ──
new_case empty-source
rc=$(run_sync)
rm -rf "$CASE/src/"* && mkdir -p "$CASE/src"
rc=$(run_sync)
check "refuses (exit 1)" '[[ $rc == 1 ]]'
check "mirror untouched" '[[ -f "$CASE/dst/a.txt" && -f "$CASE/dst/sub/b \"quoted\" name.txt" ]]'
check "says why" 'grep -q "Source is empty" "$CASE/out"'
rc=$(BACKUP_SYNC_DELETE_MODE=disabled run_sync)
check "allowed when nothing gets deleted" '[[ $rc == 0 ]]'

# ── scheduled run already covered by another machine ──
new_case scheduled-covered
mkdir -p "$CASE/dst/.mirror-backup"
printf '{"started":"%s","finished":"%s","duration_sec":1,"exit_code":0,"files_transferred":0,"files_total":0,"error":"","host":"other-os"}\n' \
    "$(date -d '-1 minute' -Iseconds)" "$(date -Iseconds)" > "$CASE/dst/.mirror-backup/history.jsonl"
rc=$(TRIGGER_UNIT=test-job.timer BACKUP_SYNC_SCHEDULE='2099-01-01 00:00:00' run_sync)
check "exits 0" '[[ $rc == 0 ]]'
check "did not run" '[[ ! -e "$CASE/dst/a.txt" ]]'
check "logs why, naming the other host" 'grep -q "skipped: the run started .* on other-os covers this slot" "$CASE/dst/.mirror-backup/backup.log"'
check "history unchanged" '[[ $(wc -l < "$CASE/dst/.mirror-backup/history.jsonl") == 1 ]]'

rc=$(run_sync)
check "a manual start still runs" '[[ $rc == 0 && -f "$CASE/dst/a.txt" ]]'

# ── scheduled run that is due ──
new_case scheduled-due
mkdir -p "$CASE/dst/.mirror-backup"
printf '{"started":"2020-01-01T00:00:00+00:00","finished":"2020-01-01T00:01:00+00:00","duration_sec":60,"exit_code":0,"files_transferred":0,"files_total":0,"error":""}\n' \
    > "$CASE/dst/.mirror-backup/history.jsonl"
rc=$(TRIGGER_UNIT=test-job.timer BACKUP_SYNC_SCHEDULE='*-*-* 00:00:00' run_sync)
check "runs" '[[ $rc == 0 && -f "$CASE/dst/a.txt" ]]'

new_case scheduled-failed-before
mkdir -p "$CASE/dst/.mirror-backup"
printf '{"started":"%s","finished":"%s","duration_sec":1,"exit_code":12,"files_transferred":0,"files_total":0,"error":"x"}\n' \
    "$(date -d '-1 minute' -Iseconds)" "$(date -Iseconds)" > "$CASE/dst/.mirror-backup/history.jsonl"
rc=$(TRIGGER_UNIT=test-job.timer BACKUP_SYNC_SCHEDULE='2099-01-01 00:00:00' run_sync)
check "a failed last run does not cover the slot" '[[ $rc == 0 && -f "$CASE/dst/a.txt" ]]'

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
