"""Tests for adw_omarchy (the Omarchy look, shared with gdrive-for-linux)
and scripts/vendor-adw-omarchy.sh.

The GTK check parses the stylesheet for every theme Omarchy ships; it needs a
display and is skipped without one.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'src/backup_monitor/adw_omarchy.py'
VENDOR = ROOT / 'scripts/vendor-adw-omarchy.sh'
sys.path.insert(0, str(ROOT / 'src'))

from backup_monitor import adw_omarchy as ao  # noqa: E402

OMARCHY_THEMES = Path('/usr/share/omarchy/themes')


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def theme(self, colors: str, shell: str = '') -> Path:
        d = self.root / 'state/omarchy/current/theme'
        d.mkdir(parents=True)
        (d / 'colors.toml').write_text(colors)
        if shell:
            (d / 'shell.toml').write_text(shell)
        return d


class PaletteTest(Sandbox):
    def test_colors_and_shell_tokens(self):
        d = self.theme('mode = "light"\naccent = "#1e66f5"\nbackground = "#eff1f5"\n'
                       'foreground = "#4c4f69"\nred = "not-a-color"\n',
                       '[controls]\nnormal-border-alpha = 0.3\nhover-cursor-fill-alpha = 0.1\n'
                       'selected-fill-alpha = true\n')
        p = ao.load_palette(d)
        self.assertFalse(p.dark)
        self.assertEqual((p.accent, p.background, p.foreground), ('#1e66f5', '#eff1f5', '#4c4f69'))
        self.assertEqual(p.red, ao.Palette().red)                     # invalid → default
        self.assertEqual((p.alphas['normal-border'], p.alphas['hover-fill']), (0.3, 0.1))
        self.assertEqual(p.alphas['selected-fill'], ao.Palette().alphas['selected-fill'])  # bool ignored

    def test_accent_falls_back_to_blue(self):
        p = ao.load_palette(self.theme('blue = "#112233"\n'))
        self.assertEqual(p.accent, '#112233')

    def test_missing_or_broken_theme(self):
        self.assertIsNone(ao.load_palette(self.root / 'nothing'))
        self.assertIsNone(ao.load_palette(self.theme('mode = "dark\n')))

    def test_css_carries_the_palette(self):
        css = ao.build_css(ao.Palette(accent='#123456'))
        self.assertIn('@define-color accent_bg_color #123456;', css)
        self.assertIn('--accent-bg-color: #123456;', css)
        self.assertIn('border-radius: 0;', css)

    def test_detection_and_override(self):
        env = {'XDG_STATE_HOME': str(self.root / 'state'), 'OMARCHY_PATH': '/usr/share/omarchy'}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(ao.wanted_theme('X_THEME'), 'adwaita')    # no theme yet
            self.theme('accent = "#7aa2f7"\n')
            self.assertEqual(ao.wanted_theme('X_THEME'), 'omarchy')
            with mock.patch.dict(os.environ, {'X_THEME': 'adwaita'}):
                self.assertEqual(ao.wanted_theme('X_THEME'), 'adwaita')


class GtkParseTest(unittest.TestCase):
    """GTK accepts the stylesheet, for every theme Omarchy ships."""

    def test_every_shipped_theme_parses(self):
        if not (os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')):
            self.skipTest('no display')
        if not OMARCHY_THEMES.is_dir():
            self.skipTest('Omarchy themes not installed')
        import gi
        gi.require_version('Gtk', '4.0')
        from gi.repository import Gtk
        Gtk.init()
        for theme in sorted(OMARCHY_THEMES.iterdir()):
            palette = ao.load_palette(theme)
            if palette is None:
                continue
            with self.subTest(theme=theme.name):
                errors = []
                provider = Gtk.CssProvider()
                provider.connect('parsing-error',
                                 lambda _p, sec, err: errors.append(f'{sec.to_string()}: {err.message}'))
                provider.load_from_string(ao.build_css(palette))
                self.assertEqual(errors, [])


class VendorTest(Sandbox):
    def vendor(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(VENDOR), *args], capture_output=True, text=True)

    def test_write_check_edit(self):
        dest = self.root / 'adw_omarchy.py'
        self.assertEqual(self.vendor(str(dest)).returncode, 0)
        lines = dest.read_text().splitlines(keepends=True)
        self.assertEqual(''.join(lines[2:]), SOURCE.read_text())
        self.assertIn('current', self.vendor('--check', str(dest)).stdout)

        dest.write_text(dest.read_text() + '# local tweak\n')
        check = self.vendor('--check', str(dest))
        self.assertEqual((check.returncode, 'edited' in check.stdout), (1, True))
        self.assertEqual(self.vendor(str(dest)).returncode, 1)          # refuses to clobber

    def test_outdated_copy_is_updated(self):
        dest = self.root / 'adw_omarchy.py'
        old_body = '"""an older adw_omarchy"""\n'
        old_hash = hashlib.sha256(old_body.encode()).hexdigest()
        dest.write_text(f'# Vendored from mirror-backup (src/backup_monitor/adw_omarchy.py), sha256:{old_hash}.\n'
                        '# Do not edit here.\n' + old_body)
        check = self.vendor('--check', str(dest))
        self.assertEqual((check.returncode, 'outdated' in check.stdout), (1, True))
        self.assertEqual(self.vendor(str(dest)).returncode, 0)
        self.assertIn('current', self.vendor('--check', str(dest)).stdout)


if __name__ == '__main__':
    unittest.main()
