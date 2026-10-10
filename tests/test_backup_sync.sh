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
last_history() { tail -1 "$STATE/history.jsonl" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
# wait_for <expression over the status d>: polls the status file for up to 10 s
wait_for() {
    local _
    for _ in $(seq 100); do
        [[ $(json "$STATE/status.json" "$1" 2>/dev/null) == True ]] && return 0
        sleep 0.1
    done
    return 1
}

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
check "finished status names no phase" '[[ $(json "$STATE/status.json" "d[\"phase\"], d[\"phase_total\"]") == "('"''"', 0)" ]]'
# ./, a.txt, sub/ and the file in it — every entry, and the two directories
check "history counts the file list and its directories" '[[ $(last_history "d[\"files_total\"], d[\"dirs_total\"]") == "(4, 2)" ]]'

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

# ── network mounts: wait for the server first ──
# A port nothing listens on, and a listener that comes up on it later. The
# fstab entry's port= option points the probe there instead of NFS's 2049.
free_port() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
listen_later() {   # listen_later <delay> <port>: accepts until killed
    python3 - "$@" <<'EOF' &
import socket, sys, time
time.sleep(float(sys.argv[1]))
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[2])))
s.listen()
while True:
    s.accept()[0].close()
EOF
    LISTENER=$!
}

new_case unreachable-destination
PORT=$(free_port)
echo "127.0.0.1:/export $CASE/dst nfs defaults,port=$PORT 0 0" > "$BACKUP_SYNC_FSTAB"
rc=$(BACKUP_SYNC_NETWORK_WAIT=1 run_sync)
check "exits 1" '[[ $rc == 1 ]]'
check "waited for the server" 'grep -q "waiting up to 1s for the network — 127.0.0.1 does not answer on port $PORT" "$CASE/out"'
check "says which server" 'grep -q "Destination unreachable: 127.0.0.1 does not answer on port $PORT" "$CASE/out"'
check "nothing written into the mount point" '[[ -z $(ls -A "$CASE/dst") ]]'
: > "$BACKUP_SYNC_FSTAB"

new_case server-comes-back
PORT=$(free_port)
echo "127.0.0.1:/export $CASE/src nfs4 defaults,port=$PORT 0 0" > "$BACKUP_SYNC_FSTAB"
listen_later 2 "$PORT"
rc=$(BACKUP_SYNC_NETWORK_WAIT=20 run_sync)
kill "$LISTENER" 2>/dev/null; wait "$LISTENER" 2>/dev/null
check "waits until the server answers" 'grep -q "network is back" "$CASE/out"'
# The server answers, but nothing is mounted at the test's mount point.
check "then checks the mount as before" '[[ $rc == 1 ]] && grep -q "Source not mounted: $CASE/src" "$CASE/out"'
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

# ── progress through the phases ──
# A stand-in rsync prints what the real one prints in each phase (rsync -ii
# --info=progress2,flist2 --debug=del2), and moves on only when the test has
# seen the status of the phase.
new_case phases
STATE="$CASE/dst/.mirror-backup"
mkdir -p "$TMP/bin" "$STATE" "$CASE/gate"
printf '{"started":"2020-01-01T00:00:00+00:00","finished":"2020-01-01T00:01:00+00:00","duration_sec":60,"exit_code":0,"files_transferred":0,"files_total":4000,"dirs_total":40,"error":""}\n' \
    > "$STATE/history.jsonl"
cat > "$TMP/bin/rsync" <<'EOF'
#!/usr/bin/env bash
step() { for _ in $(seq 600); do [[ -e "$GATE/$1" ]] && return; sleep 0.05; done; exit 99; }
printf 'building file list ... \n'
for n in $(seq 0 100 3000); do printf ' %d files...\r' "$n"; done
step listed
printf '4100 files to consider\ndeleting in .\n'
for d in $(seq 10); do printf 'delete_in_dir(dir%d)\n' "$d"; done
printf 'delete_item(dir3/old) mode=100644 flags=2\n*deleting   dir3/old\n'
step deleted
printf '.d          ./\n'
for f in $(seq 2049); do printf '.f          dir%d/file%d\n' $((f % 40)) "$f"; done
printf '>f+++++++++ dir7/new file\n'
printf '         12.34K   0%%    1.23MB/s    0:00:01 (xfr#1, to-chk=2049/4100)\n'
step compared
EOF
chmod +x "$TMP/bin/rsync"
PATH="$TMP/bin:$PATH" GATE="$CASE/gate" "$SYNC" test-job "$SRC" "$DST" "" 0 >"$CASE/out" 2>&1 &
SYNC_PID=$!
listing='(d["state"], d["phase"], d["phase_done"], d["phase_total"], d["phase_estimated"], d["progress"]) == ("scanning", "listing", 3000, 4000, True, 75.0)'
check "listing: counted against the previous run's file list" 'wait_for "$listing"'
touch "$CASE/gate/listed"
deleting='(d["state"], d["phase"], d["phase_done"], d["phase_total"], d["phase_estimated"], d["progress"], d["current_file"]) == ("scanning", "deleting", 10, 40, True, 25.0, "dir10")'
check "deleting: directories against the previous run's count" 'wait_for "$deleting"'
touch "$CASE/gate/deleted"
checking='(d["state"], d["phase"], d["phase_done"], d["phase_total"], d["phase_estimated"], d["progress"], d["files_total"], d["files_remaining"]) == ("running", "checking", 2051, 4100, False, 50.0, 4100, 2049)'
check "checking: every entry against this run's file list" 'wait_for "$checking"'
copying='(d["current_file"], d["speed"], d["files_transferred"]) == ("dir7/new file", "1.23MB/s", 1)'
check "names the file being copied, the speed and the copies" 'wait_for "$copying"'
touch "$CASE/gate/compared"
wait "$SYNC_PID"; rc=$?
check "exits 0" '[[ $rc == 0 ]]'
check "finished: idle, 100 %, no phase" '[[ $(json "$STATE/status.json" "d[\"state\"], d[\"progress\"], d[\"phase\"]") == "('"'"'idle'"'"', 100, '"''"')" ]]'
check "history carries this run's totals" '[[ $(last_history "d[\"files_total\"], d[\"dirs_total\"]") == "(4100, 1)" ]]'

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
