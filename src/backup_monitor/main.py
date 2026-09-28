"""Mirror Backup — GTK4/libadwaita desktop app for managing rsync backups.

The app itself. `python3 -m backup_monitor` is the entry point that also runs
the command line (backup_monitor.cli) without loading GTK.
"""

from __future__ import annotations

import sys
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gdk, Gio

from backup_monitor import APP_ID
from backup_monitor import desktop_theme
from backup_monitor.window import BackupMonitorWindow


class BackupMonitorApp(Adw.Application):
    """Main application class."""

    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._load_css()
        # On Omarchy: the current theme's look, kept in step with theme switches.
        self._omarchy_style = desktop_theme.omarchy_style()
        self._omarchy_style.start()

    def do_activate(self):
        win = self.get_active_window()
        if win:
            win.present()
            return

        win = BackupMonitorWindow(self)
        win.present()

    def _load_css(self):
        """Load custom CSS from data/style.css."""
        # __file__ = .../backup_monitor/main.py
        # In dev:    repo/src/backup_monitor/main.py → repo/data/style.css
        # Bundled:   ext_dir/app/backup_monitor/main.py → ext_dir/data/style.css
        # Both resolve via: parent(main.py) / .. / .. / data / style.css
        css_paths = [
            Path(__file__).parent.parent.parent / 'data' / 'style.css',
        ]
        for css_path in css_paths:
            if css_path.is_file():
                provider = Gtk.CssProvider()
                provider.load_from_path(str(css_path))
                display = Gdk.Display.get_default()
                if display:
                    Gtk.StyleContext.add_provider_for_display(
                        display, provider,
                        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                    )
                break


def main():
    # Installs up to 0.5 ran `python3 -m backup_monitor.main --regenerate-units`.
    from backup_monitor import cli
    if len(sys.argv) > 1 and sys.argv[1] in (*cli.COMMANDS, '--regenerate-units'):
        return cli.main(sys.argv[1:])
    app = BackupMonitorApp()
    return app.run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
