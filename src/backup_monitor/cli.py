"""Command line: `mirror-backup <command>`. No GTK is imported on this path.

    status [--json] [--watch]   every job's state (--watch: a JSON line per change);
                                exits 75 without reading while a suspend is under way
    control JOB ACTION          start, stop, pause or resume a job, of either scope
    dry-run JOB                 what a restic backup job's next snapshot would add
    sync-units                  make the systemd units match jobs.json
    resume [--system]           restart runs postponed or cut short, on any machine
    system apply|init|set-key|restic
                                system jobs (run by root) — see backup_monitor.system
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

from backup_monitor import paths, units
from backup_monitor.fsutil import atomic_write_text, trash
from backup_monitor.models.job import is_stale_active
from backup_monitor.services import snapshot as snap
from backup_monitor.services.job_manager import (JobManager, load_all_jobs, load_jobs,
                                                 load_system_jobs)
from backup_monitor.services.sleep_guard import (LOGIND_NAME, SleepGuard,
                                                 SuspendInProgress, guarded_read)

from gi.repository import GLib  # noqa: E402  (sleep_guard pins the version)

COMMANDS = ('status', 'control', 'dry-run', 'sync-units', 'resume', 'system', 'migrate-legacy')
ACTIONS = ('start', 'stop', 'pause', 'resume')

# `status` while a suspend is under way: nothing was read (EX_TEMPFAIL).
EXIT_SUSPENDING = 75
GUARD_WHO = 'Mirror Backup'


def main(argv: list[str]) -> int:
    if argv and argv[0] == '--regenerate-units':   # pre-0.6 spelling
        argv = ['sync-units', *argv[1:]]
    parser = argparse.ArgumentParser(prog='mirror-backup')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('status', help="every job's state")
    p.add_argument('--json', action='store_true', help='machine-readable output')
    p.add_argument('--watch', action='store_true',
                   help='keep running, print a JSON line whenever anything changes')
    p = sub.add_parser('control', help='start, stop, pause or resume a job')
    p.add_argument('job')
    p.add_argument('action', choices=ACTIONS)
    p = sub.add_parser('dry-run', help="what a restic backup job's next snapshot would add")
    p.add_argument('job')
    sub.add_parser('sync-units', help='make the systemd units match jobs.json')
    p = sub.add_parser('resume', help='restart postponed and interrupted runs')
    p.add_argument('--system', action='store_true',
                   help='the system jobs (root, at boot) instead of the user jobs')
    p = sub.add_parser('system', help='system jobs, run by root')
    ssub = p.add_subparsers(dest='system_command', required=True)
    q = ssub.add_parser('apply', help='check the edited system jobs and install them (root)')
    q.add_argument('--from', dest='source', type=Path, default=None,
                   help="the edited jobs (default: the owner's ~/.config/backup-sync/system)")
    q.add_argument('--owner', default='', help='the user they report to (default: who ran sudo/pkexec)')
    q.add_argument('--dry-run', action='store_true', help='only show what would change')
    q = ssub.add_parser('init', help="create a restic job's destination and repository (root)")
    q.add_argument('job')
    q = ssub.add_parser('set-key', help='store a repository password read from stdin (root)')
    q.add_argument('name')
    q.add_argument('--replace', action='store_true', help='replace a different existing key')
    q = ssub.add_parser('restic', help="run restic on a job's repository (root)")
    q.add_argument('job')
    q.add_argument('args', nargs=argparse.REMAINDER, help='restic arguments, after --')
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
    if args.command == 'control':
        return control(args.job, args.action)
    if args.command == 'dry-run':
        return dry_run(args.job)
    if args.command == 'sync-units':
        return sync_units()
    if args.command == 'resume':
        return resume(system=args.system)
    if args.command == 'system':
        return system_command(args)
    return migrate_legacy(args.source or paths.legacy_data_dir(),
                          retire=args.retire, dry_run=args.dry_run)


# ── status ──

def print_status(as_json: bool) -> int:
    # The destinations may be FUSE; a read in flight at the freeze aborts the
    # suspend. Hold logind off for the read, or do not read at all.
    try:
        with guarded_read(GUARD_WHO, 'Reading backup status'):
            data = snap.snapshot(load_all_jobs())
    except SuspendInProgress:
        print('mirror-backup: a suspend is under way, not reading the destinations',
              file=sys.stderr)
        return EXIT_SUSPENDING
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if not data['jobs']:
        print(f'No jobs in {data["config"]}')
        return 0
    for job in data['jobs']:
        st = job['status']
        line = f'{job["name"]:<16} {st["state"]:<12}'
        if st['state'] in ('running', 'scanning', 'paused'):
            parts = [st['phase_label'], st['progress_text'], st['speed'],
                     f'ETA {st["eta"]}' if st['eta'] else '', st['phase_count']]
            line += ' ' + '  '.join(p for p in parts if p)
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
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    return StatusWatch(sys.stdout).run()


class StatusWatch:
    """`status --watch`: print the snapshot as one JSON line now and after
    every change.

    State files are polled — they may sit on NFS, where inotify misses writes
    from other machines — every second while something runs and every five
    otherwise. Timer info (one systemctl call) refreshes each minute and
    whenever a job changes state.

    Polls run on a GLib main loop so a suspend is handled between two of them:
    a SleepGuard stops polling when one is announced and resumes after wake
    (services/sleep_guard.py says why that matters for FUSE destinations).
    """

    ACTIVE_S = 1
    IDLE_S = 5
    TIMERS_S = 60

    def __init__(self, out, *, guard_connection=None, logind_name: str = LOGIND_NAME):
        self._out = out
        self._loop = GLib.MainLoop()
        self._source: int | None = None
        self._signal_sources: list[int] = []
        self._asleep = False
        self._jobs: list[dict] = []
        self._jobs_mtime: tuple | None = None
        self._timers: dict = {}
        self._timers_at = 0.0
        self._last_line: str | None = None
        self._last_states: list | None = None
        self._guard = SleepGuard(self._on_sleep, self._on_wake, who=GUARD_WHO,
                                 why='Pausing the status watch before suspend',
                                 connection=guard_connection, name=logind_name)

    @property
    def polling(self) -> bool:
        return self._source is not None

    def run(self) -> int:
        self.start()
        try:
            self._loop.run()
        finally:
            self.stop()
        return 0

    def start(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self._signal_sources.append(
                GLib.unix_signal_add(GLib.PRIORITY_HIGH, signum, self._quit))
        self._guard.start()
        if not self._asleep:
            self._schedule(0)

    def stop(self) -> None:
        self._unschedule()
        for source in self._signal_sources:
            GLib.source_remove(source)
        self._signal_sources = []
        self._guard.stop()

    def poll(self) -> float:
        """One round: print what changed; returns the seconds until the next."""
        mtime = (_mtime(paths.jobs_file()), _mtime(paths.system_jobs_file()))
        if mtime != self._jobs_mtime:
            self._jobs, self._jobs_mtime = load_all_jobs(), mtime
            self._timers_at = 0.0
        data = snap.snapshot(self._jobs, timers={})
        states = [(j['id'], j['status']['state']) for j in data['jobs']]
        if states != self._last_states or time.monotonic() - self._timers_at > self.TIMERS_S:
            self._timers = snap.jobs_timer_info(self._jobs)
            self._timers_at = time.monotonic()
            self._last_states = states
        for job in data['jobs']:
            info = self._timers.get(job['id'], {})
            job['next_run'] = info.get('next_run', '')
            job['timer_enabled'] = info.get('timer_enabled', False)
        line = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if line != self._last_line:
            print(line, file=self._out, flush=True)
            self._last_line = line
        active = any(s in ('running', 'scanning', 'paused', 'queued') for _, s in states)
        return self.ACTIVE_S if active else self.IDLE_S

    def _tick(self) -> bool:
        self._source = None
        self._schedule(self.poll())
        return GLib.SOURCE_REMOVE

    def _schedule(self, seconds: float) -> None:
        self._unschedule()
        self._source = GLib.timeout_add(int(seconds * 1000), self._tick)

    def _unschedule(self) -> None:
        if self._source is not None:
            GLib.source_remove(self._source)
            self._source = None

    def _on_sleep(self) -> None:
        self._asleep = True
        self._unschedule()

    def _on_wake(self) -> None:
        self._asleep = False
        self._schedule(0)

    def _quit(self) -> bool:
        self._loop.quit()
        return GLib.SOURCE_CONTINUE     # stop() removes the signal sources


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
    _warn_system_drift()
    return 1 if report.errors else 0


def _warn_system_drift():
    """System jobs edited but not installed — only root can install them, so
    say so where the user sees it (this runs at login, from the resume unit)."""
    from backup_monitor import system
    changed = system.drift()
    if not changed:
        return
    print(f'system jobs changed but not installed — run: sudo {paths.SYSTEM_COMMAND} system apply')
    for path in changed:
        print(f'  {path}')
    subprocess.run(['gdbus', 'call', '--session', '--dest=org.freedesktop.Notifications',
                    '--object-path=/org/freedesktop/Notifications',
                    '--method=org.freedesktop.Notifications.Notify', 'Mirror Backup', '0',
                    'drive-harddisk', 'System backup jobs changed',
                    f'Install them with: sudo {paths.SYSTEM_COMMAND} system apply', '[]', '{}', '0'],
                   capture_output=True, timeout=10)


# ── control ──

def control(job_id: str, action: str) -> int:
    """One place that knows how to start, stop, pause and resume a job, of
    either scope; the app and both panels call it. Pause and resume signal
    backup-sync alone (--kill-whom=main): it stops and continues its child —
    restic, unlike rsync, would die of a SIGUSR1 of its own."""
    job = next((j for j in load_all_jobs() if j['id'] == job_id), None)
    if job is None:
        print(f'mirror-backup: no job {job_id!r} on this host', file=sys.stderr)
        return 2
    scope = units.job_scope(job)
    service = f'{job_id}.service'
    signal_arg = {'pause': 'USR1', 'resume': 'USR2'}

    def run(*args: str) -> subprocess.CompletedProcess:
        return _systemctl(*args, scope=scope)

    if action == 'start':
        result = run('start', '--no-block', service)
    elif action == 'stop':
        # backup-sync continues a paused run itself before stopping it.
        result = run('stop', '--no-block', service)
    else:
        result = run('kill', '--kill-whom=main', f'--signal={signal_arg[action]}', service)
    if result.returncode != 0:
        print(f'mirror-backup: {action} {job_id}: {result.stderr.strip()}', file=sys.stderr)
    return result.returncode


# ── resume ──

def _systemctl(*args: str, scope: str = 'user') -> subprocess.CompletedProcess:
    return subprocess.run(['systemctl', f'--{scope}', *args],
                          capture_output=True, text=True, timeout=30)


def resume(*, system: bool = False) -> int:
    """Start every job whose last run did not get to finish: postponed ones
    (a ``deferred`` marker) and ones whose machine went down under them — a
    crash, power loss, or simply the other operating system being booted.
    ``system``: the system jobs (run by root from a system unit at boot)."""
    scope = 'system' if system else 'user'
    jobs = load_system_jobs() if system else load_jobs()
    for job in (j for j in jobs if units.runs_here(j)):
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
        load = _systemctl('show', '-P', 'LoadState', f'{job_id}.service', scope=scope).stdout.strip()
        if load != 'loaded':
            print(f'{job_id}: unit not loaded ({load or "unknown"}), not restarting its {reason} run')
            continue
        print(f'{job_id}: restarting {reason} run')
        _systemctl('start', '--no-block', f'{job_id}.service', scope=scope)
    return 0


# ── dry run ──

def dry_run(job_id: str) -> int:
    from backup_monitor import system
    job = next((j for j in load_all_jobs() if j['id'] == job_id), None)
    if job is None:
        print(f'mirror-backup: no job {job_id!r} on this host', file=sys.stderr)
        return 2
    try:
        return system.dry_run(job)
    except system.SystemError_ as e:
        print(f'mirror-backup dry-run: {e}', file=sys.stderr)
        return 1


# ── system jobs ──

def system_command(args) -> int:
    from backup_monitor import system
    try:
        if args.system_command == 'apply':
            return system.apply(args.source, dry_run=args.dry_run, owner=args.owner)
        if args.system_command == 'init':
            return system.init(args.job)
        if args.system_command == 'set-key':
            return system.set_key(args.name, replace=args.replace)
        rest = args.args[1:] if args.args[:1] == ['--'] else args.args
        return system.restic_exec(args.job, rest)
    except system.SystemError_ as e:
        print(f'mirror-backup system: {e}', file=sys.stderr)
        return 1


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
