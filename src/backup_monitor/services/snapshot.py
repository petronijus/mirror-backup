"""One consistent picture of every job — what `mirror-backup status --json`
prints and what the panel widgets (GNOME extension, Omarchy bar) display.

Keeping this in one place means the rules for where state lives and when a run
is really alive (models.job.read_status) exist once, not once per desktop.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

from backup_monitor import paths, units
from backup_monitor.models.job import read_status
from backup_monitor.models.job_history import last_run
from backup_monitor.services.job_manager import source_label


def timer_info(job_ids: list[str], scope: str = 'user') -> dict[str, dict]:
    """Next elapse and enablement of each job's timer, from one systemctl call."""
    if not job_ids:
        return {}
    try:
        result = subprocess.run(
            ['systemctl', f'--{scope}', 'show', '--timestamp=unix',
             '--property=Id,NextElapseUSecRealtime,UnitFileState,ActiveState',
             *[f'{j}.timer' for j in job_ids]],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    info: dict[str, dict] = {}
    for block in result.stdout.split('\n\n'):
        props = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        unit = props.get('Id', '')
        if not unit.endswith('.timer'):
            continue
        info[unit[:-len('.timer')]] = {
            'next_run': _unix_to_iso(props.get('NextElapseUSecRealtime', '')),
            'timer_enabled': props.get('UnitFileState') == 'enabled',
            'timer_active': props.get('ActiveState') == 'active',
        }
    return info


def jobs_timer_info(jobs: list[dict]) -> dict[str, dict]:
    """timer_info for jobs of both scopes: one systemctl call per scope."""
    info: dict[str, dict] = {}
    for scope in ('user', 'system'):
        info.update(timer_info([j['id'] for j in jobs if units.job_scope(j) == scope], scope))
    return info


def _unix_to_iso(value: str) -> str:
    """'@1759222800' → local ISO 8601 with offset; '' when there is none."""
    value = value.strip()
    if not value.startswith('@'):
        return ''
    try:
        return datetime.fromtimestamp(int(value[1:])).astimezone().isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return ''


def job_snapshot(job: dict, timers: dict[str, dict], boot: str) -> dict:
    destination = job.get('destination', '')
    status = read_status(destination, current_boot=boot)
    entry = last_run(destination) if status.state != 'unavailable' else None
    timer = timers.get(job['id'], {})
    return {
        'id': job['id'],
        'name': job.get('name', job['id']),
        'service': f'{job["id"]}.service',
        'scope': units.job_scope(job),
        'engine': units.job_engine(job),
        'source': source_label(job),
        'destination': destination,
        'schedule': job.get('schedule', {}).get('expression', '')
        if job.get('schedule', {}).get('type', 'calendar') != 'manual' else '',
        'enabled': bool(job.get('enabled', True)),
        'status': status.to_dict(),
        'last_run': None if entry is None else {
            'started': entry.started,
            'finished': entry.finished,
            'exit_code': entry.exit_code,
            'duration_sec': entry.duration_sec,
            'host': entry.host,
        },
        'next_run': timer.get('next_run', ''),
        'timer_enabled': timer.get('timer_enabled', False),
    }


def snapshot(jobs: list[dict], timers: dict[str, dict] | None = None) -> dict:
    boot = paths.boot_id()
    if timers is None:
        timers = jobs_timer_info(jobs)
    return {
        'host': paths.host_name(),
        'config': str(paths.jobs_file()),
        'system_config': str(paths.system_jobs_file()),
        'jobs': [job_snapshot(j, timers, boot) for j in jobs],
    }


def summary_state(snap: dict) -> str:
    """The one state a panel icon shows: error > active > queued > deferred >
    unavailable > idle."""
    states = [j['status']['state'] for j in snap.get('jobs', [])]
    for wanted in ('error', 'running', 'scanning', 'paused', 'queued', 'deferred', 'unavailable'):
        if wanted in states:
            return wanted
    return 'idle'
