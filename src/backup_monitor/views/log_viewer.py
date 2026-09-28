"""Log viewer — scrollable log with search/filter."""

from __future__ import annotations

from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Pango




class LogViewerPage(Adw.NavigationPage):
    """Page for viewing a backup job's log file with search."""

    __gtype_name__ = 'LogViewerPage'

    def __init__(self, job_name: str, log_path: Path):
        super().__init__(title=f'{job_name} Log')
        self._log_path = log_path
        self._file_monitor = None

        # Layout
        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # Search toggle button
        search_btn = Gtk.ToggleButton(
            icon_name='system-search-symbolic',
            tooltip_text='Search log',
        )
        header.pack_end(search_btn)

        # Refresh button
        refresh_btn = Gtk.Button(
            icon_name='view-refresh-symbolic',
            tooltip_text='Reload log',
        )
        refresh_btn.connect('clicked', lambda b: self._load_log())
        header.pack_end(refresh_btn)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar.set_content(main_box)

        # Search bar
        self._search_bar = Gtk.SearchBar()
        self._search_entry = Gtk.SearchEntry(
            placeholder_text='Search in log...',
            hexpand=True,
        )
        self._search_entry.connect('search-changed', self._on_search_changed)
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        search_btn.bind_property(
            'active', self._search_bar, 'search-mode-enabled',
            GLib.BindingFlags.BIDIRECTIONAL,
        )
        main_box.append(self._search_bar)

        # Log text view
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hexpand=True,
        )
        main_box.append(scroll)

        self._text_view = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            monospace=True,
            left_margin=16,
            right_margin=16,
            top_margin=12,
            bottom_margin=12,
        )
        scroll.set_child(self._text_view)

        # Set up search tag for highlighting
        self._buffer = self._text_view.get_buffer()
        self._search_tag = self._buffer.create_tag(
            'search-highlight',
            background='rgba(255, 200, 50, 0.4)',
        )

        # Status bar
        self._status_label = Gtk.Label(
            xalign=0,
            css_classes=['dim-label'],
            margin_start=16,
            margin_end=16,
            margin_top=4,
            margin_bottom=8,
        )
        main_box.append(self._status_label)

        # Load content
        self._load_log()

        # Watch for changes
        self._start_monitor()

    def _load_log(self):
        """Load or reload the log file content."""
        if not self._log_path.is_file():
            self._buffer.set_text('No log file found.')
            self._status_label.set_label('')
            return

        try:
            content = self._log_path.read_text(errors='replace')
            # Show last 500KB max to keep UI responsive
            max_size = 500_000
            if len(content) > max_size:
                content = content[-max_size:]
                content = '... (truncated, showing last 500KB) ...\n\n' + content

            self._buffer.set_text(content)

            # Scroll to bottom
            end_iter = self._buffer.get_end_iter()
            self._text_view.scroll_to_iter(end_iter, 0, True, 0, 1.0)

            # Status
            size = self._log_path.stat().st_size
            size_str = self._format_size(size)
            lines = content.count('\n')
            self._status_label.set_label(f'{size_str}  ·  {lines:,} lines')

        except OSError as e:
            self._buffer.set_text(f'Error reading log: {e}')

    def _start_monitor(self):
        """Watch the log file for changes to auto-reload."""
        try:
            gfile = Gio.File.new_for_path(str(self._log_path))
            self._file_monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._file_monitor.connect('changed', self._on_file_changed)
        except GLib.Error:
            pass

    def _on_file_changed(self, monitor, file, other, event_type):
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CREATED):
            self._load_log()

    def _on_search_changed(self, entry):
        """Highlight matching text in the log."""
        query = entry.get_text().strip()
        start_iter = self._buffer.get_start_iter()
        end_iter = self._buffer.get_end_iter()
        self._buffer.remove_tag(self._search_tag, start_iter, end_iter)

        if not query:
            return

        count = 0
        search_iter = start_iter.copy()
        while True:
            result = search_iter.forward_search(
                query, Gtk.TextSearchFlags.CASE_INSENSITIVE, None,
            )
            if not result:
                break
            match_start, match_end = result
            self._buffer.apply_tag(self._search_tag, match_start, match_end)
            search_iter = match_end
            count += 1

            # Scroll to first match
            if count == 1:
                self._text_view.scroll_to_iter(match_start, 0.1, True, 0, 0.5)

    @staticmethod
    def _format_size(size: int) -> str:
        if size >= 1_048_576:
            return f'{size / 1_048_576:.1f} MB'
        if size >= 1024:
            return f'{size / 1024:.0f} KB'
        return f'{size} B'

    def do_unmap(self):
        if self._file_monitor:
            self._file_monitor.cancel()
            self._file_monitor = None
