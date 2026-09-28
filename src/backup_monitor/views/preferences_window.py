"""Preferences window — global app settings."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from backup_monitor.models.settings import Settings


class PreferencesWindow(Adw.PreferencesWindow):
    """Global preferences for Mirror Backup."""

    __gtype_name__ = 'PreferencesWindow'

    def __init__(self, parent, settings: Settings):
        super().__init__(
            transient_for=parent,
            title='Preferences',
            default_width=500,
            default_height=550,
        )
        self._settings = settings

        # ── Notifications page ──
        notif_page = Adw.PreferencesPage(
            title='Notifications',
            icon_name='preferences-system-notifications-symbolic',
        )
        self.add(notif_page)

        notif_group = Adw.PreferencesGroup(
            title='Desktop Notifications',
            description='Which events trigger a notification',
        )
        notif_page.add(notif_group)

        self._notif_start = self._switch_row(
            'On backup start',
            'Show notification when a backup begins',
            settings.get_notification('on_start'),
        )
        self._notif_start.connect('notify::active', self._on_notif_changed, 'on_start')
        notif_group.add(self._notif_start)

        self._notif_complete = self._switch_row(
            'On backup complete',
            'Show notification when a backup finishes successfully',
            settings.get_notification('on_complete'),
        )
        self._notif_complete.connect('notify::active', self._on_notif_changed, 'on_complete')
        notif_group.add(self._notif_complete)

        self._notif_error = self._switch_row(
            'On backup error',
            'Show notification when a backup fails',
            settings.get_notification('on_error'),
        )
        self._notif_error.connect('notify::active', self._on_notif_changed, 'on_error')
        notif_group.add(self._notif_error)

        # ── Defaults page ──
        defaults_page = Adw.PreferencesPage(
            title='Defaults',
            icon_name='preferences-other-symbolic',
        )
        self.add(defaults_page)

        job_defaults_group = Adw.PreferencesGroup(
            title='New Job Defaults',
            description='Default values when creating a new backup job',
        )
        defaults_page.add(job_defaults_group)

        # Archive days
        self._archive_spin = Adw.SpinRow.new_with_range(0, 365, 1)
        self._archive_spin.set_title('Archive retention (days)')
        self._archive_spin.set_subtitle('0 = no archive')
        self._archive_spin.set_value(settings.get('default_archive_days'))
        self._archive_spin.connect('notify::value', self._on_spin_changed, 'default_archive_days')
        job_defaults_group.add(self._archive_spin)

        # Bandwidth limit
        self._bwlimit_spin = Adw.SpinRow.new_with_range(0, 1000000, 100)
        self._bwlimit_spin.set_title('Bandwidth limit (KB/s)')
        self._bwlimit_spin.set_subtitle('0 = unlimited')
        self._bwlimit_spin.set_value(settings.get('default_bandwidth_limit_kbps'))
        self._bwlimit_spin.connect('notify::value', self._on_spin_changed, 'default_bandwidth_limit_kbps')
        job_defaults_group.add(self._bwlimit_spin)

        # Nice
        self._nice_spin = Adw.SpinRow.new_with_range(0, 19, 1)
        self._nice_spin.set_title('CPU priority (nice)')
        self._nice_spin.set_subtitle('Higher = lower priority (10 recommended)')
        self._nice_spin.set_value(settings.get('default_nice'))
        self._nice_spin.connect('notify::value', self._on_spin_changed, 'default_nice')
        job_defaults_group.add(self._nice_spin)

        # IO priority
        self._io_spin = Adw.SpinRow.new_with_range(0, 7, 1)
        self._io_spin.set_title('I/O priority')
        self._io_spin.set_subtitle('Higher = lower priority (7 = lowest)')
        self._io_spin.set_value(settings.get('default_io_priority'))
        self._io_spin.connect('notify::value', self._on_spin_changed, 'default_io_priority')
        job_defaults_group.add(self._io_spin)

        # ── Rsync defaults ──
        rsync_defaults_group = Adw.PreferencesGroup(
            title='Default Rsync Options',
            description='Default rsync flags for new jobs',
        )
        defaults_page.add(rsync_defaults_group)

        default_rsync = settings.get('default_rsync_options', {})

        rsync_default_flags = [
            ('compress', 'Compression', 'Compress data during transfer'),
            ('partial', 'Keep partial transfers', 'Resume interrupted transfers'),
            ('xattrs', 'Preserve extended attributes', 'Copy xattrs'),
            ('acls', 'Preserve ACLs', 'Copy filesystem ACLs'),
        ]
        self._rsync_default_switches = {}
        for key, title, subtitle in rsync_default_flags:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            row.set_active(default_rsync.get(key, False))
            row.connect('notify::active', self._on_rsync_default_changed, key)
            self._rsync_default_switches[key] = row
            rsync_defaults_group.add(row)

        # ── Maintenance ──
        maint_group = Adw.PreferencesGroup(title='Maintenance')
        defaults_page.add(maint_group)

        self._log_retention_spin = Adw.SpinRow.new_with_range(1, 365, 1)
        self._log_retention_spin.set_title('Log retention (days)')
        self._log_retention_spin.set_subtitle('Logs older than this are deleted on app start')
        self._log_retention_spin.set_value(settings.get('log_retention_days'))
        self._log_retention_spin.connect('notify::value', self._on_spin_changed, 'log_retention_days')
        maint_group.add(self._log_retention_spin)

    def _switch_row(self, title: str, subtitle: str, active: bool) -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(active)
        return row

    def _on_notif_changed(self, row, pspec, key):
        self._settings.set_notification(key, row.get_active())

    def _on_spin_changed(self, row, pspec, key):
        self._settings.set(key, int(row.get_value()))

    def _on_rsync_default_changed(self, row, pspec, key):
        defaults = self._settings.get('default_rsync_options', {})
        defaults[key] = row.get_active()
        self._settings.set('default_rsync_options', defaults)
