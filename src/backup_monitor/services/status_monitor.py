"""Status monitor — polls each job's status file and emits signals on change."""

from __future__ import annotations

import gi
gi.require_version('Gio', '2.0')
from gi.repository import GLib, Gio, GObject

from backup_monitor import paths
from backup_monitor.models.job import BackupJob
from backup_monitor.models.job_history import last_run
from backup_monitor.services import systemd_service


class StatusMonitor(GObject.Object):
    """Periodically reads the jobs' status files, last runs and timer info, emits 'updated'.

    Status files live in the destinations, often on NFS: inotify there only
    sees writes made by this machine, so the one-second poll stays the ground
    truth and the monitors merely make local changes show up sooner.
    """

    __gsignals__ = {
        'updated': (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    POLL_INTERVAL_MS = 1000
    TIMER_POLL_INTERVAL_S = 30  # Timer info changes slowly, poll less often

    def __init__(self, jobs: list[BackupJob]):
        super().__init__()
        self._jobs = jobs
        self._poll_id = None
        self._timer_poll_id = None
        self._file_monitors = []
        self._states: dict[str, str] = {}

    @property
    def jobs(self) -> list[BackupJob]:
        return self._jobs

    def start(self):
        """Start polling and file monitoring."""
        self._refresh_statuses()
        self._refresh_timer_info()

        self._poll_id = GLib.timeout_add(
            self.POLL_INTERVAL_MS, self._on_poll)

        self._timer_poll_id = GLib.timeout_add_seconds(
            self.TIMER_POLL_INTERVAL_S, self._on_timer_poll)

        for job in self._jobs:
            state_dir = paths.job_paths(job.destination).state_dir
            if not state_dir.is_dir():
                continue
            try:
                monitor = Gio.File.new_for_path(str(state_dir)).monitor_directory(
                    Gio.FileMonitorFlags.NONE, None)
                monitor.connect('changed', self._on_file_changed)
                self._file_monitors.append(monitor)
            except GLib.Error:
                pass  # Polling is the fallback

    def stop(self):
        """Stop all monitoring."""
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = None
        if self._timer_poll_id:
            GLib.source_remove(self._timer_poll_id)
            self._timer_poll_id = None
        for monitor in self._file_monitors:
            monitor.cancel()
        self._file_monitors = []

    def _on_poll(self) -> bool:
        self._refresh_statuses()
        return GLib.SOURCE_CONTINUE

    def _on_timer_poll(self) -> bool:
        self._refresh_timer_info()
        return GLib.SOURCE_CONTINUE

    def _on_file_changed(self, monitor, file, other_file, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CREATED):
            self._refresh_statuses()

    def _refresh_statuses(self):
        for job in self._jobs:
            state = job.read_status().state
            # A run just ended (here or elsewhere): its history has a new entry.
            if self._states.get(job.id) != state:
                self._refresh_last_run(job)
                self._states[job.id] = state
        self.emit('updated')

    @staticmethod
    def _refresh_last_run(job: BackupJob):
        entry = last_run(job.destination)
        job.last_run = entry.started if entry else ''
        job.last_run_host = entry.host if entry else ''

    def _refresh_timer_info(self):
        for job in self._jobs:
            self._refresh_last_run(job)
        timer_names = [f'{j.id}.timer' for j in self._jobs]
        systemd_service.get_all_timer_info(timer_names, self._on_timer_info)

    def _on_timer_info(self, results: dict):
        for job in self._jobs:
            timer_name = f'{job.id}.timer'
            if timer_name in results:
                next_run, _ = results[timer_name]
                job.next_run = next_run
        self.emit('updated')
