"""Backup job model and the rules for reading a job's live status."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from backup_monitor import paths

ACTIVE_STATES = ('running', 'scanning', 'paused', 'queued')


@dataclass
class BackupStatus:
    """Live status snapshot read from the job's status file."""
    state: str = 'idle'
    progress: float = 0
    speed: str = ''
    eta: str = ''
    current_file: str = ''
    files_transferred: int = 0
    files_total: int = 0
    pid: int = 0
    rsync_pid: int = 0
    started: str = ''
    updated: str = ''
    error: str = ''
    scan_read: str = ''
    consecutive_failures: int = 0
    suggested_excludes: list[str] = field(default_factory=list)
    # 'suspend' or 'shutdown' while postponed; 'interrupted' for a run that
    # died with the machine it ran on (crash, power loss, or the other OS).
    deferred_reason: str = ''
    host: str = ''       # machine that wrote the status
    boot_id: str = ''    # boot it was written in; empty for pre-0.6 status files

    @property
    def deferred_note(self) -> str:
        """When a postponed run is going to start again."""
        if self.deferred_reason == 'suspend':
            return 'Restarts after wake'
        if self.deferred_reason == 'interrupted':
            return 'Interrupted — restarts at next start'
        return 'Restarts at next start'

    @property
    def active(self) -> bool:
        return self.state in ('running', 'scanning', 'paused')

    def to_dict(self) -> dict:
        return {
            'state': self.state,
            'progress': self.progress,
            'speed': self.speed,
            'eta': self.eta,
            'current_file': self.current_file,
            'files_transferred': self.files_transferred,
            'files_total': self.files_total,
            'started': self.started,
            'updated': self.updated,
            'error': self.error,
            'scan_read': self.scan_read,
            'consecutive_failures': self.consecutive_failures,
            'suggested_excludes': list(self.suggested_excludes),
            'deferred_reason': self.deferred_reason,
            'host': self.host,
        }


def read_status(destination: str, *, current_boot: str | None = None) -> BackupStatus:
    """The status of the job mirroring into ``destination``.

    The file is shared by every machine that mounts the destination, so an
    active state is only believed when it was written in this boot by a
    backup-sync process that is still alive:

    * destination missing        → ``unavailable`` (not mounted, or gone)
    * no status file yet         → ``idle`` (never ran)
    * active, other/unknown boot → ``deferred``/``interrupted``: the machine
      that ran it went down; ``mirror-backup resume`` restarts it
    * active, this boot, dead    → ``error``: the process ended without
      writing a final status (SIGKILL, OOM)
    """
    job = paths.job_paths(destination)
    if not job.available:
        return BackupStatus(state='unavailable',
                            error=f'Destination not available: {destination}')
    try:
        data = json.loads(job.status.read_text())
    except FileNotFoundError:
        return BackupStatus()
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return BackupStatus(state='error', error=f'Unreadable status file: {e}')

    st = BackupStatus(
        state=str(data.get('state', 'idle')),
        progress=_number(data.get('progress')),
        speed=str(data.get('speed', '')),
        eta=str(data.get('eta', '')),
        current_file=str(data.get('current_file', '')),
        files_transferred=int(_number(data.get('files_transferred'))),
        files_total=int(_number(data.get('files_total'))),
        pid=int(_number(data.get('pid'))),
        rsync_pid=int(_number(data.get('rsync_pid'))),
        started=str(data.get('started', '')),
        updated=str(data.get('updated', '')),
        error=str(data.get('error', '')),
        scan_read=str(data.get('scan_read', '')),
        consecutive_failures=int(_number(data.get('consecutive_failures'))),
        suggested_excludes=[str(s) for s in data.get('suggested_excludes', []) or []],
        deferred_reason=str(data.get('deferred_reason', '')),
        host=str(data.get('host', '')),
        boot_id=str(data.get('boot_id', '')),
    )

    if st.state in ACTIVE_STATES:
        boot = paths.boot_id() if current_boot is None else current_boot
        if not st.boot_id or st.boot_id != boot:
            _clear_progress(st)
            st.state = 'deferred'
            st.deferred_reason = 'interrupted'
        elif not _is_backup_sync(st.pid):
            _clear_progress(st)
            st.state = 'error'
            st.error = 'Backup process ended unexpectedly'
    return st


def is_stale_active(destination: str, *, current_boot: str | None = None) -> bool:
    """An active status left behind by another boot — a run to restart."""
    return read_status(destination, current_boot=current_boot).deferred_reason == 'interrupted'


def _clear_progress(st: BackupStatus):
    st.progress = 0
    st.speed = ''
    st.eta = ''
    st.current_file = ''
    st.scan_read = ''


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _is_backup_sync(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        return b'backup-sync' in Path(f'/proc/{pid}/cmdline').read_bytes()
    except OSError:
        return False


@dataclass
class BackupJob:
    """A backup job as the UI works with it."""
    id: str
    label: str
    service_name: str
    source: str = ''
    destination: str = ''
    exclude_file: str = ''
    archive_days: int = 0
    description: str = ''
    status: BackupStatus = field(default_factory=BackupStatus)

    # Scheduling: next_run from this machine's timer, last_run from the job's
    # shared history (so a run done by another machine counts).
    next_run: str = ''
    last_run: str = ''
    last_run_host: str = ''
    timer_schedule: str = ''

    def read_status(self) -> BackupStatus:
        self.status = read_status(self.destination)
        return self.status

    @staticmethod
    def discover_from_systemd() -> list[BackupJob]:
        """Scan systemd user units and return all backup-* jobs (first-run
        migration for installs that predate jobs.json)."""
        jobs = []
        unit_dir = paths.systemd_user_dir()
        if not unit_dir.is_dir():
            return jobs

        for service_file in sorted(unit_dir.glob('backup-*.service')):
            job_id = service_file.stem
            content = service_file.read_text()

            source = ''
            destination = ''
            exclude_file = ''
            archive_days = 0
            description = ''

            for line in content.splitlines():
                line = line.strip()
                if line.startswith('Description='):
                    description = line.split('=', 1)[1]
                elif line.startswith('ExecStart='):
                    # ExecStart=%h/.local/bin/backup-sync job-id /src/ /dst/ [exclude] [days]
                    parts = line.split('=', 1)[1].split()
                    if len(parts) >= 4:
                        source = parts[2]
                        destination = parts[3]
                    if len(parts) >= 5:
                        exclude_file = parts[4].replace('%h', str(Path.home()))
                        if exclude_file in ('""', "''"):
                            exclude_file = ''
                    if len(parts) >= 6:
                        try:
                            archive_days = int(parts[5])
                        except ValueError:
                            pass

            label = job_id.replace('backup-', '').replace('-', ' ').title()

            timer_schedule = ''
            timer_file = unit_dir / f'{job_id}.timer'
            if timer_file.is_file():
                for line in timer_file.read_text().splitlines():
                    if line.strip().startswith('OnCalendar='):
                        timer_schedule = line.strip().split('=', 1)[1]
                        break

            source = source.replace('%h', str(Path.home()))
            destination = destination.replace('%h', str(Path.home()))

            jobs.append(BackupJob(
                id=job_id,
                label=label,
                service_name=f'{job_id}.service',
                source=source,
                destination=destination,
                exclude_file=exclude_file,
                archive_days=archive_days,
                description=description,
                timer_schedule=timer_schedule,
            ))

        return jobs
