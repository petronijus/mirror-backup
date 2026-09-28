"""Job detail page — current status, run history, and log access."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GObject

from backup_monitor.models.job import BackupJob
from backup_monitor.models.job_history import (
    read_history, compute_stats, HistoryEntry, HistoryStats,
)


class JobDetailPage(Adw.NavigationPage):
    """Detail page for a single backup job."""

    __gtype_name__ = 'JobDetailPage'

    __gsignals__ = {
        'view-log': (GObject.SignalFlags.RUN_FIRST, None, (str, str)),  # job_id, job_name
    }

    def __init__(self, job: BackupJob):
        super().__init__(title=job.label)
        self._job = job

        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        toolbar.set_content(scroll)

        clamp = Adw.Clamp(
            maximum_size=700,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        scroll.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(content)

        # ── Job info ──
        info_group = Adw.PreferencesGroup(title='Job Info')
        content.append(info_group)

        if self._job.description:
            info_group.add(self._info_row('Description', self._job.description))

        info_group.add(self._info_row('Source', self._job.source))
        info_group.add(self._info_row('Destination', self._job.destination))

        if self._job.timer_schedule:
            info_group.add(self._info_row('Schedule', self._job.timer_schedule))
        if self._job.archive_days > 0:
            info_group.add(self._info_row('Archive retention', f'{self._job.archive_days} days'))
        if self._job.exclude_file:
            from pathlib import Path
            info_group.add(self._info_row('Exclude file', Path(self._job.exclude_file).name))

        # ── Current status ──
        status_group = Adw.PreferencesGroup(title='Current Status')
        content.append(status_group)

        st = self._job.status
        state_display = {
            'idle': 'Idle', 'queued': 'Queued', 'scanning': 'Scanning',
            'running': 'Syncing', 'paused': 'Paused', 'deferred': 'Postponed',
            'error': 'Error', 'unavailable': 'Destination unavailable',
        }
        status_group.add(self._info_row('State', state_display.get(st.state, st.state)))
        if st.state == 'deferred':
            status_group.add(self._info_row('Postponed', st.deferred_note))

        if self._job.next_run:
            from backup_monitor.services.systemd_service import format_countdown
            countdown = format_countdown(self._job.next_run)
            status_group.add(self._info_row('Next run', countdown or self._job.next_run))
        if self._job.last_run:
            from backup_monitor.services.systemd_service import format_relative_past
            last = format_relative_past(self._job.last_run) or self._job.last_run
            if self._job.last_run_host:
                last = f'{last} on {self._job.last_run_host}'
            status_group.add(self._info_row('Last run', last))

        if st.state in ('running', 'scanning', 'paused'):
            if st.progress > 0:
                status_group.add(self._info_row('Progress', f'{st.progress:.0f}%'))
            if st.speed:
                status_group.add(self._info_row('Speed', st.speed))
            if st.eta and st.eta != '0:00:00':
                status_group.add(self._info_row('ETA', st.eta))
            if st.files_total > 0:
                status_group.add(self._info_row(
                    'Files', f'{st.files_transferred} / {st.files_total}'))

        if st.state in ('error', 'unavailable') and st.error:
            error_row = self._info_row('Error', st.error)
            error_row.add_css_class('error')
            status_group.add(error_row)

        if st.consecutive_failures >= 2:
            streak_row = self._info_row(
                'Persistent failure',
                f'Failed {st.consecutive_failures} runs in a row',
            )
            streak_row.add_css_class('error')
            status_group.add(streak_row)

        # ── Suggested excludes ──
        if st.suggested_excludes and self._job.exclude_file:
            self._build_suggested_excludes(content, st.suggested_excludes)

        # ── Statistics ──
        entries = read_history(self._job.destination)
        stats = compute_stats(entries)

        if stats.total_runs > 0:
            stats_group = Adw.PreferencesGroup(title='Statistics')
            content.append(stats_group)

            stats_group.add(self._info_row('Total runs', str(stats.total_runs)))
            stats_group.add(self._info_row(
                'Success rate', f'{stats.success_rate:.0f}% ({stats.successful_runs}/{stats.total_runs})'))
            stats_group.add(self._info_row('Avg duration', stats.avg_duration_formatted))
            stats_group.add(self._info_row('Last success', stats.last_success))
            if stats.failed_runs > 0:
                stats_group.add(self._info_row('Last failure', stats.last_failure))

        # ── Run history ──
        if entries:
            history_group = Adw.PreferencesGroup(
                title='Recent Runs',
                description=f'Last {len(entries)} runs',
            )
            content.append(history_group)

            for entry in entries[:20]:  # Show last 20
                history_group.add(self._history_row(entry))

        # ── Log access ──
        log_group = Adw.PreferencesGroup(title='Logs')
        content.append(log_group)

        log_row = Adw.ActionRow(
            title='View full log',
            subtitle=f'{self._job.destination.rstrip("/")}/.mirror-backup/backup.log',
            activatable=True,
        )
        log_row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
        log_row.connect('activated', self._on_view_log)
        log_group.add(log_row)

    def _info_row(self, title: str, value: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=value)
        row.set_subtitle_selectable(True)
        return row

    def _build_suggested_excludes(self, parent: Gtk.Box, paths: list[str]) -> None:
        """Add an Adw.PreferencesGroup with one row per suggested exclude path.

        Each row has an "Add to exclude" button that appends to the job's
        exclude file. After click, the row is hidden so the user gets visual
        confirmation without needing to re-read the page.
        """
        group = Adw.PreferencesGroup(
            title='Suggested excludes',
            description=(
                'Permission errors during the last run. '
                'Add to the exclude file to silence them.'
            ),
        )
        parent.append(group)

        for path in paths:
            row = Adw.ActionRow(title=path)
            row.set_subtitle_selectable(True)

            btn = Gtk.Button(
                label='Add to exclude',
                css_classes=['flat'],
                valign=Gtk.Align.CENTER,
            )
            btn.connect('clicked', self._on_add_exclude, path, row)
            row.add_suffix(btn)

            group.add(row)

    def _on_add_exclude(self, btn: Gtk.Button, path: str, row: Adw.ActionRow) -> None:
        """Append a path to the job's exclude file (idempotent)."""
        from pathlib import Path

        from backup_monitor import paths
        exclude_path = Path(paths.resolve_config_path(self._job.exclude_file))
        try:
            existing = ''
            if exclude_path.is_file():
                existing = exclude_path.read_text()

            # Skip if already present (allow leading whitespace, ignore comments)
            for line in existing.splitlines():
                if line.strip() == path:
                    btn.set_label('Already in exclude')
                    btn.set_sensitive(False)
                    return

            with exclude_path.open('a') as f:
                if existing and not existing.endswith('\n'):
                    f.write('\n')
                f.write(f'{path}\n')

            btn.set_label('Added ✓')
            btn.set_sensitive(False)
        except OSError as e:
            btn.set_label(f'Error: {e}')
            btn.set_sensitive(False)

    def _history_row(self, entry: HistoryEntry) -> Adw.ActionRow:
        icon = 'emblem-ok-symbolic' if entry.success else 'dialog-error-symbolic'
        status_text = 'OK' if entry.success else f'Failed (exit {entry.exit_code})'

        where = f'  ·  {entry.host}' if entry.host else ''
        row = Adw.ActionRow(
            title=entry.started_relative,
            subtitle=f'{status_text}  ·  {entry.duration_formatted}  ·  {entry.files_transferred} files{where}',
        )

        status_icon = Gtk.Image(
            icon_name=icon,
            css_classes=['success' if entry.success else 'error'],
            valign=Gtk.Align.CENTER,
        )
        row.add_prefix(status_icon)

        return row

    def _on_view_log(self, row):
        self.emit('view-log', self._job.id, self._job.label)
