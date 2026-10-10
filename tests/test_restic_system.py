"""Tests for restic jobs and system jobs: units, status wording, job loading,
control routing, and `mirror-backup system` (validation, plan, keys).

    python3 -m unittest discover -s tests

Like test_backup_monitor: a private XDG tree, and a private stand-in for
/etc/mirror-backup, /etc/systemd/system and the polkit rule. Nothing runs as
root and nothing reaches systemd.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_monitor import cli, paths, system, units  # noqa: E402
from backup_monitor.models.job import BackupStatus, read_status  # noqa: E402
from backup_monitor.services import job_manager, snapshot  # noqa: E402
from test_backup_monitor import Sandbox  # noqa: E402

KEY = '/etc/mirror-backup/keys/test.key'


class SystemSandbox(Sandbox):
    """Sandbox plus a fake /etc/mirror-backup, unit dir and polkit rule."""

    def setUp(self):
        super().setUp()
        self.etc = self.root / 'etc-mirror-backup'
        self.unit_dir = self.root / 'etc-systemd-system'
        self.unit_dir.mkdir()
        for patcher in (
            mock.patch.dict(os.environ, {'MIRROR_BACKUP_SYSTEM_CONFIG': str(self.etc)}),
            mock.patch.object(paths, 'SYSTEM_UNIT_DIR', self.unit_dir),
            mock.patch.object(system, 'POLKIT_RULE', self.root / 'polkit' / '50-mirror-backup.rules'),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key = str(self.etc / 'keys' / 'test.key')

    def restic_job(self, job_id='backup-system', **extra) -> dict:
        job = {
            'id': job_id, 'name': 'System', 'engine': 'restic', 'scope': 'system',
            'destination': str(self.dest('repo-dst')),
            'restic': {'mode': 'backup', 'paths': ['/', '/home'], 'password_file': self.key,
                       'keep': {'daily': 7, 'weekly': 4}},
            'schedule': {'type': 'calendar', 'expression': '*-*-* 12:00:00'},
        }
        job.update(extra)
        return job

    def copy_job(self, job_id='backup-system-nas', **extra) -> dict:
        job = {
            'id': job_id, 'name': 'System → NAS', 'engine': 'restic', 'scope': 'system',
            'destination': str(self.dest('nas-dst')), 'run_after': 'backup-system',
            'restic': {'mode': 'copy', 'from_job': 'backup-system', 'password_file': self.key,
                       'keep': {'daily': 7, 'monthly': 12}},
            'schedule': {'type': 'calendar', 'expression': 'daily'},
        }
        job.update(extra)
        return job

    def write_source(self, jobs: list[dict], **files) -> Path:
        src = paths.system_source_dir()
        src.mkdir(parents=True, exist_ok=True)
        (src / 'jobs.json').write_text(json.dumps({'version': 1, 'jobs': jobs}))
        for name, content in files.items():
            path = src / name.replace('__', '/')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return src


class ResticUnitsTest(SystemSandbox):
    def test_backup_job_unit(self):
        jobs = [self.restic_job(pre_command='mirror-backup-system-meta', exclude_file='system.exclude'),
                self.copy_job()]
        service = units.render_service(jobs[0], jobs, owner='petr')
        generated = self.etc / 'generated' / 'backup-system.paths'
        self.assertIn(f'ExecStart=/usr/local/lib/mirror-backup/backup-sync "backup-system" '
                      f'"{generated}" "{jobs[0]["destination"]}" "{self.etc}/system.exclude" "0"', service)
        for line in ('Environment="BACKUP_SYNC_SCOPE=system"',
                     'Environment="BACKUP_SYNC_NOTIFY_USER=petr"',
                     'Environment="BACKUP_SYNC_ENGINE=restic"',
                     'Environment="BACKUP_SYNC_RESTIC_MODE=backup"',
                     'Environment="BACKUP_SYNC_RESTIC_KEEP=--keep-daily 7 --keep-weekly 4"',
                     f'Environment="RESTIC_PASSWORD_FILE={self.key}"',
                     'Environment="BACKUP_SYNC_PRE_COMMAND=/usr/local/lib/mirror-backup/mirror-backup-system-meta"',
                     'OnSuccess=backup-system-nas.service',
                     'CacheDirectory=mirror-backup',
                     'ProtectSystem=full'):
            self.assertIn(line, service)

    def test_sandbox_never_hides_what_a_backup_reads(self):
        service = units.render_service(self.restic_job(), [self.restic_job()])
        for setting in units.HIDING_SETTINGS:
            self.assertNotIn(f'{setting}=', service)

    def test_copy_job_reads_from_the_source_jobs_destination(self):
        jobs = [self.restic_job(), self.copy_job()]
        service = units.render_service(jobs[1], jobs)
        self.assertIn(f'"backup-system-nas" "{jobs[0]["destination"]}" "{jobs[1]["destination"]}"', service)
        self.assertIn('Environment="BACKUP_SYNC_RESTIC_MODE=copy"', service)
        self.assertIn(f'Environment="RESTIC_FROM_PASSWORD_FILE={self.key}"', service)
        self.assertNotIn('OnSuccess', service)

    def test_user_restic_job_has_no_sandbox(self):
        job = self.restic_job(scope='user', job_id='backup-home')
        service = units.render_service(job)
        self.assertIn('ExecStart=%h/.local/bin/backup-sync', service)
        self.assertNotIn('ProtectSystem', service)
        self.assertNotIn('BACKUP_SYNC_SCOPE', service)

    def test_rsync_units_unchanged_by_the_new_keys(self):
        service = units.render_service(self.job())
        self.assertNotIn('BACKUP_SYNC_ENGINE', service)
        self.assertNotIn('OnSuccess', service)

    def test_sync_writes_path_list_and_only_this_scope_and_host(self):
        calls = []

        def run(args):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        jobs = [self.restic_job(), self.copy_job(hosts=['elsewhere']), self.job('backup-user')]
        report = units.sync_units(jobs, scope='system', run=run, host='here',
                                  discard=lambda p: '')
        self.assertEqual(sorted(report.written), ['backup-system.service', 'backup-system.timer'])
        listed = (self.etc / 'generated' / 'backup-system.paths').read_text().splitlines()
        self.assertEqual(listed, [units.MARKER, '/', '/home'])
        self.assertTrue((self.unit_dir / 'backup-system.service').is_file())

    def test_keep_flags_in_restics_order(self):
        self.assertEqual(units.restic_keep_args({'monthly': 6, 'daily': 7, 'yearly': 0}),
                         ['--keep-daily', '7', '--keep-monthly', '6'])


class ResticStatusTest(SystemSandbox):
    def test_snapshot_phase_counts_bytes_and_files(self):
        st = BackupStatus(state='running', phase='snapshot', phase_unit='bytes',
                          phase_done=1536 * 1024 ** 2, phase_total=4 * 1024 ** 3,
                          files_transferred=9512, files_total=40113, progress=37.5)
        self.assertEqual(st.phase_label, 'Taking a snapshot')
        self.assertEqual(st.phase_count, '1.5 GiB / 4.0 GiB  ·  9,512 / 40,113 files')

    def test_text_phases_count_their_unit(self):
        st = BackupStatus(state='running', phase='copy', phase_unit='packs', phase_done=35, phase_total=54)
        self.assertEqual((st.phase_label, st.phase_count), ('Copying snapshots', '35 / 54 packs'))

    def test_status_file_fields(self):
        d = self.dest()
        self.write_status(d, state='idle', engine='restic', phase_unit='')
        self.assertEqual(read_status(str(d)).to_dict()['engine'], 'restic')
        self.write_status(d, state='idle')
        self.assertEqual(read_status(str(d)).engine, 'rsync')


class JobLoadingTest(SystemSandbox):
    def install(self, jobs, owner='petr'):
        self.etc.mkdir(exist_ok=True)
        (self.etc / 'jobs.json').write_text(json.dumps({'version': 1, 'owner': owner, 'jobs': jobs}))

    def test_both_scopes_on_this_host(self):
        user_file = paths.jobs_file()
        user_file.parent.mkdir(parents=True)
        user_file.write_text(json.dumps({'jobs': [self.job('backup-user'),
                                                  self.restic_job('backup-sneaky')]}))
        self.install([self.restic_job(scope='user'), self.copy_job(hosts=['elsewhere'])])
        jobs = job_manager.load_all_jobs(host='here')
        self.assertEqual([(j['id'], units.job_scope(j)) for j in jobs],
                         [('backup-user', 'user'), ('backup-system', 'system')])
        self.assertEqual(job_manager.system_owner(), 'petr')

    def test_snapshot_names_scope_engine_and_paths(self):
        snap = snapshot.snapshot([self.restic_job()], timers={})
        job = snap['jobs'][0]
        self.assertEqual((job['scope'], job['engine'], job['source']), ('system', 'restic', '/, /home'))
        self.assertEqual(job_manager.source_label(self.copy_job()), 'snapshots of backup-system')

    def test_restic_jobs_need_paths_or_a_source_job(self):
        self.assertTrue(job_manager.usable_job(self.restic_job()))
        self.assertFalse(job_manager.usable_job(self.restic_job(restic={'mode': 'backup', 'paths': []})))
        self.assertFalse(job_manager.usable_job(self.copy_job(restic={'mode': 'copy'})))


class ControlTest(SystemSandbox):
    def run_control(self, jobs, job_id, action):
        calls = []

        def fake(*args, scope='user'):
            calls.append((scope, *args))
            return subprocess.CompletedProcess(args, 0, '', '')
        with mock.patch.object(cli, 'load_all_jobs', return_value=jobs), \
                mock.patch.object(cli, '_systemctl', side_effect=fake):
            rc = cli.control(job_id, action)
        return rc, calls

    def test_routes_by_scope_and_signals_backup_sync_only(self):
        jobs = [self.job('backup-user'), self.restic_job()]
        rc, calls = self.run_control(jobs, 'backup-system', 'pause')
        self.assertEqual((rc, calls), (0, [('system', 'kill', '--kill-whom=main', '--signal=USR1',
                                            'backup-system.service')]))
        _, calls = self.run_control(jobs, 'backup-user', 'start')
        self.assertEqual(calls, [('user', 'start', '--no-block', 'backup-user.service')])
        _, calls = self.run_control(jobs, 'backup-user', 'stop')
        self.assertEqual(calls, [('user', 'stop', '--no-block', 'backup-user.service')])

    def test_unknown_job(self):
        with mock.patch('sys.stderr', new_callable=io.StringIO):
            rc, calls = self.run_control([], 'backup-nope', 'start')
        self.assertEqual((rc, calls), (2, []))


class SystemApplyTest(SystemSandbox):
    def test_valid_jobs_pass(self):
        src = self.write_source([self.restic_job(exclude_file='system.exclude',
                                                 pre_command='mirror-backup-system-meta'),
                                 self.copy_job()],
                                **{'system.exclude': '/var/lib/docker\n'})
        self.assertEqual(system.validate(system.read_source(src), src), [])

    def test_what_could_hurt_as_root_is_refused(self):
        bad = self.restic_job(
            pre_command='/home/petr/evil.sh', exclude_file='../../etc/shadow', destination='/',
            restic={'mode': 'backup', 'paths': ['relative/path'], 'password_file': '/home/petr/key',
                    'keep': {'forever': 1}, 'check_subset': '5%; rm -rf /'})
        src = self.write_source([bad, self.copy_job(run_after='backup-missing')],
                                **{'mounts__etc.mount': '[Mount]\nWhere=/etc\nWhat=/dev/sda1\n'})
        problems = '\n'.join(system.validate(system.read_source(src), src))
        for expected in ('pre_command must be one of', 'exclude_file must be a file name',
                         'destination must be an absolute path other than /', 'not an absolute path',
                         'password_file must be a key in', 'unknown retention', 'check_subset',
                         'run_after names no other job', 'Where= must lie below'):
            self.assertIn(expected, problems)

    def test_mount_unit_name_must_match_where(self):
        src = self.write_source([self.restic_job()], **{
            'mounts__mnt-BACKUP.automount': '[Automount]\nWhere=/mnt/BACKUP\n',
            'mounts__backup.mount': '[Mount]\nWhere=/mnt/BACKUP\nWhat=/dev/x\n'})
        problems = system.validate(system.read_source(src), src)
        self.assertEqual(problems, ['mounts/backup.mount: a unit for /mnt/BACKUP must be named mnt-BACKUP.mount'])

    def test_plan_installs_jobs_excludes_mounts_and_polkit_rule(self):
        src = self.write_source([self.restic_job(exclude_file='system.exclude'), self.copy_job()],
                                **{'system.exclude': '/var/cache\n',
                                   'mounts__mnt-BACKUP.mount': '[Mount]\nWhere=/mnt/BACKUP\nWhat=UUID=x\n'})
        plan = system.make_plan(src, 'petr', host='here')
        written = {f.path: f.content for f in plan.files}
        installed = json.loads(written[self.etc / 'jobs.json'])
        self.assertEqual((installed['owner'], installed['files']), ('petr', ['system.exclude']))
        self.assertTrue(all(j['scope'] == 'system' for j in installed['jobs']))
        self.assertEqual(written[self.etc / 'system.exclude'], '/var/cache\n')
        self.assertTrue(written[self.unit_dir / 'mnt-BACKUP.mount'].startswith(units.MARKER + '\n'))
        rule = written[system.POLKIT_RULE]
        self.assertIn('subject.user != "petr"', rule)
        self.assertIn('["backup-system-nas.service", "backup-system.service"]', rule)
        self.assertEqual(len(plan.changes()), 4)

    def test_dry_run_shows_the_diff_and_changes_nothing(self):
        src = self.write_source([self.restic_job()])
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            rc = system.apply(src, dry_run=True, owner='petr')
        self.assertEqual(rc, 0)
        self.assertIn(f'+++ {self.etc}/jobs.json', out.getvalue())
        self.assertFalse((self.etc / 'jobs.json').exists())

    def test_drift_lists_what_apply_would_change(self):
        src = self.write_source([self.restic_job()])
        self.assertEqual(system.drift(src), [str(self.etc / 'jobs.json')])

    def test_no_drift_for_jobs_of_other_hosts(self):
        src = self.write_source([self.restic_job(hosts=['elsewhere'])])
        self.assertEqual(system.drift(src), [])

    def test_root_commands_refuse_without_root(self):
        if os.geteuid() == 0:
            self.skipTest('runs as root')
        with self.assertRaises(system.SystemError_):
            system.set_key('test')
        src = self.write_source([self.restic_job()])
        with self.assertRaises(system.SystemError_), mock.patch('sys.stdout', io.StringIO()):
            system.apply(src, owner='petr')

    def test_root_is_not_an_owner(self):
        with self.assertRaises(system.SystemError_):
            system.apply(self.write_source([self.restic_job()]), owner='root')
