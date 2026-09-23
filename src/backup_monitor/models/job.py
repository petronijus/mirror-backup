"""Backup job model — represents a single backup job discovered from systemd units."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

STATUS_DIR = Path.home() / '.local' / 'share' / 'backup-sync' / 'status'
SYSTEMD_USER_DIR = Path.home() / '.config' / 'systemd' / 'user'


@dataclass
class BackupStatus:
    """Live status snapshot read from the JSON status file."""
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
    finished: str = ''
    error: str = ''
    scan_read: str = ''
    source: str = ''
    destination: str = ''
    consecutive_failures: int = 0
    suggested_excludes: list[str] = field(default_factory=list)
    deferred_reason: str = ''  # 'suspend' or 'shutdown' while state == 'deferred'

    @property
    def deferred_note(self) -> str:
        """When a postponed run is going to start again."""
        if self.deferred_reason == 'suspend':
            return 'Restarts after wake'
        return 'Restarts at next start'


@dataclass
class BackupJob:
    """A backup job discovered from systemd service/timer units."""
    id: str
    label: str
    service_name: str
    source: str = ''
    destination: str = ''
    exclude_file: str = ''
    archive_days: int = 0
    description: str = ''
    status: BackupStatus = field(default_factory=BackupStatus)

    # Timer info (populated by systemd_service)
    next_run: str = ''
    last_run: str = ''
    timer_schedule: str = ''

    def read_status(self) -> BackupStatus:
        """Read the JSON status file for this job."""
        path = STATUS_DIR / f'{self.id}.json'
        try:
            data = json.loads(path.read_text())
            st = BackupStatus(
                state=data.get('state', 'idle'),
                progress=data.get('progress', 0),
                speed=data.get('speed', ''),
                eta=data.get('eta', ''),
                current_file=data.get('current_file', ''),
                files_transferred=data.get('files_transferred', 0),
                files_total=data.get('files_total', 0),
                pid=data.get('pid', 0),
                rsync_pid=data.get('rsync_pid', 0),
                started=data.get('started', ''),
                finished=data.get('finished', ''),
                error=data.get('error', ''),
                scan_read=data.get('scan_read', ''),
                source=data.get('source', ''),
                destination=data.get('destination', ''),
                consecutive_failures=int(data.get('consecutive_failures', 0)),
                suggested_excludes=list(data.get('suggested_excludes', [])),
                deferred_reason=data.get('deferred_reason', ''),
            )
            # Validate PID liveness for active states
            if st.state in ('running', 'scanning', 'paused', 'queued') and not _is_alive(st.pid):
                st.state = 'idle'
                st.progress = 0
                st.speed = ''
                st.eta = ''
                st.current_file = ''
                st.error = ''
            self.status = st
            return st
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.status = BackupStatus()
            return self.status

    @staticmethod
    def discover_from_systemd() -> list[BackupJob]:
        """Scan systemd user units and return all backup-* jobs."""
        jobs = []
        if not SYSTEMD_USER_DIR.is_dir():
            return jobs

        for service_file in sorted(SYSTEMD_USER_DIR.glob('backup-*.service')):
            job_id = service_file.stem  # e.g. 'backup-documents'
            content = service_file.read_text()

            # Parse ExecStart line to extract args
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
                    # Skip the binary path and job name
                    if len(parts) >= 4:
                        source = parts[2]
                        destination = parts[3]
                    if len(parts) >= 5:
                        exclude_file = parts[4].replace('%h', str(Path.home()))
                        if exclude_file == '""' or exclude_file == "''":
                            exclude_file = ''
                    if len(parts) >= 6:
                        try:
                            archive_days = int(parts[5])
                        except ValueError:
                            pass

            # Derive a friendly label from the job id
            label = job_id.replace('backup-', '').replace('-', ' ').title()

            # Read timer schedule if available
            timer_schedule = ''
            timer_file = SYSTEMD_USER_DIR / f'{job_id}.timer'
            if timer_file.is_file():
                for line in timer_file.read_text().splitlines():
                    if line.strip().startswith('OnCalendar='):
                        timer_schedule = line.strip().split('=', 1)[1]
                        break

            # Expand %h in source/destination
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


def _is_alive(pid: int) -> bool:
    """Check if a backup-sync process is alive by reading /proc/<pid>/cmdline."""
    if not pid or pid <= 0:
        return False
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()
        return b'backup-sync' in cmdline
    except OSError:
        return False
