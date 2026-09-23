"""Job card widget — shows a single backup job's status in the dashboard."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, GObject

from backup_monitor.models.job import BackupJob
from backup_monitor.services import systemd_service
from backup_monitor.services.systemd_service import format_countdown, format_relative_past


class JobCard(Gtk.Box):
    """A card widget displaying one backup job's current state."""

    __gtype_name__ = 'JobCard'

    __gsignals__ = {
        'edit-requested': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        'detail-requested': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, job: BackupJob):
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            css_classes=['card', 'bm-job-card'],
        )
        self._job = job
        self._paused = False

        # Clickable overlay for the whole card → detail page
        click = Gtk.GestureClick()
        click.connect('released', self._on_card_clicked)
        self.add_controller(click)
        self.set_cursor_from_name('pointer')

        # Main content box with padding
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=16, margin_bottom=16,
            margin_start=16, margin_end=16,
        )
        self.append(content)

        # ── Row 1: Name + State badge ──
        row1 = Gtk.Box(spacing=12)
        content.append(row1)

        self._dot = Gtk.Label(
            label='●',
            css_classes=['bm-dot', 'bm-dot-idle'],
            valign=Gtk.Align.CENTER,
        )
        row1.append(self._dot)

        self._name_label = Gtk.Label(
            label=job.label,
            xalign=0,
            hexpand=True,
            css_classes=['bm-job-name'],
        )
        row1.append(self._name_label)

        # Streak badge — shows "Nx" for consecutive failures, hidden otherwise
        self._streak_badge = Gtk.Label(
            label='',
            css_classes=['bm-state-badge', 'bm-state-attention'],
            valign=Gtk.Align.CENTER,
            visible=False,
            tooltip_text='Failed several times in a row',
        )
        row1.append(self._streak_badge)

        self._state_badge = Gtk.Label(
            label='Idle',
            css_classes=['bm-state-badge', 'bm-state-idle'],
            valign=Gtk.Align.CENTER,
        )
        # Prevent badge from shrinking
        self._state_badge.set_size_request(60, -1)
        self._state_badge.set_halign(Gtk.Align.END)
        row1.append(self._state_badge)

        # ── Row 2: Path summary ──
        self._path_label = Gtk.Label(
            label=self._format_path_summary(),
            xalign=0,
            css_classes=['bm-job-path', 'dim-label'],
            ellipsize=3,  # PANGO_ELLIPSIZE_END
            margin_start=22,  # align with name (past the dot)
        )
        content.append(self._path_label)

        # ── Progress bar ──
        self._progress_bar = Gtk.ProgressBar(
            css_classes=['bm-progress'],
            visible=False,
        )
        content.append(self._progress_bar)

        # ── Detail rows (visible when active) ──
        self._detail_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
            visible=False,
            margin_start=22,
        )
        content.append(self._detail_box)

        self._file_label = Gtk.Label(
            xalign=0,
            css_classes=['bm-detail-file', 'dim-label'],
            ellipsize=3,
        )
        self._detail_box.append(self._file_label)

        self._stats_label = Gtk.Label(
            xalign=0,
            css_classes=['bm-detail-stats', 'dim-label'],
        )
        self._detail_box.append(self._stats_label)

        # ── Schedule / next run row ──
        self._schedule_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_start=22,
        )
        content.append(self._schedule_box)

        self._next_run_label = Gtk.Label(
            xalign=0,
            css_classes=['bm-schedule', 'dim-label'],
        )
        self._schedule_box.append(self._next_run_label)

        self._last_run_label = Gtk.Label(
            xalign=0,
            css_classes=['bm-schedule', 'dim-label'],
        )
        self._schedule_box.append(self._last_run_label)

        # ── Error row ──
        self._error_label = Gtk.Label(
            xalign=0,
            wrap=True,
            css_classes=['bm-error'],
            visible=False,
            margin_start=22,
        )
        content.append(self._error_label)

        # ── Action buttons ──
        btn_box = Gtk.Box(
            spacing=8,
            margin_top=4,
        )
        content.append(btn_box)

        edit_btn = Gtk.Button(
            icon_name='document-edit-symbolic',
            css_classes=['flat', 'circular'],
            tooltip_text='Edit job',
        )
        edit_btn.connect('clicked', self._on_edit)
        btn_box.append(edit_btn)

        spacer = Gtk.Box(hexpand=True)
        btn_box.append(spacer)

        self._start_btn = Gtk.Button(
            icon_name='media-playback-start-symbolic',
            css_classes=['flat', 'circular'],
            tooltip_text='Start backup',
        )
        self._start_btn.connect('clicked', self._on_start)
        btn_box.append(self._start_btn)

        self._pause_btn = Gtk.Button(
            icon_name='media-playback-pause-symbolic',
            css_classes=['flat', 'circular'],
            tooltip_text='Pause',
            visible=False,
        )
        self._pause_btn.connect('clicked', self._on_pause)
        btn_box.append(self._pause_btn)

        self._stop_btn = Gtk.Button(
            icon_name='media-playback-stop-symbolic',
            css_classes=['flat', 'circular', 'destructive-action'],
            tooltip_text='Stop backup',
            visible=False,
        )
        self._stop_btn.connect('clicked', self._on_stop)
        btn_box.append(self._stop_btn)

    @property
    def job(self) -> BackupJob:
        return self._job

    def update(self):
        """Refresh the card UI from the job's current status."""
        st = self._job.status
        state = st.state

        is_success = state == 'idle' and st.progress >= 100
        is_active = state in ('running', 'scanning', 'paused')
        is_queued = state == 'queued'

        # State badge
        state_names = {
            'idle': 'Idle',
            'queued': 'Queued',
            'scanning': 'Scanning',
            'running': 'Syncing',
            'paused': 'Paused',
            'deferred': 'Postponed',
            'error': 'Error',
        }
        badge_text = state_names.get(state, state)
        if is_success:
            badge_text = 'Done'

        self._state_badge.set_label(badge_text)

        # Update CSS classes for state badge
        for cls in ['bm-state-idle', 'bm-state-queued', 'bm-state-running',
                     'bm-state-scanning', 'bm-state-paused', 'bm-state-deferred',
                     'bm-state-error', 'bm-state-success']:
            self._state_badge.remove_css_class(cls)

        if is_success:
            self._state_badge.add_css_class('bm-state-success')
        else:
            self._state_badge.add_css_class(f'bm-state-{state}')

        # Dot color
        for cls in ['bm-dot-idle', 'bm-dot-queued', 'bm-dot-running',
                     'bm-dot-scanning', 'bm-dot-paused', 'bm-dot-deferred',
                     'bm-dot-error', 'bm-dot-success']:
            self._dot.remove_css_class(cls)

        if is_success:
            self._dot.add_css_class('bm-dot-success')
        else:
            self._dot.add_css_class(f'bm-dot-{state}')

        # Progress bar
        self._progress_bar.set_visible(is_active)
        if is_active:
            fraction = max(0, min(1, st.progress / 100))
            self._progress_bar.set_fraction(fraction)
            if state == 'paused':
                self._progress_bar.add_css_class('bm-progress-paused')
            else:
                self._progress_bar.remove_css_class('bm-progress-paused')

        # Detail rows
        self._detail_box.set_visible(is_active)
        if is_active:
            if state == 'scanning':
                self._file_label.set_label('Building file list\u2026')
                parts = []
                if st.scan_read:
                    parts.append(f'{st.scan_read} read')
                self._stats_label.set_label('  \u00b7  '.join(parts))
            else:
                if st.current_file:
                    self._file_label.set_label(self._shorten_path(st.current_file))
                else:
                    self._file_label.set_label('')

                parts = []
                if st.progress > 0:
                    parts.append(f'{st.progress:.0f}%')
                if st.speed:
                    parts.append(st.speed)
                if st.eta and st.eta != '0:00:00':
                    parts.append(f'ETA {st.eta}')
                if st.files_total > 0:
                    parts.append(f'{st.files_transferred}/{st.files_total} files')
                self._stats_label.set_label('  \u00b7  '.join(parts))

        # Schedule info — live countdown
        countdown = format_countdown(self._job.next_run)
        last = format_relative_past(self._job.last_run)
        if state == 'deferred':
            self._next_run_label.set_label(st.deferred_note)
        else:
            self._next_run_label.set_label(f'Next: {countdown}' if countdown else '')
        self._last_run_label.set_label(f'Last: {last}' if last else '')

        # Error
        if state == 'error' and st.error:
            self._error_label.set_label(st.error)
            self._error_label.set_visible(True)
        else:
            self._error_label.set_visible(False)

        # Persistent failure streak badge
        if st.consecutive_failures >= 2:
            self._streak_badge.set_label(f'{st.consecutive_failures}×')
            self._streak_badge.set_visible(True)
        else:
            self._streak_badge.set_visible(False)

        # Buttons
        self._start_btn.set_visible(not is_active and not is_queued)
        self._pause_btn.set_visible(is_active)
        self._stop_btn.set_visible(is_active or is_queued)

        if state == 'paused':
            self._pause_btn.set_icon_name('media-playback-start-symbolic')
            self._pause_btn.set_tooltip_text('Resume')
        else:
            self._pause_btn.set_icon_name('media-playback-pause-symbolic')
            self._pause_btn.set_tooltip_text('Pause')

    def _format_path_summary(self) -> str:
        src = self._shorten_mount(self._job.source)
        dst = self._shorten_mount(self._job.destination)
        if src and dst:
            return f'{src} \u2192 {dst}'
        return ''

    @staticmethod
    def _shorten_mount(path: str) -> str:
        if not path:
            return ''
        path = path.rstrip('/')
        parts = path.split('/')
        if len(parts) >= 3 and parts[1] == 'mnt':
            return '/'.join(parts[2:])
        return path

    @staticmethod
    def _shorten_path(p: str) -> str:
        if not p:
            return ''
        parts = p.rstrip('/').split('/')
        if len(parts) <= 2:
            return p
        return '\u2026/' + '/'.join(parts[-2:])

    def _on_card_clicked(self, gesture, n_press, x, y):
        self.emit('detail-requested', self._job.id)

    def _on_edit(self, btn):
        self.emit('edit-requested', self._job.id)

    def _on_start(self, btn):
        systemd_service.start_job(self._job.service_name)
        self._paused = False

    def _on_pause(self, btn):
        if self._paused or self._job.status.state == 'paused':
            systemd_service.resume_job(self._job.service_name)
            self._paused = False
        else:
            systemd_service.pause_job(self._job.service_name)
            self._paused = True

    def _on_stop(self, btn):
        systemd_service.stop_job(
            self._job.service_name,
            is_paused=self._paused or self._job.status.state == 'paused',
        )
        self._paused = False
