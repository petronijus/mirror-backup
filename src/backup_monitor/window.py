"""Main application window."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio

from backup_monitor import paths
from backup_monitor.models.job import BackupJob
from backup_monitor.models.settings import Settings
from backup_monitor.services.job_manager import JobManager
from backup_monitor.services.status_monitor import StatusMonitor
from backup_monitor.views.dashboard_page import DashboardPage
from backup_monitor.views.job_editor_page import JobEditorPage
from backup_monitor.views.job_detail_page import JobDetailPage
from backup_monitor.views.log_viewer import LogViewerPage
from backup_monitor.views.preferences_window import PreferencesWindow


class BackupMonitorWindow(Adw.ApplicationWindow):
    """Main window with navigation and dashboard."""

    __gtype_name__ = 'BackupMonitorWindow'

    def __init__(self, app):
        super().__init__(
            application=app,
            title='Mirror Backup for GNOME',
            default_width=550,
            default_height=700,
        )
        self.set_size_request(420, 400)

        self._job_manager = JobManager()
        self._settings = Settings()

        # Toast overlay for notifications
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        # Navigation view for page stack
        self._nav_view = Adw.NavigationView()
        self._toast_overlay.set_child(self._nav_view)

        # Dashboard as root page
        dashboard_nav_page = Adw.NavigationPage(title='Mirror Backup')
        self._nav_view.push(dashboard_nav_page)

        # Dashboard toolbar view (header + content)
        toolbar = Adw.ToolbarView()
        dashboard_nav_page.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # New job button in header
        new_btn = Gtk.Button(
            icon_name='list-add-symbolic',
            tooltip_text='New backup job',
        )
        new_btn.connect('clicked', self._on_new_job)
        header.pack_start(new_btn)

        # App menu button (right side)
        menu_model = Gio.Menu()
        menu_model.append('Preferences', 'win.preferences')
        menu_model.append('Keyboard Shortcuts', 'win.show-help-overlay')
        menu_model.append('About', 'win.about')

        menu_btn = Gtk.MenuButton(
            icon_name='open-menu-symbolic',
            menu_model=menu_model,
            tooltip_text='Menu',
        )
        header.pack_end(menu_btn)

        # Actions
        prefs_action = Gio.SimpleAction.new('preferences', None)
        prefs_action.connect('activate', self._on_preferences)
        self.add_action(prefs_action)

        about_action = Gio.SimpleAction.new('about', None)
        about_action.connect('activate', self._on_about)
        self.add_action(about_action)

        # Keyboard shortcuts
        self._setup_shortcuts(app)

        # Dashboard content
        self._dashboard = DashboardPage()
        self._dashboard.connect('edit-requested', self._on_edit_job)
        self._dashboard.connect('detail-requested', self._on_detail_job)
        toolbar.set_content(self._dashboard)

        # Load jobs and start monitoring
        self._reload_jobs()

    def _setup_shortcuts(self, app):
        """Register keyboard shortcuts."""
        # Ctrl+N — new job
        app.set_accels_for_action('win.new-job', ['<Control>n'])
        new_action = Gio.SimpleAction.new('new-job', None)
        new_action.connect('activate', lambda a, p: self._on_new_job(None))
        self.add_action(new_action)

        # Ctrl+, — preferences
        app.set_accels_for_action('win.preferences', ['<Control>comma'])

        # Ctrl+R — refresh (force status poll)
        app.set_accels_for_action('win.refresh', ['<Control>r'])
        refresh_action = Gio.SimpleAction.new('refresh', None)
        refresh_action.connect('activate', lambda a, p: self._reload_jobs())
        self.add_action(refresh_action)

        # Build shortcuts window overlay
        shortcuts_action = Gio.SimpleAction.new('show-help-overlay', None)
        shortcuts_action.connect('activate', self._on_shortcuts)
        self.add_action(shortcuts_action)
        app.set_accels_for_action('win.show-help-overlay', ['<Control>question'])

    def _reload_jobs(self):
        """Reload jobs from manager and restart monitoring."""
        if hasattr(self, '_monitor') and self._monitor:
            self._monitor.stop()

        self._jobs = self._job_manager.to_backup_jobs()
        self._dashboard.set_jobs(self._jobs)

        self._monitor = StatusMonitor(self._jobs)
        self._monitor.connect('updated', self._on_updated)
        self._monitor.start()

    def _on_updated(self, monitor):
        self._dashboard.update()

    def _on_new_job(self, btn):
        editor = JobEditorPage(self._job_manager)
        editor.connect('saved', self._on_job_saved)
        self._nav_view.push(editor)

    def _on_edit_job(self, dashboard, job_id):
        editor = JobEditorPage(self._job_manager, job_id)
        editor.connect('saved', self._on_job_saved)
        editor.connect('deleted', self._on_job_deleted)
        self._nav_view.push(editor)

    def _on_detail_job(self, dashboard, job_id):
        job = self._find_job(job_id)
        if not job:
            return
        detail = JobDetailPage(job)
        detail.connect('view-log', self._on_view_log)
        self._nav_view.push(detail)

    def _on_view_log(self, detail, job_id, job_name):
        job = next((j for j in self._jobs if j.id == job_id), None)
        if not job:
            return
        log_page = LogViewerPage(job_name, paths.job_paths(job.destination).log)
        self._nav_view.push(log_page)

    def _on_job_saved(self, editor, job_id):
        self._reload_jobs()
        self.add_toast(Adw.Toast(title='Job saved'))

    def _on_job_deleted(self, editor, job_id):
        self._reload_jobs()
        self.add_toast(Adw.Toast(title='Job deleted'))

    def _on_preferences(self, action, param):
        prefs = PreferencesWindow(self, self._settings)
        prefs.present()

    def _on_about(self, action, param):
        from backup_monitor import APP_VERSION
        about = Adw.AboutDialog(
            application_name='Mirror Backup for GNOME',
            application_icon='drive-harddisk-symbolic',
            version=APP_VERSION,
            developer_name='Petr Parkan Janda',
            comments='Monitor and manage rsync backups on Linux',
            license_type=Gtk.License.MIT_X11,
        )
        about.present(self)

    def _on_shortcuts(self, action, param):
        shortcuts = Gtk.ShortcutsWindow(
            transient_for=self,
            modal=True,
        )
        section = Gtk.ShortcutsSection(visible=True)
        group = Gtk.ShortcutsGroup(title='General', visible=True)

        for accel, title in [
            ('<Control>n', 'New backup job'),
            ('<Control>r', 'Refresh'),
            ('<Control>comma', 'Preferences'),
            ('<Control>question', 'Keyboard shortcuts'),
        ]:
            shortcut = Gtk.ShortcutsShortcut(
                accelerator=accel,
                title=title,
                visible=True,
            )
            group.append(shortcut)

        section.append(group)
        shortcuts.append(section)
        shortcuts.present()

    def _find_job(self, job_id: str) -> BackupJob | None:
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    def add_toast(self, toast):
        self._toast_overlay.add_toast(toast)

    def do_close_request(self):
        if hasattr(self, '_monitor') and self._monitor:
            self._monitor.stop()
        return False
