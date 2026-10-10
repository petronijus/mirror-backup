"""Job editor page — form for creating/editing backup jobs."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, GObject

import sys
from pathlib import Path

from backup_monitor import paths, units
from backup_monitor.services.job_manager import JobManager, create_exclude_file

FREQ_MODES = ['Weekly', 'Monthly', 'Custom', 'Manual only']
WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
WEEKDAY_SYSTEMD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
ENGINES = ['rsync', 'restic']
SCOPES = ['user', 'system']
RESTIC_MODES = ['backup', 'copy']
KEEP_KEYS = ['daily', 'weekly', 'monthly', 'yearly']
META_COMMAND = 'mirror-backup-system-meta'


class JobEditorPage(Adw.NavigationPage):
    """Form page for creating or editing a backup job."""

    __gtype_name__ = 'JobEditorPage'

    __gsignals__ = {
        'saved': (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # job_id
        'deleted': (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # job_id
    }

    def __init__(self, job_manager: JobManager, job_id: str | None = None):
        is_new = job_id is None
        super().__init__(title='New Backup Job' if is_new else 'Edit Job')

        self._manager = job_manager
        self._job_id = job_id
        self._job_data = job_manager.get_job(job_id) if job_id else None
        self._exclude_file = self._job_data.get('exclude_file', '') if self._job_data else ''
        self._engine = units.job_engine(self._job_data) if self._job_data else 'rsync'
        self._scope = units.job_scope(self._job_data) if self._job_data else 'user'

        # Main layout
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        # Header bar with save button
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        save_btn = Gtk.Button(
            label='Create' if is_new else 'Save',
            css_classes=['suggested-action'],
        )
        save_btn.connect('clicked', self._on_save)
        header.pack_end(save_btn)

        # Delete button for existing jobs
        if not is_new:
            delete_btn = Gtk.Button(
                icon_name='user-trash-symbolic',
                css_classes=['destructive-action'],
                tooltip_text='Delete this job',
            )
            delete_btn.connect('clicked', self._on_delete)
            header.pack_start(delete_btn)

        # Scrollable form
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        toolbar.set_content(scroll)

        clamp = Adw.Clamp(
            maximum_size=600,
            margin_top=24, margin_bottom=24,
            margin_start=24, margin_end=24,
        )
        scroll.set_child(clamp)

        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        clamp.set_child(form)

        # ── Basic Info ──
        basic_group = Adw.PreferencesGroup(title='Basic')
        form.append(basic_group)

        self._name_row = Adw.EntryRow(title='Name')
        basic_group.add(self._name_row)

        self._desc_row = Adw.EntryRow(title='Description')
        basic_group.add(self._desc_row)

        # Kind and scope: fixed once a job exists (a job changes neither its
        # repository format nor the file it is kept in).
        kind_group = Adw.PreferencesGroup(
            title='Kind',
            description='A mirror keeps the latest state as plain files; snapshots keep every '
                        'version, deduplicated and encrypted. System jobs run as root and read '
                        'every file — what a backup of the whole system needs; installing one '
                        'asks for your password.' if is_new else '',
        )
        form.append(kind_group)
        self._engine_row = Adw.ComboRow(
            title='Backup',
            model=Gtk.StringList.new(['Mirror (rsync)', 'Snapshots (restic)']),
            sensitive=is_new,
        )
        self._engine_row.set_selected(ENGINES.index(self._engine))
        self._engine_row.connect('notify::selected', self._on_kind_changed)
        kind_group.add(self._engine_row)
        self._scope_row = Adw.ComboRow(
            title='Runs as',
            model=Gtk.StringList.new(['You', 'System (root)']),
            sensitive=is_new,
        )
        self._scope_row.set_selected(SCOPES.index(self._scope))
        self._scope_row.connect('notify::selected', self._on_kind_changed)
        kind_group.add(self._scope_row)

        # ── Paths ──
        paths_group = Adw.PreferencesGroup(title='Paths')
        form.append(paths_group)

        self._restic_mode_row = Adw.ComboRow(
            title='Snapshots of',
            model=Gtk.StringList.new(['Paths listed below', 'Another job (copy its snapshots)']),
        )
        self._restic_mode_row.connect('notify::selected', self._on_kind_changed)
        paths_group.add(self._restic_mode_row)

        self._from_ids = [j['id'] for j in job_manager.jobs + job_manager.system_jobs
                          if units.job_engine(j) == 'restic' and j['id'] != job_id]
        self._from_job_row = Adw.ComboRow(
            title='Copy from',
            model=Gtk.StringList.new(self._from_ids or ['(no restic job yet)']),
            sensitive=bool(self._from_ids),
        )
        paths_group.add(self._from_job_row)

        # The paths a restic job backs up, one per line. A group of its own:
        # a group lists widgets that are not rows after all of its rows.
        self._paths_group = Adw.PreferencesGroup(
            title='Paths to back up',
            description='One absolute path per line. Each counts on its own file system: a path '
                        'on another disk, or a bind mount, has to be listed itself.',
        )
        self._paths_view = Gtk.TextView(
            monospace=True, wrap_mode=Gtk.WrapMode.NONE,
            top_margin=8, bottom_margin=8, left_margin=12, right_margin=12,
        )
        self._paths_group.add(Gtk.Frame(child=Gtk.ScrolledWindow(
            child=self._paths_view, min_content_height=200,
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)))

        # Source path with folder picker
        self._source_row = Adw.EntryRow(title='Source')
        source_btn = Gtk.Button(
            icon_name='folder-open-symbolic',
            css_classes=['flat'],
            valign=Gtk.Align.CENTER,
        )
        source_btn.connect('clicked', lambda b: self._pick_folder(self._source_row))
        self._source_row.add_suffix(source_btn)
        paths_group.add(self._source_row)

        # Destination path with folder picker
        self._dest_row = Adw.EntryRow(title='Destination')
        dest_btn = Gtk.Button(
            icon_name='folder-open-symbolic',
            css_classes=['flat'],
            valign=Gtk.Align.CENTER,
        )
        dest_btn.connect('clicked', lambda b: self._pick_folder(self._dest_row))
        self._dest_row.add_suffix(dest_btn)
        paths_group.add(self._dest_row)

        form.append(self._paths_group)

        # ── Schedule ──
        schedule_group = Adw.PreferencesGroup(title='Schedule')
        form.append(schedule_group)

        # Frequency mode dropdown
        freq_strings = Gtk.StringList()
        for label in FREQ_MODES:
            freq_strings.append(label)
        self._freq_dropdown = Adw.ComboRow(
            title='Frequency',
            model=freq_strings,
        )
        self._freq_dropdown.connect('notify::selected', self._on_freq_changed)
        schedule_group.add(self._freq_dropdown)

        # Interval: "Every N weeks/months"
        self._interval_row = Adw.SpinRow.new_with_range(1, 99, 1)
        self._interval_row.set_title('Repeat every')
        self._interval_row.set_value(1)
        self._interval_row.connect('notify::value', self._on_schedule_detail_changed)
        schedule_group.add(self._interval_row)

        # Weekday pills (Weekly mode) — row of toggle buttons
        self._weekday_row = Adw.ActionRow(title='On days')
        weekday_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        self._weekday_buttons = []
        for label in WEEKDAY_LABELS:
            btn = Gtk.ToggleButton(
                label=label,
                css_classes=['pill', 'bm-weekday-btn'],
            )
            btn.connect('toggled', self._on_schedule_detail_changed)
            weekday_box.append(btn)
            self._weekday_buttons.append(btn)
        self._weekday_row.add_suffix(weekday_box)
        schedule_group.add(self._weekday_row)

        # Day of month (Monthly mode)
        self._month_day_row = Adw.SpinRow.new_with_range(1, 31, 1)
        self._month_day_row.set_title('On day of month')
        self._month_day_row.set_value(1)
        self._month_day_row.set_visible(False)
        self._month_day_row.connect('notify::value', self._on_schedule_detail_changed)
        schedule_group.add(self._month_day_row)

        # Time picker: [−] HH : MM [+] with 30-min steps
        time_row = Adw.ActionRow(title='At time')
        time_box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)

        minus_btn = Gtk.Button(
            icon_name='list-remove-symbolic',
            css_classes=['flat', 'circular'],
            tooltip_text='−30 minutes',
        )
        minus_btn.connect('clicked', self._on_time_minus)
        time_box.append(minus_btn)

        self._hour_entry = Gtk.Entry(
            text='00', max_length=2, width_chars=2,
            xalign=0.5, input_purpose=Gtk.InputPurpose.DIGITS,
            css_classes=['bm-time-entry'],
        )
        self._hour_entry.connect('changed', self._on_time_entry_changed)
        time_box.append(self._hour_entry)

        time_box.append(Gtk.Label(label=':', css_classes=['title-3']))

        self._minute_entry = Gtk.Entry(
            text='00', max_length=2, width_chars=2,
            xalign=0.5, input_purpose=Gtk.InputPurpose.DIGITS,
            css_classes=['bm-time-entry'],
        )
        self._minute_entry.connect('changed', self._on_time_entry_changed)
        time_box.append(self._minute_entry)

        plus_btn = Gtk.Button(
            icon_name='list-add-symbolic',
            css_classes=['flat', 'circular'],
            tooltip_text='+30 minutes',
        )
        plus_btn.connect('clicked', self._on_time_plus)
        time_box.append(plus_btn)

        time_row.add_suffix(time_box)
        schedule_group.add(time_row)

        # Custom expression (Custom mode only)
        self._custom_expr_row = Adw.EntryRow(
            title='Systemd calendar expression',
            visible=False,
        )
        schedule_group.add(self._custom_expr_row)

        # Schedule summary
        self._schedule_summary = Adw.ActionRow(
            title='Schedule summary',
            css_classes=['dim-label'],
        )
        schedule_group.add(self._schedule_summary)

        # Initial visibility
        self._on_freq_changed(self._freq_dropdown, None)

        # ── Advanced ──
        advanced_group = Adw.PreferencesGroup(title='Advanced')
        form.append(advanced_group)

        # Archive days
        self._archive_row = Adw.SpinRow.new_with_range(0, 365, 1)
        self._archive_row.set_title('Archive retention (days)')
        self._archive_row.set_subtitle('0 = no archive, deleted files are gone')
        advanced_group.add(self._archive_row)

        # Bandwidth limit
        self._bwlimit_row = Adw.SpinRow.new_with_range(0, 1000000, 100)
        self._bwlimit_row.set_title('Bandwidth limit (KB/s)')
        self._bwlimit_row.set_subtitle('0 = unlimited')
        advanced_group.add(self._bwlimit_row)

        # Exclude file
        self._exclude_row = Adw.ActionRow(
            title='Exclusion patterns',
            subtitle='No exclude file',
            activatable=True,
        )
        self._exclude_row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
        self._exclude_row.connect('activated', self._on_edit_exclusions)
        advanced_group.add(self._exclude_row)

        # Enabled toggle
        self._enabled_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._enabled_switch.set_active(True)
        enabled_row = Adw.ActionRow(
            title='Enabled',
            subtitle='Enable scheduled backups',
        )
        enabled_row.add_suffix(self._enabled_switch)
        enabled_row.set_activatable_widget(self._enabled_switch)
        advanced_group.add(enabled_row)

        # ── restic ──
        restic_group = Adw.PreferencesGroup(
            title='Snapshots',
            description=GLib.markup_escape_text(
                'The repository is <destination>/repo; create it once with '
                f'sudo {paths.SYSTEM_COMMAND} system init <job> (system jobs) '
                'or restic init (your own).'),
        )
        self._restic_group = restic_group
        form.append(restic_group)
        self._password_row = Adw.EntryRow(title='Repository password file')
        restic_group.add(self._password_row)
        self._host_row = Adw.EntryRow(title=f'Snapshot host name (empty: {paths.host_name()})')
        restic_group.add(self._host_row)
        self._keep_rows = {}
        for key, default in zip(KEEP_KEYS, (7, 4, 6, 0)):
            row = Adw.SpinRow.new_with_range(0, 1000, 1)
            row.set_title(f'Keep {key} snapshots')
            row.set_value(default)
            self._keep_rows[key] = row
            restic_group.add(row)
        self._prune_row = Adw.SpinRow.new_with_range(0, 365, 1)
        self._prune_row.set_title('Prune every (days)')
        self._prune_row.set_subtitle('Frees what forgotten snapshots held; 0 = never')
        self._prune_row.set_value(7)
        restic_group.add(self._prune_row)
        self._check_row = Adw.SpinRow.new_with_range(0, 365, 1)
        self._check_row.set_title('Check the repository every (days)')
        self._check_row.set_subtitle('Reads back a part of the data each time; 0 = never')
        self._check_row.set_value(7)
        restic_group.add(self._check_row)
        self._subset_row = Adw.EntryRow(title='Data read per check (e.g. 5%, 1/10, 2G)')
        self._subset_row.set_text('5%')
        restic_group.add(self._subset_row)
        self._meta_row = Adw.SwitchRow(
            title='Write restore metadata first',
            subtitle='Partition tables, LUKS headers, package lists — what a bare-metal '
                     'restore needs (system jobs)',
        )
        restic_group.add(self._meta_row)

        # ── Rsync Options ──
        rsync_group = Adw.PreferencesGroup(
            title='Rsync Options',
            description='Fine-tune rsync behavior for this job',
        )
        form.append(rsync_group)
        self._rsync_group = rsync_group

        # Delete mode
        delete_strings = Gtk.StringList()
        for label in ['Delete before transfer', 'Delete during transfer',
                       'Delete after transfer', 'No deletions (additive)']:
            delete_strings.append(label)
        self._delete_mode_row = Adw.ComboRow(
            title='Delete mode',
            subtitle='When to remove files not in source',
            model=delete_strings,
        )
        rsync_group.add(self._delete_mode_row)

        # Boolean rsync flags
        self._rsync_switches = {}
        rsync_flags = [
            ('compress', 'Compression', 'Compress data during transfer (useful for slow links)'),
            ('checksum', 'Checksum verification', 'Compare files by checksum instead of time+size (slower, more accurate)'),
            ('hard_links', 'Preserve hard links', 'Detect and preserve hard links between files'),
            ('xattrs', 'Preserve extended attributes', 'Copy extended filesystem attributes'),
            ('acls', 'Preserve ACLs', 'Copy filesystem access control lists'),
            ('partial', 'Keep partial transfers', 'Resume interrupted file transfers instead of restarting'),
            ('update', 'Skip newer files', 'Do not overwrite files that are newer on the destination'),
        ]
        for key, title, subtitle in rsync_flags:
            row = Adw.SwitchRow(title=title, subtitle=subtitle)
            self._rsync_switches[key] = row
            rsync_group.add(row)

        # Size filters
        self._max_size_row = Adw.EntryRow(title='Max file size (e.g. 500M, 2G)')
        rsync_group.add(self._max_size_row)

        self._min_size_row = Adw.EntryRow(title='Min file size (e.g. 1K, 10M)')
        rsync_group.add(self._min_size_row)

        # ── Dry run ──
        if not is_new:
            dryrun_group = Adw.PreferencesGroup(title='Preview')
            form.append(dryrun_group)

            dryrun_row = Adw.ActionRow(
                title='Dry run',
                subtitle='Show what would be transferred without making changes',
                activatable=True,
            )
            dryrun_row.add_suffix(Gtk.Image(icon_name='go-next-symbolic'))
            dryrun_row.connect('activated', self._on_dry_run)
            dryrun_group.add(dryrun_row)

        # ── Populate form if editing ──
        if self._job_data:
            self._populate(self._job_data)
        self._on_kind_changed()

    def _populate(self, job: dict):
        """Fill the form with existing job data."""
        self._name_row.set_text(job.get('name', ''))
        self._desc_row.set_text(job.get('description', ''))
        self._source_row.set_text(job.get('source', ''))
        self._dest_row.set_text(job.get('destination', ''))
        self._archive_row.set_value(job.get('archive_days', 0))
        self._bwlimit_row.set_value(job.get('bandwidth_limit_kbps', 0))
        self._enabled_switch.set_active(job.get('enabled', True))

        schedule = job.get('schedule', {})
        sched_type = schedule.get('type', 'calendar')
        expression = schedule.get('expression', 'daily')

        if sched_type == 'manual':
            self._freq_dropdown.set_selected(FREQ_MODES.index('Manual only'))
        else:
            self._populate_schedule_from_expression(expression)

        # Exclude file info
        exclude = job.get('exclude_file', '')
        self._exclude_file = exclude
        if exclude:
            from pathlib import Path
            name = Path(exclude).name
            self._exclude_row.set_subtitle(name)
        else:
            self._exclude_row.set_subtitle('No exclude file')

        # restic
        restic = job.get('restic') or {}
        self._restic_mode_row.set_selected(RESTIC_MODES.index(restic.get('mode', 'backup'))
                                           if restic.get('mode', 'backup') in RESTIC_MODES else 0)
        self._paths_view.get_buffer().set_text('\n'.join(restic.get('paths', [])))
        if restic.get('from_job') in self._from_ids:
            self._from_job_row.set_selected(self._from_ids.index(restic['from_job']))
        self._password_row.set_text(restic.get('password_file', ''))
        self._host_row.set_text(restic.get('host', ''))
        keep = restic.get('keep', {})
        for key, row in self._keep_rows.items():
            row.set_value(int(keep.get(key, 0) or 0) if keep else row.get_value())
        self._prune_row.set_value(int(restic.get('prune_every_days', 7)))
        self._check_row.set_value(int(restic.get('check_every_days', 7)))
        self._subset_row.set_text(restic.get('check_subset', '5%'))
        self._meta_row.set_active(job.get('pre_command') == META_COMMAND)

        # Rsync options
        rsync_opts = job.get('rsync_options', {})
        delete_mode = rsync_opts.get('delete_mode', 'before')
        delete_map = {'before': 0, 'during': 1, 'after': 2, 'disabled': 3}
        self._delete_mode_row.set_selected(delete_map.get(delete_mode, 0))

        for key, row in self._rsync_switches.items():
            row.set_active(rsync_opts.get(key, False))

        self._max_size_row.set_text(rsync_opts.get('max_size', ''))
        self._min_size_row.set_text(rsync_opts.get('min_size', ''))

    def _on_kind_changed(self, *args):
        """Show what the kind of job asks for: a source and rsync options for a
        mirror; paths or a job to copy, and retention, for snapshots."""
        self._engine = ENGINES[self._engine_row.get_selected()]
        self._scope = SCOPES[self._scope_row.get_selected()]
        restic = self._engine == 'restic'
        copy = restic and RESTIC_MODES[self._restic_mode_row.get_selected()] == 'copy'
        self._source_row.set_visible(not restic)
        self._restic_mode_row.set_visible(restic)
        self._paths_group.set_visible(restic and not copy)
        self._from_job_row.set_visible(copy)
        self._restic_group.set_visible(restic)
        self._rsync_group.set_visible(not restic)
        self._archive_row.set_visible(not restic)
        self._bwlimit_row.set_visible(not restic)
        self._meta_row.set_visible(restic and not copy and self._scope == 'system')
        if restic and not self._password_row.get_text() and self._scope == 'system':
            self._password_row.set_text(str(paths.system_config_dir() / 'keys' / 'NAME.key'))

    def _on_freq_changed(self, combo, pspec):
        idx = combo.get_selected()
        mode = FREQ_MODES[idx] if 0 <= idx < len(FREQ_MODES) else 'Weekly'

        self._interval_row.set_visible(mode in ('Weekly', 'Monthly'))
        self._weekday_row.set_visible(mode == 'Weekly')
        self._month_day_row.set_visible(mode == 'Monthly')
        self._custom_expr_row.set_visible(mode == 'Custom')

        suffixes = {'Weekly': 'week(s)', 'Monthly': 'month(s)'}
        if mode in suffixes:
            self._interval_row.set_title(f'Repeat every ... {suffixes[mode]}')

        self._update_schedule_summary()

    def _on_schedule_detail_changed(self, *args):
        self._update_schedule_summary()

    def _get_time(self) -> tuple[int, int]:
        try:
            h = max(0, min(23, int(self._hour_entry.get_text() or '0')))
        except ValueError:
            h = 0
        try:
            m = max(0, min(59, int(self._minute_entry.get_text() or '0')))
        except ValueError:
            m = 0
        return h, m

    def _set_time(self, h: int, m: int):
        self._hour_entry.set_text(f'{h:02d}')
        self._minute_entry.set_text(f'{m:02d}')

    def _on_time_entry_changed(self, entry):
        self._on_schedule_detail_changed()

    def _on_time_plus(self, btn):
        h, m = self._get_time()
        total = h * 60 + m + 30
        self._set_time((total // 60) % 24, total % 60)

    def _on_time_minus(self, btn):
        h, m = self._get_time()
        total = h * 60 + m - 30
        if total < 0:
            total += 24 * 60
        self._set_time((total // 60) % 24, total % 60)

    def _update_schedule_summary(self):
        """Build and display a human-readable schedule summary."""
        idx = self._freq_dropdown.get_selected()
        mode = FREQ_MODES[idx] if 0 <= idx < len(FREQ_MODES) else 'Weekly'

        if mode == 'Manual only':
            self._schedule_summary.set_subtitle('Manual only — no automatic scheduling')
            return
        if mode == 'Custom':
            self._schedule_summary.set_subtitle(self._custom_expr_row.get_text() or '(enter expression)')
            return

        interval = int(self._interval_row.get_value())
        hour, minute = self._get_time()
        time_str = f'{hour:02d}:{minute:02d}'

        if mode == 'Weekly':
            days = [WEEKDAY_LABELS[i] for i, btn in enumerate(self._weekday_buttons) if btn.get_active()]
            if len(days) == 7:
                days_str = 'every day'
            elif len(days) == 5 and all(WEEKDAY_LABELS[i] in days for i in range(5)):
                days_str = 'weekdays'
            elif len(days) == 2 and all(WEEKDAY_LABELS[i] in days for i in (5, 6)):
                days_str = 'weekends'
            else:
                days_str = ', '.join(days) if days else 'no days selected'
            if interval == 1:
                text = f'Every week on {days_str} at {time_str}'
            else:
                text = f'Every {interval} weeks on {days_str} at {time_str}'
            # Special case: all 7 days, interval 1 = daily
            if len(days) == 7 and interval == 1:
                text = f'Every day at {time_str}'
        elif mode == 'Monthly':
            day = int(self._month_day_row.get_value())
            if interval == 1:
                text = f'Monthly on day {day} at {time_str}'
            else:
                text = f'Every {interval} months on day {day} at {time_str}'
        else:
            text = self._build_calendar_expression()

        self._schedule_summary.set_subtitle(text)

    def _build_calendar_expression(self) -> str:
        """Build a systemd OnCalendar= expression from the UI state."""
        idx = self._freq_dropdown.get_selected()
        mode = FREQ_MODES[idx] if 0 <= idx < len(FREQ_MODES) else 'Weekly'

        if mode == 'Manual only':
            return ''
        if mode == 'Custom':
            return self._custom_expr_row.get_text().strip() or 'daily'

        interval = int(self._interval_row.get_value())
        hour, minute = self._get_time()
        time_part = f'{hour:02d}:{minute:02d}:00'

        if mode == 'Weekly':
            days = [WEEKDAY_SYSTEMD[i] for i, btn in enumerate(self._weekday_buttons) if btn.get_active()]
            if not days:
                days = ['Mon']
            day_str = ','.join(days)
            # All 7 days selected = daily expression (cleaner)
            if len(days) == 7:
                return f'*-*-* {time_part}'
            return f'{day_str} *-*-* {time_part}'

        if mode == 'Monthly':
            day = int(self._month_day_row.get_value())
            if interval == 1:
                return f'*-*-{day:02d} {time_part}'
            return f'*-1/{interval}-{day:02d} {time_part}'

        return 'daily'

    def _populate_schedule_from_expression(self, expr: str):
        """Parse a systemd calendar expression and set the UI controls."""
        import re
        expr = expr.strip()

        # Try to detect mode from expression
        # Weekly: "Mon,Wed *-*-* HH:MM:SS" or "Mon *-*-* HH:MM:SS"
        weekday_match = re.match(
            r'^((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:,(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))*)\s+\*-\*-\*\s+(\d{1,2}):(\d{2}):(\d{2})$',
            expr)
        if weekday_match:
            self._freq_dropdown.set_selected(FREQ_MODES.index('Weekly'))
            days = weekday_match.group(1).split(',')
            for i, day in enumerate(WEEKDAY_SYSTEMD):
                self._weekday_buttons[i].set_active(day in days)
            self._set_time(int(weekday_match.group(2)), int(weekday_match.group(3)))
            self._interval_row.set_value(1)
            self._update_schedule_summary()
            return

        # Monthly: "*-*-DD HH:MM:SS" or "*-1/N-DD HH:MM:SS"
        monthly_match = re.match(
            r'^\*-(?:1/(\d+)|\*)-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})$', expr)
        if monthly_match:
            self._freq_dropdown.set_selected(FREQ_MODES.index('Monthly'))
            interval_part = monthly_match.group(1)
            if interval_part:
                self._interval_row.set_value(int(interval_part))
            else:
                self._interval_row.set_value(1)
            self._month_day_row.set_value(int(monthly_match.group(2)))
            self._set_time(int(monthly_match.group(3)), int(monthly_match.group(4)))
            self._update_schedule_summary()
            return

        # Daily: "*-*-* HH:MM:SS" → Weekly with all days
        daily_match = re.match(r'^\*-\*-\*\s+(\d{1,2}):(\d{2}):(\d{2})$', expr)
        if daily_match:
            self._freq_dropdown.set_selected(FREQ_MODES.index('Weekly'))
            self._interval_row.set_value(1)
            for btn in self._weekday_buttons:
                btn.set_active(True)
            self._set_time(int(daily_match.group(1)), int(daily_match.group(2)))
            self._update_schedule_summary()
            return

        # Daily interval: "*-*-1/N HH:MM:SS" → Custom
        daily_interval_match = re.match(r'^\*-\*-1/(\d+)\s+(\d{1,2}):(\d{2}):(\d{2})$', expr)
        if daily_interval_match:
            self._freq_dropdown.set_selected(FREQ_MODES.index('Custom'))
            self._custom_expr_row.set_text(expr)
            self._set_time(int(daily_interval_match.group(2)), int(daily_interval_match.group(3)))
            self._update_schedule_summary()
            return

        # Simple keywords
        if expr == 'daily':
            self._freq_dropdown.set_selected(FREQ_MODES.index('Weekly'))
            self._interval_row.set_value(1)
            for btn in self._weekday_buttons:
                btn.set_active(True)
            self._update_schedule_summary()
            return

        # Fallback: custom
        self._freq_dropdown.set_selected(FREQ_MODES.index('Custom'))
        self._custom_expr_row.set_text(expr)
        self._update_schedule_summary()

    def _pick_folder(self, entry_row: Adw.EntryRow):
        """Open a native folder picker dialog."""
        dialog = Gtk.FileDialog(title='Select folder')
        dialog.select_folder(
            self.get_root(),
            None,
            lambda d, res: self._on_folder_picked(d, res, entry_row),
        )

    def _on_folder_picked(self, dialog, result, entry_row):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                entry_row.set_text(folder.get_path())
        except Exception:
            pass  # User cancelled

    def _on_edit_exclusions(self, row):
        """Navigate to exclusion editor."""
        from backup_monitor.views.exclusion_editor import ExclusionEditorPage

        # Determine exclude file path
        exclude_file = self._exclude_file
        directory = paths.system_source_dir() if self._scope == 'system' else None
        if not exclude_file and self._job_id:
            exclude_file = create_exclude_file(self._job_id, directory, engine=self._engine)
        elif not exclude_file:
            # New job, create temp path based on name
            name = self._name_row.get_text().strip() or 'new'
            import re
            safe_name = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
            exclude_file = create_exclude_file(f'backup-{safe_name}', directory, engine=self._engine)
        # Remembered for the save: a new job has no stored exclude file yet.
        self._exclude_file = exclude_file
        self._exclude_row.set_subtitle(Path(exclude_file).name)

        editor = ExclusionEditorPage(self._exclude_path(exclude_file))
        nav = self.get_root()
        if hasattr(nav, 'push_page'):
            nav.push_page(editor)
        else:
            # Try to find navigation view
            self._find_nav_view().push(editor)

    def _exclude_path(self, name: str) -> str:
        """A system job's exclude file sits next to system/jobs.json."""
        if self._scope == 'system' and name and '/' not in name:
            return str(paths.system_source_dir() / name)
        return name

    def _find_nav_view(self):
        """Walk up the widget tree to find the AdwNavigationView."""
        widget = self.get_parent()
        while widget:
            if isinstance(widget, Adw.NavigationView):
                return widget
            widget = widget.get_parent()
        return None

    def _collect_form_data(self) -> dict:
        """Read form fields and return a job dict."""
        # Schedule
        idx = self._freq_dropdown.get_selected()
        mode = FREQ_MODES[idx] if 0 <= idx < len(FREQ_MODES) else 'Daily'

        if mode == 'Manual only':
            schedule = {'type': 'manual', 'expression': '', 'randomized_delay_sec': 0}
        else:
            schedule = {
                'type': 'calendar',
                'expression': self._build_calendar_expression(),
                'randomized_delay_sec': 0,
            }

        exclude_file = self._exclude_file

        # Rsync options
        delete_modes = ['before', 'during', 'after', 'disabled']
        delete_idx = self._delete_mode_row.get_selected()
        rsync_options = {
            'delete_mode': delete_modes[delete_idx] if 0 <= delete_idx < 4 else 'before',
        }
        for key, row in self._rsync_switches.items():
            rsync_options[key] = row.get_active()
        rsync_options['max_size'] = self._max_size_row.get_text().strip()
        rsync_options['min_size'] = self._min_size_row.get_text().strip()

        data = {
            'name': self._name_row.get_text().strip(),
            'description': self._desc_row.get_text().strip() or f'Backup {self._name_row.get_text().strip()}',
            'source': self._source_row.get_text().strip() if self._engine == 'rsync' else '',
            'destination': self._dest_row.get_text().strip(),
            'exclude_file': exclude_file,
            'archive_days': int(self._archive_row.get_value()),
            'bandwidth_limit_kbps': int(self._bwlimit_row.get_value()),
            'schedule': schedule,
            'rsync_options': rsync_options,
            'nice': self._job_data.get('nice', 10) if self._job_data else 10,
            'io_priority': self._job_data.get('io_priority', 7) if self._job_data else 7,
            'notifications': self._job_data.get('notifications', {
                'on_start': True, 'on_complete': True, 'on_error': True,
            }) if self._job_data else {'on_start': True, 'on_complete': True, 'on_error': True},
            'enabled': self._enabled_switch.get_active(),
        }
        old = self._job_data or {}
        if self._scope == 'system':
            data['scope'] = 'system'
            # A system job belongs to the machine it was set up on.
            data['hosts'] = old.get('hosts') or [paths.host_name()]
        if old.get('run_after'):
            data['run_after'] = old['run_after']
        if self._engine == 'restic':
            data['engine'] = 'restic'
            data.pop('rsync_options')
            data.pop('archive_days')
            data.pop('bandwidth_limit_kbps')
            mode = RESTIC_MODES[self._restic_mode_row.get_selected()]
            buf = self._paths_view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            restic = {
                'mode': mode,
                'password_file': self._password_row.get_text().strip(),
                'keep': {k: int(r.get_value()) for k, r in self._keep_rows.items() if r.get_value() > 0},
                'prune_every_days': int(self._prune_row.get_value()),
                'check_every_days': int(self._check_row.get_value()),
                'check_subset': self._subset_row.get_text().strip() or '5%',
            }
            if self._host_row.get_text().strip():
                restic['host'] = self._host_row.get_text().strip()
            if mode == 'backup':
                restic['paths'] = [line.strip() for line in text.splitlines() if line.strip()]
            elif self._from_ids:
                restic['from_job'] = self._from_ids[self._from_job_row.get_selected()]
            data['restic'] = restic
            if mode == 'backup' and self._scope == 'system' and self._meta_row.get_active():
                data['pre_command'] = META_COMMAND
        return data

    def _problem(self, data: dict) -> str:
        if not data['name']:
            return 'Name is required'
        if not data['destination']:
            return 'Destination path is required'
        if self._engine == 'rsync':
            return '' if data['source'] else 'Source path is required'
        restic = data['restic']
        if restic['mode'] == 'backup' and not restic.get('paths'):
            return 'List at least one path to back up'
        if restic['mode'] == 'backup' and any(not p.startswith('/') for p in restic['paths']):
            return 'Paths must be absolute'
        if restic['mode'] == 'copy' and not restic.get('from_job'):
            return 'Choose the job whose snapshots to copy'
        if not restic['password_file']:
            return 'The repository password file is required'
        return ''

    def _on_save(self, btn):
        data = self._collect_form_data()
        problem = self._problem(data)
        if problem:
            self._show_toast(problem)
            return

        if self._job_id:
            self._manager.update_job(self._job_id, data)
        else:
            self._job_id = self._manager.create_job(data)
        if self._scope == 'system':
            self._install_system_jobs(self._job_id, 'saved')
            return
        self.emit('saved', self._job_id)
        self._go_back()

    def _go_back(self):
        nav = self._find_nav_view()
        if nav:
            nav.pop()

    def _install_system_jobs(self, job_id: str, signal: str):
        """System jobs are saved to the overlay; installing them takes root
        (`mirror-backup system apply` through pkexec, which asks for the password)."""
        self._show_toast('Installing the system jobs…')
        try:
            proc = Gio.Subprocess.new(self._manager.system_apply_command(),
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self._show_result('System jobs not installed', e.message)
            return
        proc.communicate_utf8_async(None, None, self._on_installed, (job_id, signal))

    def _on_installed(self, proc, result, data):
        job_id, signal = data
        try:
            _, output, _ = proc.communicate_utf8_finish(result)
        except GLib.Error as e:
            output = e.message
        if proc.get_successful():
            self.emit(signal, job_id)
            self._go_back()
            return
        # 126/127: pkexec was dismissed or not allowed — the change stays saved
        # in the overlay, and installing it later picks it up.
        self._show_result('System jobs saved, not installed',
                          (output or '').strip() + '\n\nInstall them later with:\n'
                          f'sudo {paths.SYSTEM_COMMAND} system apply')
        self.emit(signal, job_id)

    def _show_result(self, heading: str, body: str):
        dialog = Adw.AlertDialog(heading=heading, body=body[:5000])
        dialog.set_body_use_markup(False)
        dialog.add_response('ok', 'OK')
        dialog.present(self.get_root())

    def _on_delete(self, btn):
        """Show confirmation dialog before deleting."""
        dialog = Adw.AlertDialog(
            heading='Delete Backup Job?',
            body=f'This will remove "{self._job_data["name"]}" and its systemd timer. '
                 f'Existing backups on disk will not be deleted.',
        )
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('delete', 'Delete')
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.connect('response', self._on_delete_confirmed)
        dialog.present(self.get_root())

    def _on_delete_confirmed(self, dialog, response):
        if response == 'delete' and self._job_id:
            self._manager.delete_job(self._job_id)
            if self._scope == 'system':
                self._install_system_jobs(self._job_id, 'deleted')
                return
            self.emit('deleted', self._job_id)
            self._go_back()

    def _on_dry_run(self, row):
        """Run rsync --dry-run and show results in a dialog."""
        if not self._job_data:
            return
        if self._engine == 'restic':
            self._restic_dry_run()
            return

        source = self._source_row.get_text().strip()
        dest = self._dest_row.get_text().strip()
        if not source or not dest:
            self._show_toast('Source and destination are required')
            return

        import subprocess
        # Same protected paths as backup-sync: its own state and the archive.
        args = [
            'rsync', '-a', '--delete', '--dry-run',
            '--info=name1', '--human-readable',
            '--exclude=.mirror-backup/', '--exclude=/.archive/',
            source, dest,
        ]

        exclude = paths.resolve_config_path(self._exclude_path(self._exclude_file))
        if exclude and Path(exclude).is_file():
            args.insert(-2, f'--exclude-from={exclude}')

        self._show_toast('Running dry-run...')

        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=60,
            )
            output = result.stdout.strip() or '(no changes needed)'
            if result.returncode != 0 and result.stderr:
                output = f'Error:\n{result.stderr.strip()}\n\n{output}'
        except subprocess.TimeoutExpired:
            output = 'Dry-run timed out after 60 seconds'
        except OSError as e:
            output = f'Failed to run rsync: {e}'

        # Show result in a dialog
        dialog = Adw.AlertDialog(
            heading='Dry Run Results',
            body=output[:5000],  # Limit size
        )
        dialog.set_body_use_markup(False)
        dialog.add_response('ok', 'OK')
        dialog.present(self.get_root())

    def _restic_dry_run(self):
        """`mirror-backup dry-run` of the saved job: what its next snapshot
        would add. A system job's reads every file, so it runs as root."""
        if (self._job_data.get('restic') or {}).get('mode', 'backup') != 'backup':
            self._show_toast('A copy job has nothing to preview')
            return
        if self._scope == 'system':
            cmd = ['pkexec', paths.SYSTEM_COMMAND, 'dry-run', self._job_id]
        else:
            cmd = [sys.executable, '-m', 'backup_monitor', 'dry-run', self._job_id]
        self._show_toast('Running dry-run…')
        try:
            proc = Gio.Subprocess.new(cmd, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self._show_result('Dry Run Results', e.message)
            return

        def done(p, res, _data):
            try:
                _, out, _ = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                out = e.message
            self._show_result('Dry Run Results', (out or '').strip() or '(no output)')
        proc.communicate_utf8_async(None, None, done, None)

    def _show_toast(self, message: str):
        root = self.get_root()
        if hasattr(root, 'add_toast'):
            root.add_toast(Adw.Toast(title=message))
