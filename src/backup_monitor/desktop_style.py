"""Applies desktop_theme's stylesheet to the running app and follows theme
switches (`omarchy theme set …` rewrites the current theme in place)."""

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from backup_monitor import desktop_theme

# Above the app's own stylesheet (APPLICATION), so the theme wins over it too.
PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1


class DesktopStyle:
    """The Omarchy look while the app runs on Omarchy; nothing elsewhere."""

    def __init__(self):
        self._provider: Gtk.CssProvider | None = None
        self._monitors: list[Gio.FileMonitor] = []
        self._reload_id = 0

    def start(self):
        if desktop_theme.wanted_theme() != 'omarchy':
            return
        self._apply()
        # The theme dir's files are rewritten on a switch; theme.name names it.
        theme_dir = desktop_theme.omarchy_theme_dir()
        for path in (theme_dir.parent / 'theme.name', theme_dir / 'colors.toml', theme_dir / 'shell.toml'):
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect('changed', self._on_changed)
            self._monitors.append(monitor)

    def _on_changed(self, *_args):
        # A switch touches several files in a burst: apply once it settles.
        if self._reload_id:
            GLib.source_remove(self._reload_id)
        self._reload_id = GLib.timeout_add(400, self._reload)

    def _reload(self) -> bool:
        self._reload_id = 0
        self._apply()
        return GLib.SOURCE_REMOVE

    def _apply(self):
        palette = desktop_theme.load_palette()
        display = Gdk.Display.get_default()
        if palette is None or display is None:
            return
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if palette.dark else Adw.ColorScheme.FORCE_LIGHT)
        provider = Gtk.CssProvider()
        provider.load_from_string(desktop_theme.build_css(palette))
        if self._provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._provider)
        Gtk.StyleContext.add_provider_for_display(display, provider, PRIORITY)
        self._provider = provider
