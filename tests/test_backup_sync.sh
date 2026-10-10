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
export BACKUP_SYNC_UNIT_DIRS="$TMP/units"   # mount units the tests declare; none of the real ones
mkdir -p "$BACKUP_SYNC_UNIT_DIRS"
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

# ── destination on a mount point a mount unit declares ──
new_case unit-mounted-destination
: > "$BACKUP_SYNC_UNIT_DIRS/$(systemd-escape --path "$CASE/dst").mount"
rc=$(run_sync)
check "exits 1" '[[ $rc == 1 ]]'
check "nothing written into the mount point" '[[ -z $(ls -A "$CASE/dst") ]]'
check "says why" 'grep -q "Destination not mounted: $CASE/dst" "$CASE/out"'
rm -f "$BACKUP_SYNC_UNIT_DIRS"/*.mount

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

# ── the system queue: one lock for user and system jobs ──
# The system part's tmpfiles.d entry makes it root's and read-only; a user job
# takes it all the same (flock needs an open file, not a writable one).
new_case shared-queue
SYSRUN="$TMP/sysrun"
mkdir -p "$SYSRUN" && : > "$SYSRUN/queue.lock" && chmod 444 "$SYSRUN/queue.lock"
STATE="$CASE/dst/.mirror-backup"
flock "$SYSRUN/queue.lock" sleep 2 &
HOLDER=$!
sleep 0.3
BACKUP_SYNC_SYSTEM_RUN_DIR="$SYSRUN" "$SYNC" test-job "$SRC" "$DST" "" 0 >"$CASE/out" 2>&1 &
SYNC_PID=$!
check "waits in the shared queue" 'wait_for "d[\"state\"] == \"queued\""'
wait "$HOLDER"; wait "$SYNC_PID"; rc=$?
check "then runs" '[[ $rc == 0 && -f "$CASE/dst/a.txt" ]]'

new_case system-scope
STATE="$CASE/dst/.mirror-backup"
rc=$(BACKUP_SYNC_SCOPE=system BACKUP_SYNC_SYSTEM_RUN_DIR="$SYSRUN" run_sync)
check "exits 0" '[[ $rc == 0 && -f "$CASE/dst/a.txt" ]]'
check "runtime files in the system run dir, none in the user's" \
    '[[ -z $(ls -A "$SYSRUN" | grep -v "^queue.lock$") && ! -e "$XDG_RUNTIME_DIR/backup-sync/test-job.pid" ]]'
rc=$(BACKUP_SYNC_SCOPE=galaxy run_sync)
check "an unknown scope is refused" '[[ $rc == 2 ]]'

# ── restic ──
if ! command -v restic >/dev/null; then
    printf '\n== restic\n  (restic not installed — skipped)\n'
else
export RESTIC_PASSWORD_FILE="$TMP/restic.key" RESTIC_CACHE_DIR="$TMP/restic-cache"
echo "test password" > "$RESTIC_PASSWORD_FILE"
# run_restic [mode]: SRC and DST as a restic job sees them — the path list,
# or the source job's destination when copying.
run_restic() {
    BACKUP_SYNC_ENGINE=restic BACKUP_SYNC_RESTIC_MODE="${1:-backup}" BACKUP_SYNC_RESTIC_HOST=test-host \
        "$SYNC" test-job "$RSRC" "$RDST" "${REXCLUDE:-}" 0 >"$CASE/out" 2>&1
    echo $?
}
snapshots() { restic -r "$1" snapshots --json --no-lock 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$2"; }

new_case restic-backup
mkdir -p "$CASE/second" && echo three > "$CASE/second/c.txt" && echo skip > "$CASE/src/skip.me"
printf '# paths\n%s\n\n  %s  \n' "$CASE/src" "$CASE/second" > "$CASE/paths"
printf '*.me\n' > "$CASE/exclude"
restic init -r "$CASE/dst/repo" >/dev/null 2>&1
RSRC="$CASE/paths" RDST="$CASE/dst" REXCLUDE="$CASE/exclude"
STATE="$CASE/dst/.mirror-backup"
rc=$(BACKUP_SYNC_RESTIC_KEEP="--keep-last 2" run_restic)
check "exits 0" '[[ $rc == 0 ]]'
check "one snapshot, of both listed paths, under the job's host" \
    '[[ $(snapshots "$CASE/dst/repo" "len(d), d[0][\"hostname\"], len(d[0][\"paths\"])") == "(1, '"'"'test-host'"'"', 2)" ]]'
check "the exclude file applies" '! restic -r "$CASE/dst/repo" ls latest --no-lock 2>/dev/null | grep -q skip.me'
check "the state stays out of the repository" '[[ -f $STATE/status.json && -d "$CASE/dst/repo/data" ]]'
check "status: idle, 100 %, engine restic" \
    '[[ $(json "$STATE/status.json" "d[\"state\"], d[\"progress\"], d[\"engine\"]") == "('"'"'idle'"'"', 100, '"'"'restic'"'"')" ]]'
check "status names the paths" '[[ $(json "$STATE/status.json" "d[\"src\"]") == "$CASE/src, $CASE/second" ]]'
SNAP=$(snapshots "$CASE/dst/repo" 'd[0]["id"]')
check "history: the snapshot and what it added" \
    '[[ $(last_history "d[\"engine\"], d[\"snapshot_id\"], d[\"files_new\"], d[\"exit_code\"]") == "('"'"'restic'"'"', '"'"'$SNAP'"'"', 3, 0)" ]]'
check "first run prunes and checks" \
    '[[ -n $(json "$STATE/maintenance.json" "d[\"prune\"]") && -n $(json "$STATE/maintenance.json" "d[\"check\"]") ]]'
check "every step in the log" \
    'for p in prepare snapshot forget prune verify; do grep -q "=== .* === $p: exit 0 ===" "$STATE/backup.log" || exit 1; done'
PRUNED=$(json "$STATE/maintenance.json" 'd["prune"]')
echo four > "$CASE/src/d.txt"; rc=$(BACKUP_SYNC_RESTIC_KEEP="--keep-last 2" run_restic)
echo five > "$CASE/src/e.txt"; rc=$(BACKUP_SYNC_RESTIC_KEEP="--keep-last 2" run_restic)
check "retention keeps the last two" '[[ $rc == 0 && $(snapshots "$CASE/dst/repo" "len(d)") == 2 ]]'
check "prune is not due again the same day" '[[ $(json "$STATE/maintenance.json" "d[\"prune\"]") == "$PRUNED" ]] && ! grep -q "prune: exit" <(tail -n 8 "$STATE/backup.log")'

# ── a copy job: the snapshots into a second repository ──
COPY_SRC="$CASE/dst"
new_case restic-copy
restic init -r "$CASE/dst/repo" --from-repo "$COPY_SRC/repo" --from-password-file "$RESTIC_PASSWORD_FILE" \
    --copy-chunker-params >/dev/null 2>&1
RSRC="$COPY_SRC" RDST="$CASE/dst" REXCLUDE=""
STATE="$CASE/dst/.mirror-backup"
rc=$(BACKUP_SYNC_RESTIC_KEEP="--keep-last 1" run_restic copy)
check "exits 0" '[[ $rc == 0 ]]'
check "copied the host's snapshots, then thinned them" '[[ $(snapshots "$CASE/dst/repo" "len(d), d[0][\"hostname\"]") == "(1, '"'"'test-host'"'"')" ]]'
check "history counts the copies" '[[ $(last_history "d[\"engine\"], d[\"snapshots_copied\"]") == "('"'"'restic'"'"', 2)" ]]'
check "the source repository is untouched" '[[ $(snapshots "$COPY_SRC/repo" "len(d)") == 2 ]]'

# ── no repository: never initialised by a run ──
new_case restic-no-repo
printf '%s\n' "$CASE/src" > "$CASE/paths"
RSRC="$CASE/paths" RDST="$CASE/dst"
rc=$(run_restic)
check "exits 1" '[[ $rc == 1 ]]'
check "says why" 'grep -q "No restic repository in $CASE/dst/repo" "$CASE/out"'
check "no repository created" '[[ ! -e "$CASE/dst/repo" ]]'

# ── a listed path on an unmounted mount point ──
new_case restic-unmounted-path
mkdir -p "$CASE/bind"
printf '%s\n%s\n' "$CASE/src" "$CASE/bind" > "$CASE/paths"
restic init -r "$CASE/dst/repo" >/dev/null 2>&1
echo "$CASE/elsewhere $CASE/bind none bind 0 0" > "$BACKUP_SYNC_FSTAB"
RSRC="$CASE/paths" RDST="$CASE/dst"
rc=$(run_restic)
check "refuses (exit 1)" '[[ $rc == 1 ]]'
check "says which" 'grep -q "Source not mounted: $CASE/bind" "$CASE/out"'
check "no snapshot taken" '[[ $(snapshots "$CASE/dst/repo" "len(d)") == 0 ]]'
: > "$BACKUP_SYNC_FSTAB"

# ── unreadable files: a snapshot without them, a warning, a suggestion ──
if (( EUID != 0 )); then
    new_case restic-unreadable
    echo secret > "$CASE/src/locked" && chmod 000 "$CASE/src/locked"
    printf '%s\n' "$CASE/src" > "$CASE/paths"
    restic init -r "$CASE/dst/repo" >/dev/null 2>&1
    RSRC="$CASE/paths" RDST="$CASE/dst"
    STATE="$CASE/dst/.mirror-backup"
    rc=$(run_restic)
    check "exits 0 (restic's 3 is a warning)" '[[ $rc == 0 && $(last_history "d[\"exit_code\"]") == 0 ]]'
    check "says so" '[[ $(json "$STATE/status.json" "d[\"error\"]") == *"could not be read"* ]]'
    check "suggests excluding it" '[[ $(json "$STATE/status.json" "d[\"suggested_excludes\"]") == "['"'"'$CASE/src/locked'"'"']" ]]'
    chmod 600 "$CASE/src/locked"
fi

# ── the pre-command ──
new_case restic-pre-command
printf '%s\n' "$CASE/src" > "$CASE/paths"
restic init -r "$CASE/dst/repo" >/dev/null 2>&1
RSRC="$CASE/paths" RDST="$CASE/dst"
STATE="$CASE/dst/.mirror-backup"
printf '#!/bin/sh\necho "metadata written"\n' > "$CASE/pre-ok" && chmod +x "$CASE/pre-ok"
printf '#!/bin/sh\necho "no disk" >&2\nexit 4\n' > "$CASE/pre-fail" && chmod +x "$CASE/pre-fail"
rc=$(BACKUP_SYNC_PRE_COMMAND="$CASE/pre-ok" run_restic)
check "runs first, output in the log" '[[ $rc == 0 ]] && grep -q "metadata written" "$STATE/backup.log"'
rc=$(BACKUP_SYNC_PRE_COMMAND="$CASE/pre-fail" run_restic)
check "a failing one ends the run" '[[ $rc == 4 && $(snapshots "$CASE/dst/repo" "len(d)") == 1 ]]'
check "and says so" '[[ $(json "$STATE/status.json" "d[\"state\"], d[\"error\"]") == "('"'"'error'"'"', '"'"'Preparation failed: no disk'"'"')" ]]'

# ── progress of a restic run ──
# A stand-in restic: `backup` prints a status message like the real one and
# waits for the test, the other commands do nothing.
new_case restic-phases
printf '%s\n' "$CASE/src" > "$CASE/paths"
mkdir -p "$CASE/dst/repo" "$CASE/gate" "$TMP/rbin" && : > "$CASE/dst/repo/config"
cat > "$TMP/rbin/restic" <<'EOF'
#!/usr/bin/env bash
step() { for _ in $(seq 600); do [[ -e "$GATE/$1" ]] && return; sleep 0.05; done; exit 99; }
[[ $1 == backup ]] || exit 0
printf '{"message_type":"status","percent_done":0.5,"total_files":40,"files_done":10,"total_bytes":2000,"bytes_done":1000,"seconds_remaining":75,"current_files":["/data/a \\"b\\".txt","/data/c"]}\n'
step seen
printf '{"message_type":"summary","files_new":3,"files_changed":1,"files_unmodified":36,"data_added":512,"total_files_processed":40,"total_bytes_processed":2000,"snapshot_id":"abc123"}\n'
EOF
chmod +x "$TMP/rbin/restic"
STATE="$CASE/dst/.mirror-backup"
PATH="$TMP/rbin:$PATH" GATE="$CASE/gate" BACKUP_SYNC_ENGINE=restic BACKUP_SYNC_RESTIC_CHECK_DAYS=0 BACKUP_SYNC_RESTIC_PRUNE_DAYS=0 \
    "$SYNC" test-job "$CASE/paths" "$CASE/dst" "" 0 >"$CASE/out" 2>&1 &
SYNC_PID=$!
snapshot='(d["state"], d["phase"], d["phase_unit"], d["phase_done"], d["phase_total"], d["progress"], d["eta"], d["files_transferred"], d["files_total"], d["current_file"]) == ("running", "snapshot", "bytes", 1000, 2000, 50.0, "0:01:15", 10, 40, "/data/a \"b\".txt")'
check "snapshot: bytes of the total, files, restic's ETA, the current file" 'wait_for "$snapshot"'
touch "$CASE/gate/seen"
wait "$SYNC_PID"; rc=$?
check "exits 0" '[[ $rc == 0 ]]'
check "history from the summary" \
    '[[ $(last_history "d[\"snapshot_id\"], d[\"files_transferred\"], d[\"files_total\"], d[\"data_added\"], d[\"bytes_total\"]") == "('"'"'abc123'"'"', 4, 40, 512, 2000)" ]]'
check "maintenance switched off: no prune, no check" '[[ ! -e "$STATE/maintenance.json" ]]'
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
