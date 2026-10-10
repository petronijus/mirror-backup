"""Job manager — CRUD for jobs.json, keeps the systemd units in step, handles migration."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from backup_monitor import paths, units
from backup_monitor.fsutil import atomic_write_text
from backup_monitor.models.job import BackupJob

DEFAULT_RSYNC_OPTIONS = {
    'delete_mode': 'before',   # before, during, after, disabled
    'compress': False,          # --compress (useful for remote/slow links)
    'checksum': False,          # --checksum (verify by content, not time+size)
    'hard_links': False,        # --hard-links (preserve hard links)
    'xattrs': False,            # --xattrs (preserve extended attributes)
    'acls': False,              # --acls (preserve ACLs)
    'partial': False,           # --partial (keep partially transferred files)
    'update': False,            # --update (skip files newer on destination)
    'max_size': '',             # --max-size (e.g. "500M", "2G")
    'min_size': '',             # --min-size (e.g. "1K")
}


# Keys a job may carry beyond the rsync basics; copied as given.
_EXTRA_KEYS = ('engine', 'scope', 'restic', 'pre_command', 'hosts', 'run_after')


class JobManager:
    """Manages backup job configuration, systemd unit generation, and persistence.

    User jobs live in jobs.json and their units are generated right here.
    System jobs are edited in the overlay (``system/jobs.json``); installing
    them takes root — ``system_apply_command()`` is what the app runs for it,
    through pkexec, after saving one.
    """

    def __init__(self):
        self._jobs: list[dict] = []
        self._system_jobs: list[dict] = load_system_source()
        self._load_or_migrate()

    def _load_or_migrate(self):
        """Load jobs.json or migrate from existing systemd units on first run."""
        if paths.jobs_file().is_file():
            self._load()
        else:
            self._migrate_from_systemd()

    def _load(self):
        self._jobs = load_jobs()

    def _save(self):
        """Write jobs.json — atomically: it may be shared with other installs."""
        data = {
            'version': 1,
            'jobs': self._jobs,
        }
        atomic_write_text(paths.jobs_file(),
                          json.dumps(data, indent=2, ensure_ascii=False) + '\n')

    def _migrate_from_systemd(self):
        """First-run migration: read existing systemd units and create jobs.json."""
        discovered = BackupJob.discover_from_systemd()
        self._jobs = []
        for job in discovered:
            self._jobs.append({
                'id': job.id,
                'name': job.label,
                'source': job.source,
                'destination': job.destination,
                'exclude_file': job.exclude_file,
                'archive_days': job.archive_days,
                'description': job.description,
                'schedule': {
                    'type': 'calendar',
                    'expression': job.timer_schedule or 'daily',
                    'randomized_delay_sec': 0,
                },
                'bandwidth_limit_kbps': 0,
                'nice': 10,
                'io_priority': 7,
                'rsync_options': DEFAULT_RSYNC_OPTIONS.copy(),
                'notifications': {
                    'on_start': True,
                    'on_complete': True,
                    'on_error': True,
                },
                'enabled': True,
                'created': datetime.now().isoformat(),
            })
        self._save()

    @property
    def jobs(self) -> list[dict]:
        return self._jobs

    @property
    def system_jobs(self) -> list[dict]:
        """The system jobs as edited (not necessarily installed yet)."""
        return self._system_jobs

    def get_job(self, job_id: str) -> Optional[dict]:
        for j in (*self._jobs, *self._system_jobs):
            if j['id'] == job_id:
                return j
        return None

    def _save_system(self):
        path = paths.system_source_dir() / 'jobs.json'
        atomic_write_text(path, json.dumps({'version': 1, 'jobs': self._system_jobs},
                                           indent=2, ensure_ascii=False) + '\n')

    @staticmethod
    def system_apply_command() -> list[str]:
        """Installs the edited system jobs as root (prompting through polkit)."""
        import getpass
        return ['pkexec', paths.SYSTEM_COMMAND, 'system', 'apply', '--owner', getpass.getuser(),
                '--from', str(paths.system_source_dir())]

    def create_job(self, job_data: dict) -> str:
        """Create a new backup job. Returns the job ID."""
        # Generate ID from name
        job_id = 'backup-' + re.sub(r'[^a-z0-9]+', '-', job_data['name'].lower()).strip('-')

        # Ensure unique — across both scopes, whose units may share a name
        existing_ids = {j['id'] for j in (*self._jobs, *self._system_jobs)}
        base_id = job_id
        counter = 2
        while job_id in existing_ids:
            job_id = f'{base_id}-{counter}'
            counter += 1

        job = {
            'id': job_id,
            'name': job_data['name'],
            'source': job_data['source'].rstrip('/') + '/' if job_data.get('source') else '',
            'destination': job_data['destination'].rstrip('/') + '/',
            'exclude_file': job_data.get('exclude_file', ''),
            'archive_days': job_data.get('archive_days', 0),
            'description': job_data.get('description', f'Backup {job_data["name"]}'),
            'schedule': job_data.get('schedule', {
                'type': 'calendar',
                'expression': 'daily',
                'randomized_delay_sec': 0,
            }),
            'bandwidth_limit_kbps': job_data.get('bandwidth_limit_kbps', 0),
            'nice': job_data.get('nice', 10),
            'io_priority': job_data.get('io_priority', 7),
            'rsync_options': job_data.get('rsync_options', DEFAULT_RSYNC_OPTIONS.copy()),
            'notifications': job_data.get('notifications', {
                'on_start': True,
                'on_complete': True,
                'on_error': True,
            }),
            'enabled': True,
            'created': datetime.now().isoformat(),
        }
        job.update({k: job_data[k] for k in _EXTRA_KEYS if k in job_data})
        if units.job_engine(job) == 'restic':
            job.pop('rsync_options', None)
            job.pop('archive_days', None)

        if units.job_scope(job) == 'system':
            self._system_jobs.append(job)
            self._save_system()
            return job_id
        self._jobs.append(job)
        self._save()
        self.sync_units()
        return job_id

    def update_job(self, job_id: str, job_data: dict):
        """Update an existing backup job. A system job is saved to the overlay;
        installing it is the caller's (system_apply_command)."""
        for i, j in enumerate(self._system_jobs):
            if j['id'] == job_id:
                job_data['id'] = job_id
                job_data['created'] = j.get('created', datetime.now().isoformat())
                job_data['scope'] = 'system'
                self._system_jobs[i] = job_data
                self._save_system()
                return
        for i, j in enumerate(self._jobs):
            if j['id'] == job_id:
                # Preserve id, created
                job_data['id'] = job_id
                job_data['created'] = j.get('created', datetime.now().isoformat())
                if job_data.get('source'):
                    job_data['source'] = job_data['source'].rstrip('/') + '/'
                if 'destination' in job_data:
                    job_data['destination'] = job_data['destination'].rstrip('/') + '/'
                self._jobs[i] = job_data
                self._save()
                self.sync_units()
                return
        raise ValueError(f'Job not found: {job_id}')

    def delete_job(self, job_id: str):
        """Delete a backup job; its units go to the trash. A system job's go
        when the system jobs are installed again (system_apply_command)."""
        if any(j['id'] == job_id for j in self._system_jobs):
            self._system_jobs = [j for j in self._system_jobs if j['id'] != job_id]
            self._save_system()
            return
        self._jobs = [j for j in self._jobs if j['id'] != job_id]
        self._save()
        self.sync_units(remove_ids=(job_id,))

    def toggle_job(self, job_id: str, enabled: bool):
        """Enable or disable a job's timer."""
        for j in self._system_jobs:
            if j['id'] == job_id:
                j['enabled'] = enabled
                self._save_system()
                return
        for j in self._jobs:
            if j['id'] == job_id:
                j['enabled'] = enabled
                self._save()
                self.sync_units()
                return

    def to_backup_jobs(self) -> list[BackupJob]:
        """The jobs the dashboard shows: the user's, and the system jobs as
        installed (what actually runs) — those of this host."""
        result = []
        for j in (*self._jobs, *load_system_jobs()):
            if not units.runs_here(j):
                continue
            schedule = j.get('schedule', {})
            result.append(BackupJob(
                id=j['id'],
                label=j['name'],
                service_name=f'{j["id"]}.service',
                scope=units.job_scope(j),
                engine=units.job_engine(j),
                source=source_label(j),
                destination=j.get('destination', ''),
                exclude_file=j.get('exclude_file', ''),
                archive_days=j.get('archive_days', 0),
                description=j.get('description', ''),
                timer_schedule=schedule.get('expression', ''),
            ))
        return result

    # ── systemd units ──

    def sync_units(self, remove_ids: tuple[str, ...] = ()) -> units.SyncReport:
        """Bring the systemd units in line with the jobs (see units.sync_units)."""
        report = units.sync_units(self._jobs, remove_ids=remove_ids)
        for line in report.lines():
            print(f'[BackupMonitor] units {line}')
        return report


# ── Exclusion file helpers ──

def read_exclusions(exclude_file: str) -> list[dict]:
    """Read an exclude file and return list of {pattern, enabled, comment}."""
    path = Path(paths.resolve_config_path(exclude_file))
    if not exclude_file or not path.is_file():
        return []

    entries = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            # Check if it's a commented-out pattern (disabled)
            pattern = stripped.lstrip('#').strip()
            if pattern and not pattern.startswith(' '):
                entries.append({'pattern': pattern, 'enabled': False})
            # Skip pure comments
        else:
            entries.append({'pattern': stripped, 'enabled': True})
    return entries


def write_exclusions(exclude_file: str, entries: list[dict]):
    """Write exclusion entries back to the exclude file."""
    path = Path(paths.resolve_config_path(exclude_file))
    lines = []
    for entry in entries:
        if entry.get('enabled', True):
            lines.append(entry['pattern'])
        else:
            lines.append(f'# {entry["pattern"]}')
    atomic_write_text(path, '\n'.join(lines) + '\n')


DEFAULT_EXCLUDES = """\
# Exclude patterns for rsync (one per line)

# Windows system directories (typically inaccessible from Linux)
$RECYCLE.BIN
System Volume Information
WpSystem
"""


RESTIC_DEFAULT_EXCLUDES = """\
# Exclude patterns for restic (one per line): an absolute path anchors,
# * and ** match within and across directories.
"""


def create_exclude_file(job_id: str, directory: Path | None = None, *, engine: str = 'rsync') -> str:
    """Create a new exclude file with sensible defaults.

    Returns its name relative to the config dir — how jobs.json refers to it,
    so the file is found wherever the config dir is (see paths.resolve_config_path).
    ``directory``: where it goes instead (a system job's, next to system/jobs.json).
    """
    name = f'{job_id}.exclude'
    path = (directory or paths.config_dir()) / name
    if not path.exists():
        atomic_write_text(path, RESTIC_DEFAULT_EXCLUDES if engine == 'restic' else DEFAULT_EXCLUDES)
    return name


def usable_job(job) -> bool:
    """Enough of a job to run it: an id, a destination, and something to back
    up — a source (rsync), a path list or the job to copy from (restic)."""
    if not isinstance(job, dict) or not units.valid_job_id(str(job.get('id', ''))) \
            or not job.get('destination'):
        return False
    if units.job_engine(job) == 'restic':
        restic = job.get('restic') or {}
        if restic.get('mode', 'backup') == 'copy':
            return bool(restic.get('from_job'))
        return bool(restic.get('paths'))
    return bool(job.get('source'))


def _read_jobs_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f'[BackupMonitor] Error loading {path}: {e}')
        return {}
    return data if isinstance(data, dict) else {}


def load_jobs() -> list[dict]:
    """The user jobs from jobs.json; empty (with a message) when unreadable."""
    jobs = []
    for job in _read_jobs_file(paths.jobs_file()).get('jobs', []):
        if not usable_job(job):
            print(f'[BackupMonitor] Skipping invalid job in jobs.json: {job!r:.120}')
        elif units.job_scope(job) == 'system':
            print(f'[BackupMonitor] Skipping {job["id"]} in jobs.json: system jobs belong in '
                  f'{paths.system_source_dir()}/jobs.json')
        else:
            jobs.append(job)
    return jobs


def load_system_source() -> list[dict]:
    """The system jobs as edited in the overlay; [] when there are none."""
    return [{**j, 'scope': 'system'} for j in
            _read_jobs_file(paths.system_source_dir() / 'jobs.json').get('jobs', [])
            if usable_job(j)]


def load_system_jobs() -> list[dict]:
    """The system jobs as installed (``mirror-backup system apply``) — what the
    system units actually run, which is not necessarily what is being edited."""
    jobs = []
    for job in _read_jobs_file(paths.system_jobs_file()).get('jobs', []):
        if usable_job(job):
            jobs.append({**job, 'scope': 'system'})
    return jobs


def system_owner() -> str:
    """The user the system jobs report to and who may start and stop them."""
    return str(_read_jobs_file(paths.system_jobs_file()).get('owner', ''))


def load_all_jobs(host: str | None = None) -> list[dict]:
    """Every job of both scopes that runs on this host — what the panels show."""
    return [j for j in (*load_jobs(), *load_system_jobs()) if units.runs_here(j, host)]


def source_label(job: dict) -> str:
    """What a job backs up, in a few words: its source, its paths, or the job
    whose snapshots it copies."""
    if units.job_engine(job) != 'restic':
        return job.get('source', '')
    restic = job.get('restic') or {}
    if restic.get('mode', 'backup') == 'copy':
        return f'snapshots of {restic.get("from_job", "?")}'
    return ', '.join(restic.get('paths', []))
