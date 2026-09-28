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

``scripts/backup-sync`` resolves the same paths in bash; keep the two in step.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

STATE_DIRNAME = '.mirror-backup'


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
