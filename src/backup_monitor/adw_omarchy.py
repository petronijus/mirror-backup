"""Omarchy's look for libadwaita apps.

Omarchy leaves GTK apps on stock Adwaita. An app that starts `OmarchyStyle`
instead takes the current Omarchy theme: its palette (``colors.toml``), the
control tokens the Omarchy shell draws with (``shell.toml``: fill, border and
hover alphas), the monospace UI font, square corners, flat hairline borders
instead of shadows, and the shell's small-caps section headers — dark or light
as the theme says. A theme switch (``omarchy theme set``) restyles open windows.
Off Omarchy nothing changes.

    style = OmarchyStyle(env_var='MYAPP_THEME', extra_css=my_rules)
    style.start()          # in Adw.Application.do_startup, after the app's own CSS

``extra_css(palette) -> str`` adds rules for the app's own widgets. The
environment variable, set to ``omarchy`` or ``adwaita``, overrides detection.

One file, the standard library plus PyGObject (imported only by OmarchyStyle).
This is the canonical copy, in Mirror Backup; other apps (gdrive-for-linux)
carry a copy made by Mirror Backup's scripts/vendor-adw-omarchy.sh, which
refuses to overwrite a copy that was edited in place.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_HEX = re.compile(r'^#[0-9a-fA-F]{6}$')


def omarchy_theme_dir() -> Path:
    state = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local' / 'state')
    return Path(state) / 'omarchy' / 'current' / 'theme'


def wanted_theme(env_var: str = 'ADW_OMARCHY_THEME') -> str:
    """'omarchy' or 'adwaita'."""
    forced = os.environ.get(env_var, '').strip().lower()
    if forced in ('omarchy', 'adwaita'):
        return forced
    on_omarchy = bool(os.environ.get('OMARCHY_PATH')) or Path('/usr/share/omarchy').is_dir()
    if on_omarchy and (omarchy_theme_dir() / 'colors.toml').is_file():
        return 'omarchy'
    return 'adwaita'


@dataclass
class Palette:
    """The parts of an Omarchy theme a libadwaita app needs. Defaults: Tokyo Night."""
    dark: bool = True
    background: str = '#1a1b26'
    lighter_background: str = '#24283b'
    foreground: str = '#a9b1d6'
    dim_foreground: str = '#565f89'
    accent: str = '#7aa2f7'
    red: str = '#f7768e'
    yellow: str = '#e0af68'
    green: str = '#9ece6a'
    # The shell's control tokens ([controls] in shell.toml)
    alphas: dict[str, float] = field(default_factory=lambda: {
        'normal-fill': 0.04, 'normal-border': 0.4,
        'hover-fill': 0.08, 'hover-border': 0.25,
        'selected-fill': 0.18, 'pressed-fill': 0.22, 'selection-fill': 0.35,
    })


def load_palette(theme_dir: Path | None = None) -> Palette | None:
    """The theme's palette; None when there is no readable colors.toml."""
    theme_dir = theme_dir or omarchy_theme_dir()
    try:
        colors = tomllib.loads((theme_dir / 'colors.toml').read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    p = Palette()

    def color(*keys: str, default: str) -> str:
        for key in keys:
            value = str(colors.get(key, '')).strip()
            if _HEX.match(value):
                return value
        return default

    p.dark = str(colors.get('mode', 'dark')).strip().lower() != 'light'
    p.background = color('background', default=p.background)
    p.lighter_background = color('lighter_background', default=p.lighter_background)
    p.foreground = color('foreground', default=p.foreground)
    p.dim_foreground = color('dark_foreground', 'muted', default=p.dim_foreground)
    p.accent = color('accent', 'blue', default=p.accent)
    p.red = color('red', default=p.red)
    p.yellow = color('yellow', default=p.yellow)
    p.green = color('green', default=p.green)

    try:
        controls = tomllib.loads((theme_dir / 'shell.toml').read_text(encoding='utf-8')).get('controls', {})
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        controls = {}
    keys = {
        'normal-fill': 'normal-fill-alpha', 'normal-border': 'normal-border-alpha',
        'hover-fill': 'hover-cursor-fill-alpha', 'hover-border': 'hover-cursor-border-alpha',
        'selected-fill': 'selected-fill-alpha', 'pressed-fill': 'pressed-fill-alpha',
        'selection-fill': 'selection-fill-alpha',
    }
    for token, key in keys.items():
        value = controls.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            p.alphas[token] = float(value)
    return p


def build_css(p: Palette) -> str:
    """The stylesheet that restyles libadwaita to the palette."""
    a = p.alphas
    fg, bg, acc = p.foreground, p.background, p.accent
    on_fill = bg   # text on a filled accent/state background

    named = {
        'window_bg_color': bg, 'window_fg_color': fg,
        'view_bg_color': bg, 'view_fg_color': fg,
        'headerbar_bg_color': bg, 'headerbar_fg_color': fg,
        'headerbar_backdrop_color': bg,
        'headerbar_border_color': f'alpha({fg}, 0.12)',
        'headerbar_shade_color': 'transparent',
        'sidebar_bg_color': bg, 'sidebar_fg_color': fg,
        'card_bg_color': f'alpha({fg}, {a["normal-fill"]})', 'card_fg_color': fg,
        'card_shade_color': 'transparent',
        'dialog_bg_color': bg, 'dialog_fg_color': fg,
        'popover_bg_color': bg, 'popover_fg_color': fg,
        'popover_shade_color': 'transparent',
        'thumbnail_bg_color': p.lighter_background, 'thumbnail_fg_color': fg,
        'accent_color': acc, 'accent_bg_color': acc, 'accent_fg_color': on_fill,
        'destructive_color': p.red, 'destructive_bg_color': p.red, 'destructive_fg_color': on_fill,
        'success_color': p.green, 'success_bg_color': p.green, 'success_fg_color': on_fill,
        'warning_color': p.yellow, 'warning_bg_color': p.yellow, 'warning_fg_color': on_fill,
        'error_color': p.red, 'error_bg_color': p.red, 'error_fg_color': on_fill,
        'shade_color': 'transparent',
        'scrollbar_outline_color': 'transparent',
        'borders': f'alpha({fg}, 0.12)',
    }
    defines = '\n'.join(f'@define-color {k} {v};' for k, v in named.items())
    # libadwaita 1.6+ reads CSS variables; the named colors cover older versions.
    variables = '\n'.join(f'  --{k.replace("_", "-")}: {v};'
                          for k, v in named.items() if k.endswith('_color'))
    variables += f'\n  --border-color: {named["borders"]};'

    return f'''/* Omarchy look — generated by adw_omarchy from the current theme */
{defines}

:root {{
{variables}
  --window-radius: 0;
}}

/* Omarchy draws square, flat and in one monospace face. */
* {{
  font-family: monospace;
  border-radius: 0;
  box-shadow: none;
  -gtk-icon-shadow: none;
}}
radio {{ border-radius: 9999px; }}

window, window.csd, window > .titlebar {{ border-radius: 0; }}
selection, text selection {{ background-color: alpha({acc}, {a["selection-fill"]}); color: {fg}; }}
*:focus-visible {{ outline: 1px solid {acc}; outline-offset: -1px; }}

headerbar {{
  background: {bg};
  border-bottom: 1px solid alpha({fg}, 0.12);
  min-height: 40px;
}}
headerbar .title {{ font-weight: bold; }}

/* ── controls: the shell's normal / hover / pressed tokens ── */
button:not(.flat):not(.link):not(.suggested-action):not(.destructive-action),
menubutton > button:not(.flat),
dropdown > button,
spinbutton,
entry {{
  background: alpha({fg}, {a["normal-fill"]});
  border: 1px solid alpha({fg}, {a["normal-border"]});
}}
button:not(.flat):not(.suggested-action):not(.destructive-action):hover,
dropdown > button:hover {{
  background: alpha({fg}, {a["hover-fill"]});
  border-color: alpha({fg}, {a["hover-border"]});
}}
button:active {{ background: alpha({fg}, {a["pressed-fill"]}); }}
button.flat:hover, menubutton > button.flat:hover {{ background: alpha({fg}, {a["hover-fill"]}); }}
button.suggested-action {{ background: {acc}; color: {on_fill}; border: 1px solid {acc}; }}
button.suggested-action:hover {{ background: alpha({acc}, 0.85); }}
button.destructive-action {{
  background: transparent; color: {p.red}; border: 1px solid alpha({p.red}, 0.6);
}}
button.destructive-action:hover {{ background: alpha({p.red}, 0.12); }}
entry:focus-within {{ border-color: {acc}; }}

/* Header actions are the shell's panel actions: no chrome until hovered.
   (As specific as the control rule above, so they win.) */
headerbar button:not(.flat):not(.link):not(.suggested-action):not(.destructive-action),
headerbar menubutton > button:not(.flat) {{
  background: transparent;
  border: 1px solid transparent;
}}
headerbar button:not(.flat):not(.link):not(.suggested-action):not(.destructive-action):hover,
headerbar menubutton > button:not(.flat):hover {{
  background: alpha({fg}, {a["hover-fill"]});
  border-color: alpha({fg}, {a["hover-border"]});
}}
windowcontrols > button > image {{ background: transparent; }}

/* View switchers read like the shell's tabs: selected = filled, no pill. */
viewswitcher button {{ background: transparent; border: 1px solid transparent; }}
viewswitcher button:hover {{ background: alpha({fg}, {a["hover-fill"]}); }}
viewswitcher button:checked {{
  background: alpha({fg}, {a["selected-fill"]});
  border-bottom: 1px solid {acc};
}}

switch {{ background: alpha({fg}, 0.14); border: 1px solid alpha({fg}, {a["normal-border"]}); }}
switch:checked {{ background: {acc}; border-color: {acc}; }}
switch > slider {{ background: {fg}; }}
switch:checked > slider {{ background: {on_fill}; }}
check {{ border: 1px solid alpha({fg}, {a["normal-border"]}); background: transparent; }}
check:checked {{ background: {acc}; color: {on_fill}; border-color: {acc}; }}

progressbar > trough {{ background: alpha({fg}, 0.14); min-height: 4px; }}
progressbar > trough > progress {{ background: {acc}; min-height: 4px; }}
scrollbar slider {{ background: alpha({fg}, 0.3); }}

/* ── surfaces: flat fill, hairline border — the shell's cards and popups ── */
.card, list.boxed-list, .boxed-list {{
  background: alpha({fg}, {a["normal-fill"]});
  border: 1px solid alpha({fg}, 0.14);
}}
list.boxed-list > row, .boxed-list > row {{ border-color: alpha({fg}, 0.10); }}
row.activatable:hover {{ background: alpha({fg}, {a["hover-fill"]}); }}
popover > contents, popover.menu > contents {{
  background: {bg}; border: 1px solid {acc}; padding: 6px;
}}
popover > arrow {{ background: {acc}; }}
tooltip {{ background: {bg}; color: {fg}; border: 1px solid alpha({fg}, 0.6); }}
toast {{ background: {bg}; color: {fg}; border: 1px solid {acc}; }}
dialog.alert .dialog-contents, window.dialog {{ background: {bg}; border: 1px solid {acc}; }}
banner > revealer > widget {{ background: alpha({acc}, 0.14); border-bottom: 1px solid alpha({acc}, 0.4); }}
statuspage .title {{ font-weight: 800; }}

/* Section headers read like the shell's panel headers. */
preferencesgroup .header .heading, preferencesgroup > box > box > label.heading {{
  text-transform: uppercase;
  letter-spacing: 1px;
  font-size: 0.83em;
  color: alpha({fg}, 0.6);
}}
'''


# ── applying it (PyGObject) ──

class OmarchyStyle:
    """Applies build_css to the running app and follows theme switches.

    Sits one step above the app's own stylesheet (APPLICATION + 1), so the
    theme wins over it; the app's ``extra_css`` rides along in the same
    provider.
    """

    def __init__(self, env_var: str = 'ADW_OMARCHY_THEME',
                 extra_css: Callable[[Palette], str] | None = None):
        self._env_var = env_var
        self._extra_css = extra_css
        self._provider = None
        self._monitors = []
        self._reload_id = 0

    @property
    def active(self) -> bool:
        return self._provider is not None

    def start(self) -> bool:
        """Apply the theme if the app runs on Omarchy. Returns whether it does."""
        if wanted_theme(self._env_var) != 'omarchy':
            return False
        from gi.repository import Gio, GLib
        self._apply()
        # A switch rewrites the theme's files; theme.name names the new one.
        theme_dir = omarchy_theme_dir()
        for path in (theme_dir.parent / 'theme.name', theme_dir / 'colors.toml',
                     theme_dir / 'shell.toml'):
            try:
                monitor = Gio.File.new_for_path(str(path)).monitor_file(Gio.FileMonitorFlags.NONE, None)
            except GLib.Error:
                continue
            monitor.connect('changed', self._on_changed)
            self._monitors.append(monitor)
        return self.active

    def _on_changed(self, *_args):
        from gi.repository import GLib
        # A switch touches several files in a burst: apply once it settles.
        if self._reload_id:
            GLib.source_remove(self._reload_id)
        self._reload_id = GLib.timeout_add(400, self._reload)

    def _reload(self) -> bool:
        self._reload_id = 0
        self._apply()
        return False

    def _apply(self):
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        from gi.repository import Adw, Gdk, Gtk

        palette = load_palette()
        display = Gdk.Display.get_default()
        if palette is None or display is None:
            return
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if palette.dark else Adw.ColorScheme.FORCE_LIGHT)
        css = build_css(palette)
        if self._extra_css is not None:
            css += '\n/* ── the app\'s own widgets ── */\n' + self._extra_css(palette)
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        if self._provider is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self._provider)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._provider = provider
