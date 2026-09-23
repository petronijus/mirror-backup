"""Dashboard page — overview of all backup jobs."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GObject

from backup_monitor.models.job import BackupJob
from backup_monitor.widgets.job_card import JobCard


class DashboardPage(Gtk.Box):
    """Main dashboard showing all job cards in a responsive grid."""

    __gtype_name__ = 'DashboardPage'

    __gsignals__ = {
        'edit-requested': (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # job_id
        'detail-requested': (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # job_id
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._cards: list[JobCard] = []

        # Scrollable area
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        self.append(scroll)

        # Clamp for max content width (looks good on ultrawide)
        clamp = Adw.Clamp(
            maximum_size=800,
            margin_top=16,
            margin_bottom=16,
            margin_start=12,
            margin_end=12,
        )
        scroll.set_child(clamp)

        # Main content
        self._content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
        )
        clamp.set_child(self._content_box)

        # Title row
        title_row = Gtk.Box(spacing=12)
        self._content_box.append(title_row)

        title = Gtk.Label(
            label='Backup Jobs',
            xalign=0,
            hexpand=True,
            css_classes=['title-1'],
        )
        title_row.append(title)

        # Summary label
        self._summary_label = Gtk.Label(
            xalign=1,
            css_classes=['dim-label'],
            valign=Gtk.Align.CENTER,
        )
        title_row.append(self._summary_label)

        # Job cards container
        self._cards_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        self._content_box.append(self._cards_box)

    def set_jobs(self, jobs: list[BackupJob]):
        """Create job cards for the given jobs."""
        while child := self._cards_box.get_first_child():
            self._cards_box.remove(child)
        self._cards.clear()

        for job in jobs:
            card = JobCard(job)
            card.connect('edit-requested', self._on_card_edit)
            card.connect('detail-requested', self._on_card_detail)
            self._cards.append(card)
            self._cards_box.append(card)

    def _on_card_edit(self, card, job_id):
        self.emit('edit-requested', job_id)

    def _on_card_detail(self, card, job_id):
        self.emit('detail-requested', job_id)

    def update(self):
        """Refresh all job cards and the summary."""
        active = 0
        queued = 0
        postponed = 0
        errors = 0

        for card in self._cards:
            card.update()
            state = card.job.status.state
            if state in ('running', 'scanning', 'paused'):
                active += 1
            elif state == 'queued':
                queued += 1
            elif state == 'deferred':
                postponed += 1
            elif state == 'error':
                errors += 1

        parts = []
        if active:
            parts.append(f'{active} active')
        if queued:
            parts.append(f'{queued} queued')
        if postponed:
            parts.append(f'{postponed} postponed')
        if errors:
            parts.append(f'{errors} error{"s" if errors > 1 else ""}')
        self._summary_label.set_label(', '.join(parts) if parts else 'All idle')
