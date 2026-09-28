"""Async systemctl --user wrapper using Gio.Subprocess."""

from __future__ import annotations

from datetime import datetime

import gi
gi.require_version('Gio', '2.0')
from gi.repository import Gio, GLib


def _run_systemctl(args: list[str], callback=None):
    """Run systemctl --user with given args asynchronously."""
    try:
        proc = Gio.Subprocess.new(
            ['systemctl', '--user', *args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
        proc.communicate_utf8_async(None, None, _on_done, callback)
    except GLib.Error as e:
        print(f'[BackupMonitor] systemctl error: {e.message}')
        if callback:
            callback('', str(e))


def _on_done(proc, result, callback):
    try:
        _, stdout, stderr = proc.communicate_utf8_finish(result)
        if callback:
            callback(stdout.strip() if stdout else '', None)
    except GLib.Error as e:
        if callback:
            callback('', str(e))


def start_job(service_name: str):
    _run_systemctl(['start', service_name])


def stop_job(service_name: str, is_paused: bool = False):
    if is_paused:
        _run_systemctl(['kill', '--signal=USR2', service_name])
    _run_systemctl(['stop', service_name])


def pause_job(service_name: str):
    _run_systemctl(['kill', '--signal=USR1', service_name])


def resume_job(service_name: str):
    _run_systemctl(['kill', '--signal=USR2', service_name])


def get_timer_info(timer_name: str, callback):
    """Get next/last trigger times for a timer.

    callback(next_run_iso: str, last_run_iso: str, error: str | None)
    Returns ISO timestamps so the UI can compute live countdowns.
    """
    _run_systemctl(
        ['show', timer_name,
         '--property=NextElapseUSecRealtime',
         '--property=LastTriggerUSec'],
        lambda stdout, err: _parse_timer_info(stdout, err, callback),
    )


def _parse_timer_info(stdout: str, error, callback):
    if error:
        callback('', '', error)
        return

    next_run = ''
    last_run = ''
    for line in stdout.splitlines():
        if line.startswith('NextElapseUSecRealtime='):
            val = line.split('=', 1)[1].strip()
            if val and val != 'n/a':
                next_run = _parse_systemd_timestamp(val)
        elif line.startswith('LastTriggerUSec='):
            val = line.split('=', 1)[1].strip()
            if val and val != 'n/a':
                last_run = _parse_systemd_timestamp(val)
    callback(next_run, last_run, None)


def get_all_timer_info(timer_names: list[str], callback):
    """Get timer info for multiple timers. callback(dict[timer_name, (next_iso, last_iso)])"""
    results = {}
    remaining = [len(timer_names)]

    if not timer_names:
        callback({})
        return

    def on_one(timer_name, next_run, last_run, err):
        results[timer_name] = (next_run, last_run)
        remaining[0] -= 1
        if remaining[0] <= 0:
            callback(results)

    for name in timer_names:
        get_timer_info(
            name,
            lambda nr, lr, e, n=name: on_one(n, nr, lr, e),
        )


def _parse_systemd_timestamp(systemd_ts: str) -> str:
    """Parse systemd timestamp to ISO format.

    Input:  'Thu 2026-03-27 18:00:00 CET'
    Output: '2026-03-27T18:00:00'
    """
    parts = systemd_ts.split()
    if len(parts) >= 3:
        date_part = parts[1]  # 2026-03-27
        time_part = parts[2]  # 18:00:00
        return f'{date_part}T{time_part}'
    return ''


def local_datetime(iso_ts: str) -> datetime:
    """Parse an ISO 8601 timestamp into naive local time, whether or not it
    carries an offset — backup-sync writes one (date -Iseconds), systemd
    timestamps parsed above do not, and the two must compare with now()."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def format_countdown(iso_ts: str) -> str:
    """Format an ISO timestamp as a countdown string like '2h 15m' or 'in 3d 5h'.

    Returns '' if timestamp is empty or in the past.
    """
    if not iso_ts:
        return ''
    try:
        dt = local_datetime(iso_ts)
        now = datetime.now()
        diff = dt - now
        total_sec = int(diff.total_seconds())

        if total_sec <= 0:
            return 'now'

        days = total_sec // 86400
        hours = (total_sec % 86400) // 3600
        minutes = (total_sec % 3600) // 60

        if days > 0:
            return f'in {days}d {hours}h'
        if hours > 0:
            return f'in {hours}h {minutes}m'
        if minutes > 0:
            return f'in {minutes}m'
        return f'in {total_sec}s'
    except (ValueError, TypeError):
        return ''


def format_relative_past(iso_ts: str) -> str:
    """Format an ISO timestamp as a relative past string like '2h ago' or 'Yesterday 14:30'."""
    if not iso_ts:
        return ''
    try:
        dt = local_datetime(iso_ts)
        now = datetime.now()
        diff = now - dt
        total_sec = int(diff.total_seconds())

        if total_sec < 0:
            return dt.strftime('%H:%M')
        if total_sec < 60:
            return 'just now'

        minutes = total_sec // 60
        hours = total_sec // 3600
        days = (now.date() - dt.date()).days

        if days == 0:
            if hours > 0:
                return f'{hours}h ago'
            return f'{minutes}m ago'
        if days == 1:
            return f'yesterday {dt.strftime("%H:%M")}'
        if days < 7:
            return f'{dt.strftime("%a")} {dt.strftime("%H:%M")}'
        return dt.strftime('%b %d %H:%M')
    except (ValueError, TypeError):
        return ''
