"""System jobs: backups run by root, from system units.

A user job can read only what its user can. A backup of the whole system — to
restore it on bare metal — has to read every file, keep every owner and mode,
and write a repository the user cannot quietly change; that takes root. Such
jobs are kept apart:

* **Edited** in ``<config dir>/system/`` — ``jobs.json``, the exclude files it
  names, and ``mounts/`` (systemd mount and automount units for destinations).
  It sits in the user's config dir, so the overlay that carries the user jobs
  carries these too.
* **Installed** by ``mirror-backup system apply`` (root): checked, copied to
  ``/etc/mirror-backup``, and turned into system units. Root never reads its
  jobs from a file the user can write, and never runs a program the user can
  change: backup-sync and the pre-commands come from ``/usr/local/lib/
  mirror-backup`` (installed by ``install.sh --system``).
* **Controlled** by the user who applied them (the *owner*): a polkit rule,
  generated with the jobs, lets that user start, stop, pause and resume
  exactly these units from the panels; notifications go to that user's session.

Repository passwords never pass through the jobs: ``mirror-backup system
set-key <name>`` stores one, read from stdin, as ``keys/<name>.key`` (root,
0600), and jobs name the file.
"""

from __future__ import annotations

import difflib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from backup_monitor import paths, units
from backup_monitor.fsutil import atomic_write_text, trash
from backup_monitor.services.job_manager import load_system_jobs, system_owner, usable_job

# Programs in paths.SYSTEM_LIB_DIR a system job may run before it starts.
PRE_COMMANDS = ('mirror-backup-system-meta',)

POLKIT_RULE = Path('/etc/polkit-1/rules.d/50-mirror-backup.rules')
_KEY_NAME = re.compile(r'^[a-z0-9][a-z0-9-]*$')
_MOUNT_UNIT = re.compile(r'^[A-Za-z0-9:_.\\-]+\.(mount|automount)$')
_CHECK_SUBSET = re.compile(r'^(\d+(\.\d+)?%|\d+/\d+|\d+[KMGT]?)$')
# Where a mount unit from the overlay may mount something: never over the system.
_MOUNT_ROOTS = ('/mnt/', '/media/')


class SystemError_(Exception):
    """A system command that cannot go on; the message says why."""


def keys_dir() -> Path:
    return paths.system_config_dir() / 'keys'


def require_root(what: str):
    if os.geteuid() != 0:
        raise SystemError_(f'{what} needs root — run it with sudo (or pkexec)')


# ── reading and checking the edited jobs ──

def read_source(source_dir: Path) -> dict:
    path = source_dir / 'jobs.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise SystemError_(f'no system jobs in {path}')
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SystemError_(f'{path}: {e}')
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise SystemError_(f'{path}: expected {{"version": 1, "jobs": [...]}}')
    return data


def _calendar_ok(expression: str) -> bool:
    if not shutil.which('systemd-analyze'):
        return True
    result = subprocess.run(['systemd-analyze', 'calendar', expression],
                            capture_output=True, text=True, timeout=10)
    return result.returncode == 0


def validate(data: dict, source_dir: Path) -> list[str]:
    """Everything wrong with the edited system jobs; empty when they can be
    installed. Strict on purpose: what passes here runs as root."""
    problems: list[str] = []
    jobs = data.get('jobs', [])
    ids = [j.get('id') for j in jobs if isinstance(j, dict)]
    by_id = {j.get('id'): j for j in jobs if isinstance(j, dict)}
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f'job id {dup!r} is used more than once')
    for job in jobs:
        if not isinstance(job, dict):
            problems.append(f'not a job: {job!r:.80}')
            continue
        jid = str(job.get('id', ''))
        say = lambda msg, jid=jid: problems.append(f'{jid or "?"}: {msg}')  # noqa: E731
        if not units.valid_job_id(jid):
            say('the id must look like backup-<name> (lower case, digits, dashes)')
        if not usable_job(job):
            say('incomplete: needs a destination and something to back up')
        dst = str(job.get('destination', ''))
        if not dst.startswith('/') or dst.rstrip('/') == '':
            say(f'destination must be an absolute path other than /: {dst!r}')
        engine = job.get('engine', 'rsync')
        if engine not in ('rsync', 'restic'):
            say(f'unknown engine {engine!r}')
        if engine == 'rsync' and not str(job.get('source', '')).startswith('/'):
            say('an rsync job needs an absolute source')
        if engine == 'restic':
            problems.extend(f'{jid}: {p}' for p in _validate_restic(job, by_id))
        exclude = job.get('exclude_file', '') or ''
        if exclude:
            if '/' in exclude:
                say(f'exclude_file must be a file name next to jobs.json: {exclude!r}')
            elif not (source_dir / exclude).is_file():
                say(f'exclude file {exclude} is missing in {source_dir}')
        pre = job.get('pre_command', '') or ''
        if pre and pre not in PRE_COMMANDS:
            say(f'pre_command must be one of {", ".join(PRE_COMMANDS)} (programs Mirror Backup ships)')
        hosts = job.get('hosts', [])
        if not isinstance(hosts, list) or not all(isinstance(h, str) and h for h in hosts):
            say('hosts must be a list of host names')
        after = job.get('run_after')
        if after and (after not in by_id or after == jid):
            say(f'run_after names no other job here: {after!r}')
        schedule = job.get('schedule', {}) or {}
        if schedule.get('type', 'calendar') not in ('calendar', 'manual'):
            say(f'unknown schedule type {schedule.get("type")!r}')
        elif schedule.get('type', 'calendar') == 'calendar' \
                and not _calendar_ok(str(schedule.get('expression', 'daily'))):
            say(f'not a systemd calendar expression: {schedule.get("expression")!r}')
        try:
            if not 0 <= int(job.get('io_priority', 7)) <= 7 or not -20 <= int(job.get('nice', 10)) <= 19:
                say('nice must be -20..19 and io_priority 0..7')
        except (TypeError, ValueError):
            say('nice and io_priority must be numbers')
    problems.extend(_validate_mounts(source_dir))
    return problems


def _validate_restic(job: dict, by_id: dict) -> list[str]:
    problems = []
    restic = job.get('restic') or {}
    mode = restic.get('mode', 'backup')
    if mode == 'backup':
        listed = restic.get('paths', [])
        if not isinstance(listed, list) or not listed:
            problems.append('restic.paths must list the paths to back up')
        for p in listed if isinstance(listed, list) else []:
            if not isinstance(p, str) or not p.startswith('/') or '\n' in p:
                problems.append(f'not an absolute path: {p!r}')
    elif mode == 'copy':
        source = by_id.get(restic.get('from_job'))
        if source is None or source.get('engine') != 'restic':
            problems.append(f'restic.from_job names no restic job here: {restic.get("from_job")!r}')
    else:
        problems.append(f'unknown restic mode {mode!r}')
    key = str(restic.get('password_file', ''))
    if not key.startswith(str(keys_dir()) + '/'):
        problems.append(f'restic.password_file must be a key in {keys_dir()} '
                        f'(sudo {paths.SYSTEM_COMMAND} system set-key <name>)')
    elif os.geteuid() == 0 and not Path(key).is_file():
        problems.append(f'{key} does not exist yet — sudo {paths.SYSTEM_COMMAND} system set-key '
                        f'{Path(key).stem} < password')
    for name, value in (restic.get('keep') or {}).items():
        if name not in units._KEEP_FLAGS:
            problems.append(f'unknown retention restic.keep.{name}')
        elif not isinstance(value, int) or value < 0:
            problems.append(f'restic.keep.{name} must be a whole number ≥ 0')
    for name in ('prune_every_days', 'check_every_days'):
        value = restic.get(name, 7)
        if not isinstance(value, int) or value < 0:
            problems.append(f'restic.{name} must be a whole number of days (0 = never)')
    subset = str(restic.get('check_subset', '5%'))
    if not _CHECK_SUBSET.match(subset):
        problems.append(f'restic.check_subset is not something restic check --read-data-subset takes: {subset!r}')
    host = restic.get('host', '')
    if host and not re.match(r'^[A-Za-z0-9._-]+$', str(host)):
        problems.append(f'restic.host is not a host name: {host!r}')
    return problems


def _validate_mounts(source_dir: Path) -> list[str]:
    problems = []
    for path in sorted((source_dir / 'mounts').glob('*')) if (source_dir / 'mounts').is_dir() else []:
        if not _MOUNT_UNIT.match(path.name):
            problems.append(f'mounts/{path.name}: only .mount and .automount units go here')
            continue
        where = _unit_setting(path.read_text(encoding='utf-8', errors='replace'), 'Where')
        if not where or not where.startswith(_MOUNT_ROOTS):
            problems.append(f'mounts/{path.name}: Where= must lie below {" or ".join(_MOUNT_ROOTS)}')
            continue
        expected = _escape_path(where) + path.suffix
        if path.name != expected:
            problems.append(f'mounts/{path.name}: a unit for {where} must be named {expected}')
    return problems


def _unit_setting(text: str, key: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f'{key}='):
            return line.split('=', 1)[1].strip()
    return ''


def _escape_path(path: str) -> str:
    """systemd-escape --path, for the paths the mounts may use."""
    result = subprocess.run(['systemd-escape', '--path', path], capture_output=True, text=True)
    return result.stdout.strip()


# ── what an apply writes ──

@dataclass
class Planned:
    path: Path
    content: str
    mode: int = 0o644


@dataclass
class Plan:
    owner: str
    jobs: list[dict]
    files: list[Planned] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)       # unit names
    obsolete: list[Path] = field(default_factory=list)    # installed by us before, gone now

    def changes(self) -> list[tuple[Planned, str]]:
        """(file, current content or '') for every file that would change."""
        out = []
        for f in self.files:
            try:
                current = f.path.read_text(encoding='utf-8')
            except FileNotFoundError:
                current = ''
            except PermissionError:
                current = '\0unreadable'
            if current != f.content:
                out.append((f, current))
        return out


def render_polkit_rule(jobs: list[dict], owner: str) -> str:
    """Lets ``owner``, in an active local session, start, stop and signal the
    service units of exactly these jobs — nothing else."""
    services = sorted(f'{j["id"]}.service' for j in jobs)
    return '\n'.join([
        '// ' + units.MARKER.lstrip('# '),
        '// Lets the owner of the Mirror Backup system jobs start, stop, pause and',
        '// resume them from the panels. Generated by `mirror-backup system apply`.',
        'polkit.addRule(function (action, subject) {',
        '    if (action.id != "org.freedesktop.systemd1.manage-units") return;',
        f'    if (subject.user != {json.dumps(owner)} || !subject.local || !subject.active) return;',
        f'    var units = {json.dumps(services)};',
        '    var verbs = ["start", "stop", "restart", "kill"];',
        '    if (units.indexOf(action.lookup("unit")) >= 0 && verbs.indexOf(action.lookup("verb")) >= 0)',
        '        return polkit.Result.YES;',
        '});',
        '',
    ])


def make_plan(source_dir: Path, owner: str, host: str | None = None) -> Plan:
    data = read_source(source_dir)
    problems = validate(data, source_dir)
    if problems:
        raise SystemError_('the system jobs cannot be installed:\n  ' + '\n  '.join(problems))
    jobs = [{**j, 'scope': 'system'} for j in data['jobs']]
    config = paths.system_config_dir()
    excludes = sorted({j['exclude_file'] for j in jobs if j.get('exclude_file')})
    installed = {
        'version': 1,
        'owner': owner,
        # what apply installed besides jobs.json, so the next one can remove leftovers
        'files': excludes,
        'jobs': jobs,
    }
    plan = Plan(owner=owner, jobs=jobs)
    plan.files.append(Planned(config / 'jobs.json',
                              json.dumps(installed, indent=2, ensure_ascii=False) + '\n'))
    for name in excludes:
        plan.files.append(Planned(config / name, (source_dir / name).read_text(encoding='utf-8')))
    mounts_dir = source_dir / 'mounts'
    for path in sorted(mounts_dir.glob('*')) if mounts_dir.is_dir() else []:
        plan.files.append(Planned(paths.SYSTEM_UNIT_DIR / path.name,
                                  units.MARKER + '\n' + path.read_text(encoding='utf-8')))
        plan.mounts.append(path.name)
    here = [j for j in jobs if units.runs_here(j, host)]
    plan.files.append(Planned(POLKIT_RULE, render_polkit_rule(here, owner)))

    previous = {}
    try:
        previous = json.loads(paths.system_jobs_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    for name in previous.get('files', []):
        if name not in excludes and '/' not in name:
            plan.obsolete.append(config / name)
    for path in sorted(paths.SYSTEM_UNIT_DIR.glob('*.mount')) + sorted(paths.SYSTEM_UNIT_DIR.glob('*.automount')):
        if path.name not in plan.mounts and units.is_generated(path):
            plan.obsolete.append(path)
    return plan


def show_plan(plan: Plan, out=None) -> bool:
    """Print what the apply changes; True when anything does."""
    out = out or sys.stdout
    changed = plan.changes()
    for f, current in changed:
        if current == '\0unreadable':
            print(f'--- {f.path} (not readable here) — would be written', file=out)
            continue
        diff = difflib.unified_diff(current.splitlines(True), f.content.splitlines(True),
                                    str(f.path) if current else '/dev/null', str(f.path))
        out.writelines(diff)
    for path in plan.obsolete:
        print(f'--- {path}: no longer needed — to the trash', file=out)
    return bool(changed or plan.obsolete)


# ── commands ──

def invoking_user() -> str:
    """Who asked for this root command: pkexec and sudo both say."""
    uid = os.environ.get('PKEXEC_UID')
    if uid and uid.isdigit():
        try:
            return pwd.getpwuid(int(uid)).pw_name
        except KeyError:
            pass
    return os.environ.get('SUDO_USER', '')


def default_source_dir(owner: str) -> Path:
    """The owner's ``~/.config/backup-sync/system``. The edited copy lives in
    the owner's config dir, not root's."""
    if owner:
        try:
            home = Path(pwd.getpwnam(owner).pw_dir)
            return home / '.config' / 'backup-sync' / 'system'
        except KeyError:
            pass
    return paths.system_source_dir()


def apply(source_dir: Path | None = None, *, dry_run: bool = False, owner: str = '') -> int:
    owner = owner or invoking_user() or system_owner()
    if not owner:
        raise SystemError_('who owns the system jobs? Run through sudo or pkexec, or pass --owner')
    if owner == 'root':
        raise SystemError_('the owner must be the desktop user who watches the jobs, not root')
    source_dir = source_dir or default_source_dir(owner)
    plan = make_plan(source_dir, owner)
    changed = show_plan(plan)
    if dry_run:
        print('(dry run — nothing changed)' if changed else 'system jobs up to date')
        return 0
    require_root('Installing the system jobs')

    (paths.system_config_dir()).mkdir(mode=0o755, parents=True, exist_ok=True)
    keys_dir().mkdir(mode=0o700, exist_ok=True)
    mounts_changed = False
    for f, current in plan.changes():
        if current and current != '\0unreadable':
            trash(f.path)
        atomic_write_text(f.path, f.content, mode=f.mode)
        mounts_changed |= f.path.parent == paths.SYSTEM_UNIT_DIR
        print(f'wrote {f.path}')
    for path in plan.obsolete:
        if path.parent == paths.SYSTEM_UNIT_DIR:
            units.systemctl(['disable', '--now', path.name], 'system')
            mounts_changed = True
        print(f'{path} → {trash(path)}')
    if mounts_changed:
        units.systemctl(['daemon-reload'], 'system')
    for name in plan.mounts:
        # An automount is what is wanted where there is one; its mount comes with it.
        if name.endswith('.mount') and f'{name[:-len(".mount")]}.automount' in plan.mounts:
            continue
        result = units.systemctl(['enable', '--now', name], 'system')
        if result.returncode != 0:
            print(f'enable {name}: {result.stderr.strip()}', file=sys.stderr)

    report = units.sync_units(plan.jobs, scope='system', owner=plan.owner)
    for line in report.lines():
        print(f'units {line}')
    return 1 if report.errors else 0


def _job(job_id: str) -> dict:
    for job in load_system_jobs():
        if job['id'] == job_id:
            return job
    raise SystemError_(f'no installed system job {job_id!r} (sudo {paths.SYSTEM_COMMAND} system apply first)')


def declared_mount_points() -> set[str]:
    """Mount points fstab or a mount unit declares — mounted or not."""
    points = set()
    result = subprocess.run(['findmnt', '-s', '-n', '-o', 'TARGET'], capture_output=True, text=True)
    points.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    for unit_dir in (paths.SYSTEM_UNIT_DIR, Path('/usr/lib/systemd/system')):
        for unit in unit_dir.glob('*.mount') if unit_dir.is_dir() else []:
            where = _unit_setting(unit.read_text(encoding='utf-8', errors='replace'), 'Where')
            if where:
                points.add(where.rstrip('/') or '/')
    return points


def unmounted_on_the_way(path: Path) -> str:
    """The first declared mount point on the way from / to ``path`` that is
    not mounted; '' when there is none. Looking at it starts an automount."""
    declared = declared_mount_points()
    current = Path('/')
    for part in path.parts[1:]:
        current = current / part
        if str(current) in declared and not os.path.ismount(current):
            return str(current)
    return ''


def restic_env(job: dict) -> dict[str, str]:
    restic = job.get('restic') or {}
    env = dict(os.environ)
    env['RESTIC_REPOSITORY'] = str(Path(job['destination']) / 'repo')
    env['RESTIC_PASSWORD_FILE'] = restic.get('password_file', '')
    if units.job_scope(job) == 'system':
        # the cache the system unit uses (CacheDirectory=mirror-backup)
        env.setdefault('RESTIC_CACHE_DIR', '/var/cache/mirror-backup/restic')
    return env


def init(job_id: str) -> int:
    """Create the destination directory and the restic repository of a job.
    A copy job's repository takes the chunker parameters of the one it copies
    from, so the copies deduplicate like the originals."""
    require_root('Initialising a repository')
    job = _job(job_id)
    if units.job_engine(job) != 'restic':
        raise SystemError_(f'{job_id} is not a restic job')
    dst = Path(job['destination'])
    unmounted = unmounted_on_the_way(dst)
    if unmounted:
        raise SystemError_(f'{unmounted} is not mounted — the destination would land on the disk below it')
    dst.mkdir(mode=0o755, parents=True, exist_ok=True)
    repo = dst / 'repo'
    if (repo / 'config').is_file():
        print(f'{repo}: already a repository')
        return 0
    cmd = ['restic', 'init']
    restic = job.get('restic') or {}
    if restic.get('mode') == 'copy':
        source = _job(restic['from_job'])
        cmd += ['--from-repo', str(Path(source['destination']) / 'repo'),
                '--from-password-file', (source.get('restic') or {}).get('password_file', ''),
                '--copy-chunker-params']
    return subprocess.run(cmd, env=restic_env(job)).returncode


def backup_args(job: dict) -> list[str]:
    """`restic backup` arguments as backup-sync builds them for this job."""
    restic = job.get('restic') or {}
    args = ['--one-file-system', '--exclude-caches',
            '--host', restic.get('host') or paths.host_name(),
            '--exclude', str(Path(job['destination'].rstrip('/') or '/'))]
    exclude = units.resolve_job_file(job, job.get('exclude_file', '') or '')
    if exclude and Path(exclude).is_file():
        args += ['--exclude-file', exclude]
    return [*args, '--', *restic.get('paths', [])]


def dry_run(job: dict) -> int:
    """What the next snapshot of a restic backup job would add, without
    taking it. A system job needs root (it reads everything)."""
    if units.job_scope(job) == 'system':
        require_root('A dry run of a system job')
    if units.job_engine(job) != 'restic' or (job.get('restic') or {}).get('mode', 'backup') != 'backup':
        raise SystemError_(f'{job["id"]} is not a restic backup job')
    result = subprocess.run(['restic', 'backup', '--dry-run', '--json', *backup_args(job)],
                            capture_output=True, text=True, env=restic_env(job))
    summary = next((json.loads(line) for line in result.stdout.splitlines()
                    if line.startswith('{"message_type":"summary"')), None)
    unreadable = [json.loads(line).get('item', '') for line in result.stderr.splitlines()
                  if line.startswith('{"message_type":"error"')]
    if summary is None:
        print(result.stderr.strip() or f'restic exited {result.returncode}', file=sys.stderr)
        return result.returncode or 1
    from backup_monitor.models.job import format_size
    print(f'{summary.get("files_new", 0):,} new and {summary.get("files_changed", 0):,} changed files '
          f'would add {format_size(summary.get("data_added", 0))}')
    print(f'{summary.get("total_files_processed", 0):,} files, '
          f'{format_size(summary.get("total_bytes_processed", 0))} in all')
    for item in unreadable[:20]:
        print(f'unreadable: {item}')
    if len(unreadable) > 20:
        print(f'… and {len(unreadable) - 20} more unreadable')
    return 0


def restic_exec(job_id: str, args: list[str]) -> int:
    """restic against a job's repository, its key and cache set."""
    require_root('Opening a system repository')
    job = _job(job_id)
    os.execvpe('restic', ['restic', *args], restic_env(job))
    return 127  # not reached


def set_key(name: str, *, replace: bool = False) -> int:
    """Store a repository password read from stdin. An existing, different key
    is kept unless replace is asked for: the repository opens with the old one."""
    require_root('Storing a repository key')
    if not _KEY_NAME.match(name):
        raise SystemError_(f'not a key name: {name!r} (lower case, digits, dashes)')
    secret = sys.stdin.read().rstrip('\n')
    if not secret:
        raise SystemError_('no password on stdin')
    keys_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(keys_dir(), 0o700)
    path = keys_dir() / f'{name}.key'
    if path.exists():
        if path.read_text(encoding='utf-8').rstrip('\n') == secret:
            print(f'{path}: unchanged')
            return 0
        if not replace:
            raise SystemError_(f'{path} holds a different password — repositories created with it '
                               f'would no longer open. Pass --replace if that is really meant.')
        print(f'{path} → {trash(path)}')
    atomic_write_text(path, secret + '\n', mode=0o600)
    print(f'wrote {path}')
    return 0


def drift(source_dir: Path | None = None) -> list[str]:
    """Files an apply would change — what the user edited but did not install.
    Readable without root, except the polkit rule (which only follows the jobs)."""
    source_dir = source_dir or paths.system_source_dir()
    if not (source_dir / 'jobs.json').is_file():
        return []
    try:
        plan = make_plan(source_dir, system_owner() or os.environ.get('USER', ''))
    except SystemError_ as e:
        return [str(e)]
    # The overlay is shared with the other systems of a dual boot: jobs meant
    # for other hosts only, with nothing installed here, are nothing to install.
    if not paths.system_jobs_file().exists() and not any(units.runs_here(j) for j in plan.jobs):
        return []
    return [str(f.path) for f, current in plan.changes()
            if f.path != POLKIT_RULE and current != '\0unreadable'] + [str(p) for p in plan.obsolete]
