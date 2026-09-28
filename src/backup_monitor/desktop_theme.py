"""Mirror Backup's own widgets in the Omarchy look.

The look itself — palette, controls, surfaces, live theme switching — is
adw_omarchy, which is app-independent (gdrive-for-linux vendors it). This
module adds only what is Mirror Backup's: the dashboard title, the job cards
and their state badges, the schedule editor.
"""

from __future__ import annotations

from backup_monitor.adw_omarchy import OmarchyStyle, Palette

ENV_VAR = 'MIRROR_BACKUP_THEME'   # omarchy | adwaita — overrides detection


def extra_css(p: Palette) -> str:
    a, fg, acc = p.alphas, p.foreground, p.accent
    return f'''
.bm-title {{ font-size: 1.5em; font-weight: 800; }}
.bm-summary {{
  text-transform: uppercase; letter-spacing: 1.2px;
  font-size: 0.8em; font-weight: bold; color: alpha({fg}, 0.6);
}}
.bm-job-card:hover {{
  background: alpha({fg}, {a["hover-fill"]});
  border-color: alpha({fg}, {a["hover-border"]});
}}
.bm-job-name {{ font-size: 1em; }}
/* State badges read like the shell's detail pills. */
.bm-state-badge {{
  background: transparent;
  border: 1px solid alpha(currentColor, 0.5);
  padding: 1px 8px;
  font-size: 0.72em;
  letter-spacing: 1px;
  text-transform: uppercase;
}}
.bm-dot-idle {{ color: {p.dim_foreground}; }}
.bm-weekday-btn {{ border-radius: 0; }}
.bm-weekday-btn:checked {{ background: {acc}; color: {p.background}; border-color: {acc}; }}
.bm-time-entry {{ border-radius: 0; }}
'''


def omarchy_style() -> OmarchyStyle:
    return OmarchyStyle(env_var=ENV_VAR, extra_css=extra_css)
