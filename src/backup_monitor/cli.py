"""Command line: `mirror-backup <command>`. No GTK is imported on this path.

    status [--json] [--watch]   every job's state (--watch: a JSON line per change)
    sync-units                  make the systemd units match jobs.json
    resume                      restart runs postponed or cut short, on any machine
    migrate-legacy [--from DIR] [--retire] [--dry-run]
                                move pre-0.6 state (~/.local/share/backup-sync)
                                into each job's destination
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from backup_monitor import paths
from backup_monitor.fsutil import atomic_write_text, trash
from backup_monitor.models.job import is_stale_active
from backup_monitor.services import snapshot as snap
from backup_monitor.services.job_manager import JobManager, load_jobs

COMMANDS = ('status', 'sync-units', 'resume', 'migrate-legacy')


def main(argv: list[str]) -> int:
    if argv and argv[0] == '--regenerate-units':   # pre-0.6 spelling
        argv = ['sync-units', *argv[1:]]
    parser = argparse.ArgumentParser(prog='mirror-backup')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('status', help="every job's state")
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--watch', action='store_true',
                   help='keep running, print a JSON line whenever anything changes')
    sub.add_parser('sync-units', help='make the systemd units match jobs.json')
    sub.add_parser('resume', help='restart postponed and interrupted runs')
    p = sub.add_parser('migrate-legacy', help='move pre-0.6 state into the destinations')
    p.add_argument('--from', dest='source', type=Path, default=None,
                   help='legacy data dir (default: $XDG_DATA_HOME/backup-sync)')
    p.add_argument('--retire', action='store_true',
                   help='move the legacy dir to the trash once every job is migrated')
    p.add_argument('--dry-run', action='store_true')

    args = parser.parse_args(argv)
    if args.command == 'status':
        if args.watch:
            return watch_status()
        return print_status(as_json=args.json)
    if args.command == 'sync-units':
        return sync_units()
    if args.command == 'resume':
        return resume()
    return migrate_legacy(args.source or paths.legacy_data_dir(),
                          retire=args.retire, dry_run=args.dry_run)


# ── status ──

def print_status(as_json: bool) -> int:
    data = snap.snapshot(load_jobs())
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if not data['jobs']:
        print(f'No jobs in {data["config"]}')
        return 0
    for job in data['jobs']:
        st = job['status']
        line = f'{job["name"]:<16} {st["state"]:<12}'
        if st['state'] in ('running', 'paused'):
            line += f' {st["progress"]:.0f}%  {st["speed"]}  ETA {st["eta"]}'
        elif st['state'] in ('error', 'unavailable') and st['error']:
            line += f' {st["error"]}'
        last = job['last_run']
        if last:
            where = f' on {last["host"]}' if last['host'] else ''
            result = 'ok' if last['exit_code'] == 0 else f'exit {last["exit_code"]}'
            line += f'  last {last["started"]}{where} ({result})'
        if job['next_run']:
            line += f'  next {job["next_run"]}'
        print(line)
    return 0


def watch_status() -> int:
    """Print the snapshot as one JSON line now and after every change.

    State files are polled — they may sit on NFS, where inotify misses writes
    from other machines — every second while something runs and every five
    otherwise. Timer info (one systemctl call) refreshes each minute and
    whenever a job changes state.
    """
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    jobs, jobs_mtime = load_jobs(), _mtime(paths.jobs_file())
    timers, timers_at = snap.timer_info([j['id'] for j in jobs]), time.monotonic()
    last_line, last_states = None, None
    while True:
        mtime = _mtime(paths.jobs_file())
        if mtime != jobs_mtime:
            jobs, jobs_mtime = load_jobs(), mtime
            timers_at = 0.0
        data = snap.snapshot(jobs, timers={})
        states = [(j['id'], j['status']['state']) for j in data['jobs']]
        if states != last_states or time.monotonic() - timers_at > 60:
            timers, timers_at = snap.timer_info([j['id'] for j in jobs]), time.monotonic()
            last_states = states
        for job in data['jobs']:
            info = timers.get(job['id'], {})
            job['next_run'] = info.get('next_run', '')
            job['timer_enabled'] = info.get('timer_enabled', False)
        line = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if line != last_line:
            print(line, flush=True)
            last_line = line
        active = any(s in ('running', 'scanning', 'paused', 'queued') for _, s in states)
        time.sleep(1 if active else 5)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ── units ──

def sync_units() -> int:
    report = JobManager().sync_units()
    if not report.changed and not report.unmanaged and not report.errors:
        print('units up to date')
    return 1 if report.errors else 0


# ── resume ──

def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', '--user', *args],
                          capture_output=True, text=True, timeout=30)


def resume() -> int:
    """Start every job whose last run did not get to finish: postponed ones
    (a ``deferred`` marker) and ones whose machine went down under them — a
    crash, power loss, or simply the other operating system being booted."""
    for job in load_jobs():
        job_id, destination = job['id'], job['destination']
        state = paths.job_paths(destination)
        if not state.available:
            print(f'{job_id}: destination not available, nothing to resume')
            continue
        if state.deferred.exists():
            reason = 'postponed'
        elif is_stale_active(destination):
            reason = 'interrupted'
        else:
            continue
        load = _systemctl('show', '-P', 'LoadState', f'{job_id}.service').stdout.strip()
        if load != 'loaded':
            print(f'{job_id}: unit not loaded ({load or "unknown"}), not restarting its {reason} run')
            continue
        print(f'{job_id}: restarting {reason} run')
        _systemctl('start', '--no-block', f'{job_id}.service')
    return 0


# ── migration from pre-0.6 ──

@dataclass
class MigrationResult:
    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # destination not available
    actions: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.skipped


def migrate_legacy(legacy: Path, *, retire: bool = False, dry_run: bool = False) -> int:
    if not legacy.is_dir():
        print(f'no legacy state in {legacy}')
        return 0
    result = migrate_jobs(load_jobs(), legacy, dry_run=dry_run)
    for line in result.actions:
        print(line)
    for job_id in result.skipped:
        print(f'{job_id}: destination not available — not migrated')
    if retire:
        if not result.complete:
            print(f'kept {legacy}: not every job could be migrated')
            return 1
        if dry_run:
            print(f'would move {legacy} to the trash')
        else:
            print(f'{legacy} → {trash(legacy)}')
    return 0 if result.complete else 1


def migrate_jobs(jobs: list[dict], legacy: Path, *, dry_run: bool = False) -> MigrationResult:
    """Merge each job's legacy status, history, log and postponed marker into
    ``<destination>/.mirror-backup``. Idempotent: history is merged by run,
    and nothing already in the destination is overwritten."""
    result = MigrationResult()
    for job in jobs:
        job_id = job['id']
        target = paths.job_paths(job['destination'])
        old = {
            'status': legacy / 'status' / f'{job_id}.json',
            'deferred': legacy / 'status' / f'{job_id}.deferred',
            'history': legacy / 'history' / f'{job_id}.jsonl',
            'log': legacy / 'logs' / f'{job_id}.log',
        }
        if not any(p.exists() for p in old.values()):
            continue
        if not target.available:
            result.skipped.append(job_id)
            continue
        if not dry_run:
            target.state_dir.mkdir(exist_ok=True)

        if old['history'].is_file():
            merged, added = _merge_history(old['history'], target.history)
            if added:
                result.actions.append(f'{job_id}: history +{added} run(s) → {target.history}')
                if not dry_run:
                    atomic_write_text(target.history, merged)

        for key in ('status', 'deferred'):
            dst = target.status if key == 'status' else target.deferred
            if old[key].is_file() and not dst.exists():
                result.actions.append(f'{job_id}: {key} → {dst}')
                if not dry_run:
                    atomic_write_text(dst, old[key].read_text(encoding='utf-8', errors='replace'))

        if old['log'].is_file() and not _continues(target.log, old['log']):
            dst = target.log if not target.log.exists() else target.log.with_name('backup.log.pre-0.6')
            if not dst.exists():
                result.actions.append(f'{job_id}: log → {dst}')
                if not dry_run:
                    shutil.copyfile(old['log'], dst)
        result.migrated.append(job_id)
    return result


def _continues(current: Path, legacy: Path) -> bool:
    """``current`` starts with all of ``legacy`` — it was migrated from it
    before (by another install sharing the destination) and has grown since."""
    try:
        size = legacy.stat().st_size
        with open(current, 'rb') as a, open(legacy, 'rb') as b:
            return a.read(size) == b.read()
    except OSError:
        return False


def _merge_history(legacy: Path, current: Path) -> tuple[str, int]:
    """Union of both files' runs, keyed by start and finish, oldest first."""
    def entries(path: Path) -> dict[tuple, str]:
        out = {}
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except FileNotFoundError:
            return out
        for line in lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[(data.get('started', ''), data.get('finished', ''))] = line.strip()
        return out

    have = entries(current)
    old = entries(legacy)
    added = sum(1 for k in old if k not in have)
    merged = {**old, **have}
    text = ''.join(merged[k] + '\n' for k in sorted(merged))
    return text, added


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
