"""Where Mirror Backup keeps things — the one module that knows.

Three places, three lifetimes:

``config``    ``$XDG_CONFIG_HOME/backup-sync``
    Job definitions (``jobs.json``), app settings and exclude files. May be a
    symlink into a git checkout, so several installs share one set of jobs.

``job state`` ``<destination>/.mirror-backup``
    Status, run history, log and the postponed-run marker of one job. It lives
    inside the mirror it describes, so every machine or operating system that
    mounts the destination sees the same state, and a run finished on one of
    them counts on the others.

``runtime``   ``$XDG_RUNTIME_DIR/backup-sync``
    Queue lock, PID files and rsync progress output. Meaningful for the current
    boot only, which is exactly the lifetime of a runtime directory.

System jobs (scope ``system``: run by root, see ``system``) have their own:

``/etc/mirror-backup``           the installed jobs (``jobs.json``), their exclude
                                 files, repository keys (``keys/``) and the
                                 generated path lists (``generated/``)
``config/system``                where they are edited — inside the user's config
                                 dir, so the overlay that holds the user jobs
                                 holds them too; ``mirror-backup system apply``
                                 installs them
``/usr/local/lib/mirror-backup`` the root-owned copy of backup-sync and the app
                                 that system units run
``/run/mirror-backup``           their runtime files, and the queue lock that
                                 jobs of both scopes share

``scripts/backup-sync`` resolves the same paths in bash; keep the two in step.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

STATE_DIRNAME = '.mirror-backup'
SYSTEM_LIB_DIR = '/usr/local/lib/mirror-backup'
# The root-owned command; root must never run the user's ~/.local/bin copy.
SYSTEM_COMMAND = f'{SYSTEM_LIB_DIR}/mirror-backup'
SYSTEM_RUN_DIR = '/run/mirror-backup'
SYSTEM_UNIT_DIR = Path('/etc/systemd/system')


def _xdg(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value and os.path.isabs(value) else fallback


def config_dir() -> Path:
    return _xdg('XDG_CONFIG_HOME', Path.home() / '.config') / 'backup-sync'


def jobs_file() -> Path:
    return config_dir() / 'jobs.json'


def settings_file() -> Path:
    return config_dir() / 'settings.json'


def systemd_user_dir() -> Path:
    return _xdg('XDG_CONFIG_HOME', Path.home() / '.config') / 'systemd' / 'user'


def runtime_dir() -> Path:
    return _xdg('XDG_RUNTIME_DIR', Path(f'/run/user/{os.getuid()}')) / 'backup-sync'


def system_config_dir() -> Path:
    """The installed system jobs. MIRROR_BACKUP_SYSTEM_CONFIG moves it (tests)."""
    return Path(os.environ.get('MIRROR_BACKUP_SYSTEM_CONFIG') or '/etc/mirror-backup')


def system_jobs_file() -> Path:
    return system_config_dir() / 'jobs.json'


def system_source_dir() -> Path:
    """Where the system jobs are edited, before `mirror-backup system apply`."""
    return config_dir() / 'system'


def unit_dir(scope: str) -> Path:
    return SYSTEM_UNIT_DIR if scope == 'system' else systemd_user_dir()


def generated_dir(scope: str) -> Path:
    """Files generated next to the units: the path lists of restic jobs."""
    if scope == 'system':
        return system_config_dir() / 'generated'
    return _xdg('XDG_DATA_HOME', Path.home() / '.local' / 'share') / 'mirror-backup' / 'generated'


def legacy_data_dir() -> Path:
    """Where versions up to 0.5 kept status, history and logs of every job."""
    return _xdg('XDG_DATA_HOME', Path.home() / '.local' / 'share') / 'backup-sync'


def resolve_config_path(path: str) -> str:
    """Exclude files may be named relative to the config dir, which keeps a
    jobs.json kept in git independent of the home directory it is used from."""
    if not path:
        return ''
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return expanded
    return str(config_dir() / expanded)


@dataclass(frozen=True)
class JobPaths:
    """The state files of one job, all inside its destination."""
    destination: Path

    @property
    def state_dir(self) -> Path:
        return self.destination / STATE_DIRNAME

    @property
    def status(self) -> Path:
        return self.state_dir / 'status.json'

    @property
    def history(self) -> Path:
        return self.state_dir / 'history.jsonl'

    @property
    def log(self) -> Path:
        return self.state_dir / 'backup.log'

    @property
    def deferred(self) -> Path:
        return self.state_dir / 'deferred'

    @property
    def available(self) -> bool:
        """The destination is there — mounted, if it lives on a mount."""
        return self.destination.is_dir()


def job_paths(destination: str) -> JobPaths:
    return JobPaths(Path(destination.rstrip('/') or '/'))


def boot_id() -> str:
    """Identifies this boot of this operating system; a status written under
    another boot id is never live here, whatever its PID says."""
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


def host_name() -> str:
    return socket.gethostname()
