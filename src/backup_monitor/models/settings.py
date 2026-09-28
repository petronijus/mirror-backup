"""Global app settings — stored as JSON in the config dir's settings.json."""

from __future__ import annotations

import json

from backup_monitor import paths
from backup_monitor.fsutil import atomic_write_text

DEFAULTS = {
    'notifications': {
        'on_start': True,
        'on_complete': True,
        'on_error': True,
    },
    'log_retention_days': 30,
    'default_nice': 10,
    'default_io_priority': 7,
    'default_archive_days': 0,
    'default_bandwidth_limit_kbps': 0,
}


class Settings:
    """Read/write global app settings."""

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        settings_file = paths.settings_file()
        if settings_file.is_file():
            try:
                self._data = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        atomic_write_text(paths.settings_file(), json.dumps(self._data, indent=2) + '\n')

    def get(self, key: str, default=None):
        """Get a setting value, falling back to DEFAULTS then default."""
        if key in self._data:
            return self._data[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def set(self, key: str, value):
        """Set a setting value and save."""
        self._data[key] = value
        self._save()

    def get_notification(self, key: str) -> bool:
        notifs = self._data.get('notifications', DEFAULTS['notifications'])
        return notifs.get(key, True)

    def set_notification(self, key: str, value: bool):
        if 'notifications' not in self._data:
            self._data['notifications'] = dict(DEFAULTS['notifications'])
        self._data['notifications'][key] = value
        self._save()
