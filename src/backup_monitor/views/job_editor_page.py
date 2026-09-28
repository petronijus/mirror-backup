"""Job editor page — form for creating/editing backup jobs."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GObject

from pathlib import Path

from backup_monitor import paths
from backup_monitor.services.job_manager import JobManager, create_exclude_file

FREQ_MODES = ['Weekly', 'Monthly', 'Custom', 'Manual only']
WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
WEEKDAY_SYSTEMD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


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

        # ── Paths ──
        paths_group = Adw.PreferencesGroup(title='Paths')
        form.append(paths_group)

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

        # ── Rsync Options ──
        rsync_group = Adw.PreferencesGroup(
            title='Rsync Options',
            description='Fine-tune rsync behavior for this job',
        )
        form.append(rsync_group)

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

        # Rsync options
        rsync_opts = job.get('rsync_options', {})
        delete_mode = rsync_opts.get('delete_mode', 'before')
        delete_map = {'before': 0, 'during': 1, 'after': 2, 'disabled': 3}
        self._delete_mode_row.set_selected(delete_map.get(delete_mode, 0))

        for key, row in self._rsync_switches.items():
            row.set_active(rsync_opts.get(key, False))

        self._max_size_row.set_text(rsync_opts.get('max_size', ''))
        self._min_size_row.set_text(rsync_opts.get('min_size', ''))

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
        if not exclude_file and self._job_id:
            exclude_file = create_exclude_file(self._job_id)
        elif not exclude_file:
            # New job, create temp path based on name
            name = self._name_row.get_text().strip() or 'new'
            import re
            safe_name = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
            exclude_file = create_exclude_file(f'backup-{safe_name}')
        # Remembered for the save: a new job has no stored exclude file yet.
        self._exclude_file = exclude_file
        self._exclude_row.set_subtitle(Path(exclude_file).name)

        editor = ExclusionEditorPage(exclude_file)
        nav = self.get_root()
        if hasattr(nav, 'push_page'):
            nav.push_page(editor)
        else:
            # Try to find navigation view
            self._find_nav_view().push(editor)

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

        return {
            'name': self._name_row.get_text().strip(),
            'description': self._desc_row.get_text().strip() or f'Backup {self._name_row.get_text().strip()}',
            'source': self._source_row.get_text().strip(),
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

    def _on_save(self, btn):
        data = self._collect_form_data()

        # Basic validation
        if not data['name']:
            self._show_toast('Name is required')
            return
        if not data['source']:
            self._show_toast('Source path is required')
            return
        if not data['destination']:
            self._show_toast('Destination path is required')
            return

        if self._job_id:
            self._manager.update_job(self._job_id, data)
            self.emit('saved', self._job_id)
        else:
            job_id = self._manager.create_job(data)
            self._job_id = job_id
            self.emit('saved', job_id)

        # Navigate back
        nav = self._find_nav_view()
        if nav:
            nav.pop()

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
            self.emit('deleted', self._job_id)
            nav = self._find_nav_view()
            if nav:
                nav.pop()

    def _on_dry_run(self, row):
        """Run rsync --dry-run and show results in a dialog."""
        if not self._job_data:
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

        exclude = paths.resolve_config_path(self._exclude_file)
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

    def _show_toast(self, message: str):
        root = self.get_root()
        if hasattr(root, 'add_toast'):
            root.add_toast(Adw.Toast(title=message))
