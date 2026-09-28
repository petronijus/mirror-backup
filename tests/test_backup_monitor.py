"""Tests for the app's GTK-free core: paths, status rules, units, migration, CLI.

    python3 -m unittest discover -s tests

Every test works in its own temporary XDG tree; nothing touches the real
config, units, trash or systemd.
"""

from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from backup_monitor import cli, desktop_theme, paths, units  # noqa: E402
from backup_monitor.models.job import read_status  # noqa: E402
from backup_monitor.models.job_history import last_run, read_history  # noqa: E402
from backup_monitor.services import snapshot  # noqa: E402

BOOT = paths.boot_id()


class Sandbox(unittest.TestCase):
    """A private XDG tree plus a helper to make destinations."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = {
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'),
            'XDG_RUNTIME_DIR': str(self.root / 'run'),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def dest(self, name='dst') -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_status(self, dest: Path, **fields):
        state = dest / '.mirror-backup'
        state.mkdir(exist_ok=True)
        (state / 'status.json').write_text(json.dumps(fields))

    def job(self, job_id='backup-test', dest: Path | None = None, **extra) -> dict:
        job = {
            'id': job_id, 'name': 'Test', 'source': str(self.root / 'src') + '/',
            'destination': str(dest or self.dest()) + '/',
            'schedule': {'type': 'calendar', 'expression': 'Mon *-*-* 11:00:00'},
            'enabled': True,
        }
        job.update(extra)
        return job


class PathsTest(Sandbox):
    def test_xdg_locations(self):
        self.assertEqual(paths.jobs_file(), self.root / 'config/backup-sync/jobs.json')
        self.assertEqual(paths.systemd_user_dir(), self.root / 'config/systemd/user')
        self.assertEqual(paths.runtime_dir(), self.root / 'run/backup-sync')
        self.assertEqual(paths.legacy_data_dir(), self.root / 'data/backup-sync')

    def test_job_state_lives_in_destination(self):
        p = paths.job_paths('/mnt/x/mirror/')
        self.assertEqual(p.status, Path('/mnt/x/mirror/.mirror-backup/status.json'))
        self.assertEqual(p.history, Path('/mnt/x/mirror/.mirror-backup/history.jsonl'))

    def test_exclude_paths_relative_to_config(self):
        self.assertEqual(paths.resolve_config_path('a.exclude'),
                         str(self.root / 'config/backup-sync/a.exclude'))
        self.assertEqual(paths.resolve_config_path('/abs/a.exclude'), '/abs/a.exclude')
        self.assertEqual(paths.resolve_config_path(''), '')


class StatusRulesTest(Sandbox):
    def test_missing_destination_is_unavailable(self):
        st = read_status(str(self.root / 'nope'))
        self.assertEqual(st.state, 'unavailable')

    def test_never_ran_is_idle(self):
        self.assertEqual(read_status(str(self.dest())).state, 'idle')

    def test_active_from_another_boot_is_interrupted(self):
        d = self.dest()
        self.write_status(d, state='running', pid=os.getpid(), boot_id='other-boot', progress=40)
        st = read_status(str(d))
        self.assertEqual((st.state, st.deferred_reason, st.progress), ('deferred', 'interrupted', 0))

    def test_pre_06_active_status_without_boot_is_interrupted(self):
        d = self.dest()
        self.write_status(d, state='scanning', pid=1)
        self.assertEqual(read_status(str(d)).deferred_reason, 'interrupted')

    def test_active_this_boot_but_dead_is_error(self):
        d = self.dest()
        self.write_status(d, state='running', pid=2 ** 22 + 7, boot_id=BOOT)
        st = read_status(str(d))
        self.assertEqual(st.state, 'error')
        self.assertIn('ended unexpectedly', st.error)

    def test_active_this_boot_and_alive_stays_active(self):
        proc = subprocess.Popen(['bash', '-c', 'exec -a backup-sync sleep 30'])
        self.addCleanup(proc.wait)   # cleanups run last-in first-out: kill, then reap
        self.addCleanup(proc.kill)
        time.sleep(0.2)
        d = self.dest()
        self.write_status(d, state='running', pid=proc.pid, boot_id=BOOT, progress=40)
        st = read_status(str(d))
        self.assertEqual((st.state, st.progress), ('running', 40))

    def test_foreign_live_pid_is_not_believed(self):
        d = self.dest()
        self.write_status(d, state='running', pid=os.getpid(), boot_id=BOOT)
        self.assertEqual(read_status(str(d)).state, 'error')   # not a backup-sync process

    def test_unreadable_status_is_an_error(self):
        d = self.dest()
        (d / '.mirror-backup').mkdir()
        (d / '.mirror-backup/status.json').write_text('{half')
        self.assertEqual(read_status(str(d)).state, 'error')


class HistoryTest(Sandbox):
    def test_last_run_and_host(self):
        d = self.dest()
        (d / '.mirror-backup').mkdir()
        (d / '.mirror-backup/history.jsonl').write_text(
            '{"started":"2026-09-27T22:00:00+02:00","finished":"x","duration_sec":1,"exit_code":0,'
            '"files_transferred":1,"files_total":2,"error":""}\n'
            '{"started":"2026-09-28T11:00:00+02:00","finished":"y","duration_sec":1,"exit_code":0,'
            '"files_transferred":1,"files_total":2,"error":"","host":"omarchy"}\n'
            'not json\n')
        entry = last_run(str(d))
        self.assertEqual((entry.started[:10], entry.host), ('2026-09-28', 'omarchy'))
        self.assertEqual(len(read_history(str(d))), 2)


class TimeFormatTest(unittest.TestCase):
    """History stores times with an offset (date -Iseconds); formatting
    compares them with a naive now()."""

    def test_offset_timestamps_format(self):
        from datetime import datetime, timedelta
        from backup_monitor.models.job_history import HistoryEntry
        from backup_monitor.services.systemd_service import format_countdown, format_relative_past
        aware = (datetime.now().astimezone() - timedelta(hours=2)).isoformat(timespec='seconds')
        self.assertEqual(format_relative_past(aware), '2h ago')
        soon = (datetime.now().astimezone() + timedelta(hours=3, minutes=1)).isoformat(timespec='seconds')
        self.assertEqual(format_countdown(soon), 'in 3h 0m')
        entry = HistoryEntry(aware, aware, 1, 0, 0, 0, '')
        self.assertIn(entry.started_relative, ('Today ' + entry.started_datetime.strftime('%H:%M'),
                                               'Yesterday ' + entry.started_datetime.strftime('%H:%M')))


class OmarchyLookTest(Sandbox):
    """The look is adw_omarchy (tests/test_adw_omarchy.py); the app adds its own rules."""

    def test_app_rules_use_the_palette(self):
        from backup_monitor.adw_omarchy import Palette
        css = desktop_theme.extra_css(Palette(accent='#123456', background='#000001'))
        self.assertIn('.bm-weekday-btn:checked { background: #123456; color: #000001;', css)

    def test_env_var_overrides_detection(self):
        from backup_monitor.adw_omarchy import wanted_theme
        with mock.patch.dict(os.environ, {desktop_theme.ENV_VAR: 'adwaita'}):
            self.assertEqual(wanted_theme(desktop_theme.ENV_VAR), 'adwaita')


class UnitsTest(Sandbox):
    def fake_systemctl(self):
        calls = []

        def run(args):
            calls.append(args)
            out = 'disabled' if args[0] == 'is-enabled' else 'inactive' if args[0] == 'is-active' else ''
            return subprocess.CompletedProcess(args, 0, out, '')
        return calls, run

    def test_exec_args_survive_awkward_paths(self):
        job = self.job(source='/mnt/My Disk/50% "odd" $HOME/', destination='/mnt/b/')
        service = units.render_service(job)
        self.assertIn('ExecStart=%h/.local/bin/backup-sync "backup-test" '
                      '"/mnt/My Disk/50%% \\"odd\\" $$HOME/" "/mnt/b/" "" "0"', service)
        self.assertTrue(service.startswith(units.MARKER + '\n'))

    def test_relative_exclude_resolved_into_unit(self):
        service = units.render_service(self.job(exclude_file='backup-test.exclude'))
        self.assertIn(f'"{self.root}/config/backup-sync/backup-test.exclude"', service)

    def test_manual_job_has_no_timer(self):
        job = self.job(schedule={'type': 'manual'})
        self.assertEqual(set(units.desired_units([job])), {'backup-test.service'})

    def test_sync_writes_enables_and_is_idempotent(self):
        calls, run = self.fake_systemctl()
        discarded = []
        report = units.sync_units([self.job()], run=run, discard=discarded.append)
        self.assertEqual(sorted(report.written), ['backup-test.service', 'backup-test.timer'])
        self.assertIn(['enable', '--now', 'backup-test.timer'], calls)
        self.assertIn(['daemon-reload'], calls)
        self.assertEqual(discarded, [])

        calls.clear()
        report = units.sync_units([self.job()], run=lambda a: subprocess.CompletedProcess(
            a, 0, 'enabled' if a[0] == 'is-enabled' else 'active', ''), discard=discarded.append)
        self.assertFalse(report.written or report.removed)

    def test_changed_unit_goes_to_discard_first(self):
        _, run = self.fake_systemctl()
        unit_dir = paths.systemd_user_dir()
        unit_dir.mkdir(parents=True)
        (unit_dir / 'backup-test.service').write_text('[Service]\nExecStart=/hand/edited\n')
        discarded = []
        units.sync_units([self.job()], run=run, discard=discarded.append)
        self.assertEqual([p.name for p in discarded], ['backup-test.service'])

    def test_orphans_removed_only_when_generated(self):
        _, run = self.fake_systemctl()
        units.sync_units([self.job('backup-old'), self.job('backup-keep')], run=run, discard=lambda p: '')
        unit_dir = paths.systemd_user_dir()
        (unit_dir / 'backup-handmade.service').write_text('[Service]\n')
        discarded = []
        report = units.sync_units([self.job('backup-keep')], run=run,
                                  discard=lambda p: discarded.append(p.name))
        self.assertEqual(sorted(discarded), ['backup-old.service', 'backup-old.timer'])
        self.assertEqual(report.unmanaged, ['backup-handmade.service'])

        report = units.sync_units([self.job('backup-keep')], run=run, remove_ids=('backup-handmade',),
                                  discard=lambda p: discarded.append(p.name))
        self.assertIn('backup-handmade.service', discarded)

    def test_invalid_job_id_gets_no_units(self):
        _, run = self.fake_systemctl()
        report = units.sync_units([self.job('../evil')], run=run, discard=lambda p: '')
        self.assertTrue(report.errors)
        self.assertFalse(report.written)


class MigrationTest(Sandbox):
    def legacy(self) -> Path:
        legacy = self.root / 'data/backup-sync'
        for sub in ('status', 'history', 'logs'):
            (legacy / sub).mkdir(parents=True, exist_ok=True)
        (legacy / 'status/backup-test.json').write_text('{"state": "idle", "progress": 100}')
        (legacy / 'history/backup-test.jsonl').write_text(
            '{"started":"2026-09-01T00:00:00+02:00","finished":"a","exit_code":0}\n'
            '{"started":"2026-09-02T00:00:00+02:00","finished":"b","exit_code":0}\n')
        (legacy / 'logs/backup-test.log').write_text('old log\n')
        return legacy

    def test_merges_into_destination_idempotently(self):
        d = self.dest()
        (d / '.mirror-backup').mkdir()
        (d / '.mirror-backup/history.jsonl').write_text(
            '{"started":"2026-09-02T00:00:00+02:00","finished":"b","exit_code":0}\n'
            '{"started":"2026-09-03T00:00:00+02:00","finished":"c","exit_code":0,"host":"omarchy"}\n')
        legacy = self.legacy()
        result = cli.migrate_jobs([self.job(dest=d)], legacy)
        self.assertTrue(result.complete)
        lines = (d / '.mirror-backup/history.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(x)['finished'] for x in lines], ['a', 'b', 'c'])
        self.assertEqual((d / '.mirror-backup/backup.log').read_text(), 'old log\n')
        self.assertTrue((d / '.mirror-backup/status.json').is_file())

        again = cli.migrate_jobs([self.job(dest=d)], legacy)
        self.assertFalse(any('history' in a for a in again.actions))

    def test_log_migrated_before_is_not_copied_again(self):
        d = self.dest()
        legacy = self.legacy()
        cli.migrate_jobs([self.job(dest=d)], legacy)
        with open(d / '.mirror-backup/backup.log', 'a') as f:
            f.write('a later run\n')
        again = cli.migrate_jobs([self.job(dest=d)], legacy)
        self.assertFalse(any('log' in a for a in again.actions))
        self.assertFalse((d / '.mirror-backup/backup.log.pre-0.6').exists())

    def test_unavailable_destination_blocks_retire(self):
        legacy = self.legacy()
        result = cli.migrate_jobs([self.job(dest=self.root / 'gone')], legacy)
        self.assertEqual(result.skipped, ['backup-test'])
        self.assertFalse(result.complete)

    def test_existing_log_kept_and_legacy_log_set_aside(self):
        d = self.dest()
        (d / '.mirror-backup').mkdir()
        (d / '.mirror-backup/backup.log').write_text('new log\n')
        cli.migrate_jobs([self.job(dest=d)], self.legacy())
        self.assertEqual((d / '.mirror-backup/backup.log').read_text(), 'new log\n')
        self.assertEqual((d / '.mirror-backup/backup.log.pre-0.6').read_text(), 'old log\n')


class ResumeTest(Sandbox):
    def test_restarts_postponed_and_interrupted_runs_only(self):
        a, b, c = self.dest('a'), self.dest('b'), self.dest('c')
        (a / '.mirror-backup').mkdir()
        (a / '.mirror-backup/deferred').write_text('{"reason": "shutdown"}\n')
        self.write_status(b, state='running', pid=1, boot_id='another-os')
        self.write_status(c, state='idle')
        jobs = [self.job('backup-a', a), self.job('backup-b', b), self.job('backup-c', c),
                self.job('backup-gone', self.root / 'gone')]
        started = []

        def fake(*args):
            if args[0] == 'start':
                started.append(args[-1])
            return subprocess.CompletedProcess(args, 0, 'loaded' if args[0] == 'show' else '', '')
        with mock.patch.object(cli, 'load_jobs', return_value=jobs), \
                mock.patch.object(cli, '_systemctl', side_effect=fake):
            cli.resume()
        self.assertEqual(started, ['backup-a.service', 'backup-b.service'])


class SnapshotTest(Sandbox):
    def test_snapshot_shape(self):
        d = self.dest()
        self.write_status(d, state='idle', progress=100, host='ubuntu')
        timers = {'backup-test': {'next_run': '2026-09-30T11:00:00+02:00', 'timer_enabled': True}}
        snap = snapshot.snapshot([self.job(dest=d)], timers=timers)
        job = snap['jobs'][0]
        self.assertEqual(job['status']['state'], 'idle')
        self.assertEqual(job['next_run'], '2026-09-30T11:00:00+02:00')
        self.assertIsNone(job['last_run'])
        self.assertEqual(snapshot.summary_state(snap), 'idle')

    def test_summary_prefers_error_then_activity(self):
        snap = {'jobs': [{'status': {'state': s}} for s in ('idle', 'running', 'unavailable')]}
        self.assertEqual(snapshot.summary_state(snap), 'running')
        snap['jobs'].append({'status': {'state': 'error'}})
        self.assertEqual(snapshot.summary_state(snap), 'error')

    def test_unix_timestamps(self):
        self.assertTrue(snapshot._unix_to_iso('@1790758800').startswith('2026-09-30T'))
        self.assertEqual(snapshot._unix_to_iso(''), '')


if __name__ == '__main__':
    unittest.main()
