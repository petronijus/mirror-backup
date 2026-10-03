import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as LoginManager from 'resource:///org/gnome/shell/misc/loginManager.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const HOME = GLib.get_home_dir();
const JOBS_FILE = GLib.build_filenamev([HOME, '.config', 'backup-sync', 'jobs.json']);
const BIN_DIR = GLib.build_filenamev([HOME, '.local', 'bin']);
const MIRROR_BACKUP = GLib.build_filenamev([BIN_DIR, 'mirror-backup']);
const APP_HOME = GLib.build_filenamev([GLib.get_user_data_dir(), 'mirror-backup']);

// Poll `mirror-backup status` this often (seconds): quickly while a backup
// runs, lazily otherwise. Opening the menu always asks at once.
const POLL_ACTIVE_S = 3;
const POLL_IDLE_S = 15;

// `mirror-backup status` exits with this when a suspend is already under way:
// it read nothing, which is no error to show.
const EXIT_SUSPENDING = 75;

// Read the user's jobs from jobs.json (the same source the desktop app uses)
// so the menu can be built before the first status arrives.
function _loadJobs() {
    try {
        const [ok, contents] = GLib.file_get_contents(JOBS_FILE);
        if (!ok) return [];
        const data = JSON.parse(new TextDecoder().decode(contents));
        return (data.jobs ?? [])
            .filter(j => j && j.id)
            .map(j => ({id: j.id, name: j.name ?? j.id, service: `${j.id}.service`}));
    } catch (_e) {
        return [];
    }
}

function _runSystemctlAsync(args, callback) {
    try {
        const proc = Gio.Subprocess.new(
            ['systemctl', '--user', ...args],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        );
        proc.communicate_utf8_async(null, null, (_proc, res) => {
            try {
                const [, stdout] = _proc.communicate_utf8_finish(res);
                if (callback) callback(stdout?.trim() ?? '', null);
            } catch (e) {
                if (callback) callback('', e);
            }
        });
    } catch (e) {
        log(`[BackupMonitor] systemctl error: ${e.message}`);
        if (callback) callback('', e);
    }
}

// Every job's state, as `mirror-backup status --json` reports it. The rules
// for where a job's state lives (its destination) and when a run counts as
// alive are the app's; the panel only displays the result. Calls back with
// neither a snapshot nor an error when the command skipped a pending suspend.
function _fetchSnapshot(cancellable, callback) {
    try {
        const proc = Gio.Subprocess.new(
            [MIRROR_BACKUP, 'status', '--json'],
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        );
        proc.communicate_utf8_async(null, cancellable, (_proc, res) => {
            try {
                const [, stdout, stderr] = _proc.communicate_utf8_finish(res);
                if (_proc.get_if_exited() && _proc.get_exit_status() === EXIT_SUSPENDING) {
                    callback(null, null);
                    return;
                }
                if (!_proc.get_successful()) {
                    callback(null, stderr?.trim() || 'mirror-backup status failed');
                    return;
                }
                callback(JSON.parse(stdout), null);
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    callback(null, e.message);
            }
        });
    } catch (e) {
        callback(null, `mirror-backup not available: ${e.message}`);
    }
}

function _formatCountdown(isoTimestamp) {
    const target = new Date(isoTimestamp).getTime();
    if (isNaN(target)) return '';
    const sec = Math.floor((target - Date.now()) / 1000);
    if (sec <= 0) return 'now';

    const days = Math.floor(sec / 86400);
    const hours = Math.floor((sec % 86400) / 3600);
    const minutes = Math.floor((sec % 3600) / 60);

    if (days > 0) return `in ${days}d ${hours}h`;
    if (hours > 0) return `in ${hours}h ${minutes}m`;
    if (minutes > 0) return `in ${minutes}m`;
    return `in ${sec}s`;
}

function _formatRelativePast(isoTimestamp) {
    const then = new Date(isoTimestamp);
    if (isNaN(then.getTime())) return '';
    const sec = Math.floor((Date.now() - then.getTime()) / 1000);
    const clock = `${String(then.getHours()).padStart(2, '0')}:${String(then.getMinutes()).padStart(2, '0')}`;
    if (sec < 0) return clock;
    if (sec < 60) return 'just now';
    const now = new Date();
    const dayOf = d => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const days = Math.round((dayOf(now) - dayOf(then)) / 86400000);
    if (days <= 0)
        return sec < 3600 ? `${Math.floor(sec / 60)}m ago` : `${Math.floor(sec / 3600)}h ago`;
    if (days === 1) return `yesterday ${clock}`;
    if (days < 7) return `${then.toLocaleDateString('en-US', {weekday: 'short'})} ${clock}`;
    return then.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
}

class BackupJobSection {
    constructor(job, menu) {
        this._job = job;
        this._paused = false;
        this._snap = null;

        this._item = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        this._box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            style_class: 'bm-job',
        });
        this._item.add_child(this._box);

        // ── header row: dot · name · state label ──
        this._headerRow = new St.BoxLayout({
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'bm-header',
        });

        this._dot = new St.Icon({
            icon_name: 'media-record-symbolic',
            icon_size: 10,
            style_class: 'bm-dot bm-dot-idle',
        });
        this._headerRow.add_child(this._dot);

        this._nameLabel = new St.Label({
            text: job.name,
            x_expand: true,
            style_class: 'bm-name',
        });
        this._headerRow.add_child(this._nameLabel);

        this._stateLabel = new St.Label({
            text: 'Idle',
            style_class: 'bm-state',
        });
        this._headerRow.add_child(this._stateLabel);

        this._box.add_child(this._headerRow);

        // ── progress bar ──
        this._progressTrack = new St.BoxLayout({
            style_class: 'bm-progress-track',
            x_expand: true,
        });
        this._progressFill = new St.Widget({
            style_class: 'bm-progress-fill',
        });
        this._progressTrack.add_child(this._progressFill);
        this._box.add_child(this._progressTrack);
        this._progressTrack.visible = false;

        // ── detail rows ──
        this._detailBox = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            style_class: 'bm-details',
        });

        this._fileLabel = new St.Label({
            text: '',
            style_class: 'bm-detail bm-file',
        });
        this._fileLabel.clutter_text.set_ellipsize(3); // END
        this._detailBox.add_child(this._fileLabel);

        this._statsLabel = new St.Label({
            text: '',
            style_class: 'bm-detail',
        });
        this._detailBox.add_child(this._statsLabel);

        this._filesLabel = new St.Label({
            text: '',
            style_class: 'bm-detail',
        });
        this._detailBox.add_child(this._filesLabel);

        this._box.add_child(this._detailBox);
        this._detailBox.visible = false;

        // ── error row ──
        this._errorLabel = new St.Label({
            text: '',
            style_class: 'bm-error',
        });
        this._errorLabel.clutter_text.set_line_wrap(true);
        this._box.add_child(this._errorLabel);
        this._errorLabel.visible = false;

        // ── schedule row: next run · last run (on whichever machine) ──
        this._countdownLabel = new St.Label({
            text: '',
            style_class: 'bm-detail bm-countdown',
        });
        this._box.add_child(this._countdownLabel);

        // ── action buttons ──
        this._btnRow = new St.BoxLayout({style_class: 'bm-buttons'});

        this._startBtn = this._iconButton(
            'media-playback-start-symbolic', () => this._onStart());
        this._pauseBtn = this._iconButton(
            'media-playback-pause-symbolic', () => this._onPause());
        this._stopBtn = this._iconButton(
            'media-playback-stop-symbolic', () => this._onStop());

        this._btnRow.add_child(this._startBtn);
        this._btnRow.add_child(this._pauseBtn);
        this._btnRow.add_child(this._stopBtn);
        this._box.add_child(this._btnRow);

        this._separator = new PopupMenu.PopupSeparatorMenuItem();
        menu.addMenuItem(this._item);
        menu.addMenuItem(this._separator);
    }

    get state() {
        return this._snap?.status?.state ?? 'idle';
    }

    _iconButton(iconName, callback) {
        const btn = new St.Button({
            style_class: 'bm-icon-btn',
            can_focus: true,
            child: new St.Icon({icon_name: iconName, icon_size: 16}),
        });
        btn.connect('clicked', () => {
            callback();
            return Clutter.EVENT_STOP;
        });
        return btn;
    }

    update(snapJob) {
        this._snap = snapJob;
        const status = snapJob?.status ?? null;
        const state = this.state;
        const lastOk = snapJob?.last_run?.exit_code === 0;

        // header
        const names = {
            idle: 'Idle',
            queued: 'Queued',
            scanning: 'Scanning',
            running: 'Syncing',
            paused: 'Paused',
            deferred: 'Postponed',
            error: 'Error',
            unavailable: 'Unavailable',
        };
        const isSuccess = state === 'idle' && lastOk;
        const streak = status?.consecutive_failures ?? 0;
        let stateText = names[state] ?? state;
        if (streak >= 2)
            stateText = `${stateText}  ${streak}×`;
        this._stateLabel.text = stateText;
        this._dot.style_class = isSuccess
            ? 'bm-dot bm-dot-success' : `bm-dot bm-dot-${state}`;

        const isActive =
            state === 'running' || state === 'paused' || state === 'scanning';
        const isQueued = state === 'queued';

        // progress bar
        this._progressTrack.visible = isActive;
        if (isActive && status) {
            const pct = Math.min(100, Math.max(0, status.progress ?? 0));
            const tw = this._progressTrack.get_width();
            if (tw > 0)
                this._progressFill.set_width(Math.round(tw * pct / 100));
            this._progressFill.style_class = state === 'paused'
                ? 'bm-progress-fill bm-progress-paused' : 'bm-progress-fill';
        }

        // detail rows: what the run is at, how far it has got, and the count.
        // phase_label, phase_count and progress_text are worded by
        // mirror-backup (models/job.py); a status written by a backup-sync
        // without phases has none of them.
        this._detailBox.visible = isActive;
        if (isActive && status) {
            let headline = status.phase_label || '';
            if (!headline && status.current_file)
                headline = this._shortenPath(status.current_file);
            if (!headline && state === 'scanning')
                headline = 'Building file list…';
            this._fileLabel.text = headline;
            this._fileLabel.visible = headline !== '';

            const parts = [];
            const pct = status.progress_text ??
                (status.progress > 0 ? `${Math.round(status.progress)}%` : '');
            if (pct) parts.push(pct);
            if (status.speed) parts.push(status.speed);
            if (status.eta && status.eta !== '0:00:00')
                parts.push(`ETA ${status.eta}`);
            if (state === 'scanning') {
                if (!status.phase && status.scan_read && status.scan_read !== '0 B')
                    parts.push(`${status.scan_read} read`);
                if (status.started) {
                    const elapsed = this._formatElapsed(status.started);
                    if (elapsed) parts.push(elapsed);
                }
            }
            this._statsLabel.text = parts.join('  ·  ');
            this._statsLabel.visible = parts.length > 0;

            let count = status.phase_count || '';
            if (!count && status.files_total > 0)
                count = `${status.files_transferred} / ${status.files_total} files`;
            this._filesLabel.text = count;
            this._filesLabel.visible = count !== '';
        }

        // error, or why the destination cannot be used
        if ((state === 'error' || state === 'unavailable') && status?.error) {
            this._errorLabel.text = status.error;
            this._errorLabel.visible = true;
        } else {
            this._errorLabel.visible = false;
        }

        // schedule: when a postponed run restarts, or next run · last run
        const parts = [];
        if (state === 'deferred') {
            parts.push(status?.deferred_reason === 'suspend' ? 'Restarts after wake'
                : status?.deferred_reason === 'interrupted'
                    ? 'Interrupted — restarts at next start' : 'Restarts at next start');
        } else if (!isActive && !isQueued) {
            const cd = snapJob?.next_run ? _formatCountdown(snapJob.next_run) : '';
            if (cd) parts.push(`Next ${cd}`);
            const last = snapJob?.last_run;
            if (last) {
                let text = `last ${_formatRelativePast(last.started)}`;
                if (last.host) text += ` on ${last.host}`;
                if (last.exit_code !== 0) text += ' (failed)';
                parts.push(text);
            }
        }
        this._countdownLabel.text = parts.join('  ·  ');
        this._countdownLabel.visible = parts.length > 0;

        // buttons
        this._startBtn.visible = !isActive && !isQueued && state !== 'unavailable';
        this._pauseBtn.visible = isActive;
        this._stopBtn.visible = isActive || isQueued;
        this._pauseBtn.child.icon_name = state === 'paused'
            ? 'media-playback-start-symbolic' : 'media-playback-pause-symbolic';
    }

    _shortenPath(p) {
        if (!p) return '';
        const parts = p.replace(/\/$/, '').split('/');
        if (parts.length <= 2) return p;
        return '…/' + parts.slice(-2).join('/');
    }

    _formatElapsed(isoStarted) {
        const startMs = new Date(isoStarted).getTime();
        if (isNaN(startMs)) return '';
        const sec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
        if (sec < 1) return '';
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        if (h > 0)
            return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    // ── actions ──

    _onStart() {
        _runSystemctlAsync(['start', this._job.service], () => this.onChanged?.());
        this._paused = false;
    }

    _onStop() {
        // backup-sync resumes a paused rsync itself before stopping it.
        _runSystemctlAsync(['stop', this._job.service], () => this.onChanged?.());
        this._paused = false;
    }

    _onPause() {
        if (this._paused || this.state === 'paused') {
            _runSystemctlAsync(['kill', '--signal=USR2', this._job.service], () => this.onChanged?.());
            this._paused = false;
        } else {
            _runSystemctlAsync(['kill', '--signal=USR1', this._job.service], () => this.onChanged?.());
            this._paused = true;
        }
    }

    destroy() {
        this._item.destroy();
        this._separator.destroy();
    }
}

/* ------------------------------------------------------------- panel position */

// Follow the panel box chosen for tray icons in Ubuntu AppIndicators, so this
// indicator sits with them instead of being stranded on the right when the tray
// is moved to the centre. The schema belongs to that extension and is absent on
// machines without it, hence the lookup and the fallback.
const TRAY_POS_SCHEMA = 'org.gnome.shell.extensions.appindicator';
const TRAY_POS_KEY = 'tray-pos';
const PANEL_BOXES = ['left', 'center', 'right'];
const DEFAULT_PANEL_BOX = 'right';

function getTrayPosSettings() {
    const schema = Gio.SettingsSchemaSource.get_default()?.lookup(TRAY_POS_SCHEMA, true);
    return schema ? new Gio.Settings({settings_schema: schema}) : null;
}

function trayPanelBox(settings) {
    const pos = settings?.get_string(TRAY_POS_KEY);
    return PANEL_BOXES.includes(pos) ? pos : DEFAULT_PANEL_BOX;
}

export default class BackupMonitorExtension extends Extension {
    enable() {
        // First-run setup: install backup-sync, the app and its units from a
        // release zip (install.sh does all of this itself)
        this._firstRunSetup();

        this._indicator = new PanelMenu.Button(0.0, 'Mirror Backup', false);

        this._panelIcon = new St.Icon({
            icon_name: 'drive-harddisk-symbolic',
            style_class: 'system-status-icon',
        });
        this._indicator.add_child(this._panelIcon);
        this._pulsing = false;

        this._indicator.menu.box.add_style_class_name('bm-menu');

        // Shown when `mirror-backup status` cannot be read
        this._problemItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._problemItem.label.add_style_class_name('bm-error');
        this._problemItem.visible = false;
        this._indicator.menu.addMenuItem(this._problemItem);

        this._jobSections = [];
        this._jobsSection = new PopupMenu.PopupMenuSection();
        this._indicator.menu.addMenuItem(this._jobsSection);
        this._rebuildSections(_loadJobs());

        // "Open Mirror Backup" button at the bottom
        const openAppItem = new PopupMenu.PopupMenuItem('Open Mirror Backup');
        openAppItem.label.add_style_class_name('bm-open-app');
        openAppItem.connect('activate', () => {
            try {
                Gio.Subprocess.new([MIRROR_BACKUP], Gio.SubprocessFlags.NONE);
            } catch (e) {
                log(`[BackupMonitor] Failed to launch app: ${e.message}`);
            }
        });
        this._indicator.menu.addMenuItem(openAppItem);

        this._watchTrayPos();
        this._placeIndicator();

        this._indicator.menu.connect('open-state-changed', (_menu, open) => {
            if (open) this._refresh();
        });

        this._cancellable = new Gio.Cancellable();
        this._fetching = false;
        this._pollId = null;

        // Destinations may be FUSE: a status read in flight when userspace is
        // frozen aborts the suspend. Stop polling once one is announced (a
        // read already running holds logind off by itself) and pick up again
        // after wake.
        this._asleep = false;
        this._loginManager = LoginManager.getLoginManager();
        this._sleepId = this._loginManager.connect('prepare-for-sleep',
            (_lm, aboutToSuspend) => this._onPrepareForSleep(aboutToSuspend));

        this._refresh();
    }

    _onPrepareForSleep(aboutToSuspend) {
        this._asleep = aboutToSuspend;
        if (aboutToSuspend) {
            if (this._pollId) {
                GLib.source_remove(this._pollId);
                this._pollId = null;
            }
        } else {
            this._refresh();
        }
    }

    _rebuildSections(jobs) {
        for (const s of this._jobSections) s.destroy();
        this._jobSections = [];
        for (const job of jobs) {
            const section = new BackupJobSection(job, this._jobsSection);
            section.onChanged = () => this._refreshSoon();
            this._jobSections.push(section);
        }
    }

    // Copy a directory tree (the bundled app) file by file.
    _copyTree(src, dst) {
        if (!dst.query_exists(null))
            dst.make_directory_with_parents(null);
        const children = src.enumerate_children('standard::name,standard::type',
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
        let info;
        while ((info = children.next_file(null)) !== null) {
            const name = info.get_name();
            if (name === '__pycache__') continue;
            const child = src.get_child(name);
            if (info.get_file_type() === Gio.FileType.DIRECTORY)
                this._copyTree(child, dst.get_child(name));
            else
                child.copy(dst.get_child(name), Gio.FileCopyFlags.OVERWRITE, null, null);
        }
        children.close(null);
    }

    _readText(file) {
        try {
            const [, bytes] = file.load_contents(null);
            return new TextDecoder().decode(bytes);
        } catch (_e) {
            return null;
        }
    }

    _firstRunSetup() {
        const extDir = this.dir.get_path();
        GLib.mkdir_with_parents(BIN_DIR, 0o755);
        GLib.mkdir_with_parents(GLib.build_filenamev([HOME, '.config', 'backup-sync']), 0o755);

        // backup-sync and mirror-backup from the bundled copies
        for (const name of ['backup-sync', 'mirror-backup']) {
            try {
                const srcFile = Gio.File.new_for_path(
                    GLib.build_filenamev([extDir, 'scripts', name]));
                if (!srcFile.query_exists(null)) continue;
                const target = Gio.File.new_for_path(GLib.build_filenamev([BIN_DIR, name]));
                srcFile.copy(target, Gio.FileCopyFlags.OVERWRITE, null, null);
                target.set_attribute_uint32('unix::mode', 0o755, Gio.FileQueryInfoFlags.NONE, null);
            } catch (e) {
                log(`[BackupMonitor] Failed to install ${name}: ${e.message}`);
            }
        }

        // The app, where the mirror-backup command expects it — refreshed
        // whenever the bundled version differs from the installed one.
        try {
            const bundled = Gio.File.new_for_path(GLib.build_filenamev([extDir, 'app']));
            if (bundled.query_exists(null)) {
                const versionOf = root => this._readText(
                    root.get_child('backup_monitor').get_child('__init__.py'));
                const installed = Gio.File.new_for_path(GLib.build_filenamev([APP_HOME, 'app']));
                if (versionOf(bundled) !== versionOf(installed)) {
                    this._copyTree(bundled, installed);
                    const css = Gio.File.new_for_path(GLib.build_filenamev([extDir, 'data', 'style.css']));
                    if (css.query_exists(null)) {
                        const dataDir = Gio.File.new_for_path(GLib.build_filenamev([APP_HOME, 'data']));
                        if (!dataDir.query_exists(null))
                            dataDir.make_directory_with_parents(null);
                        css.copy(dataDir.get_child('style.css'), Gio.FileCopyFlags.OVERWRITE, null, null);
                    }
                }
            }
        } catch (e) {
            log(`[BackupMonitor] Failed to install the app: ${e.message}`);
        }

        // The unit that syncs job units and restarts postponed backups at the
        // next start. Bundled in release zips only; install.sh does it itself.
        try {
            const unitName = 'mirror-backup-resume.service';
            const unitSrc = Gio.File.new_for_path(
                GLib.build_filenamev([extDir, 'systemd', unitName]));
            if (unitSrc.query_exists(null)) {
                const unitDir = GLib.build_filenamev([HOME, '.config', 'systemd', 'user']);
                const unitDst = Gio.File.new_for_path(
                    GLib.build_filenamev([unitDir, unitName]));
                const wanted = Gio.File.new_for_path(
                    GLib.build_filenamev([unitDir, 'default.target.wants', unitName]));
                const current = this._readText(unitSrc) === this._readText(unitDst);
                if (!current || !wanted.query_exists(null)) {
                    GLib.mkdir_with_parents(unitDir, 0o755);
                    unitSrc.copy(unitDst, Gio.FileCopyFlags.OVERWRITE, null, null);
                    _runSystemctlAsync(['daemon-reload'],
                        () => _runSystemctlAsync(['enable', unitName]));
                }
            }
        } catch (e) {
            log(`[BackupMonitor] Failed to install mirror-backup-resume.service: ${e.message}`);
        }

        // Install .desktop file for the app launcher
        try {
            const desktopSource = GLib.build_filenamev([extDir, 'data',
                'com.github.petronijus.BackupMonitor.desktop']);
            const desktopDir = GLib.build_filenamev([GLib.get_user_data_dir(), 'applications']);
            const desktopTarget = GLib.build_filenamev([desktopDir,
                'com.github.petronijus.BackupMonitor.desktop']);
            GLib.mkdir_with_parents(desktopDir, 0o755);
            const src = Gio.File.new_for_path(desktopSource);
            if (src.query_exists(null)) {
                src.copy(Gio.File.new_for_path(desktopTarget),
                    Gio.FileCopyFlags.OVERWRITE, null, null);
            }
        } catch (_e) { /* ignore */ }
    }

    disable() {
        if (this._sleepId) {
            this._loginManager.disconnect(this._sleepId);
            this._sleepId = null;
        }
        this._loginManager = null;
        this._unwatchTrayPos();
        this._stopPulse();
        this._cancellable?.cancel();
        this._cancellable = null;
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = null;
        }
        for (const s of this._jobSections) s.destroy();
        this._jobSections = [];
        this._indicator?.destroy();
        this._indicator = null;
    }

    // A control was just used: ask again shortly, once the job has had a
    // moment to write its first status.
    _refreshSoon() {
        this._schedulePoll(1);
    }

    _schedulePoll(seconds) {
        if (this._pollId)
            GLib.source_remove(this._pollId);
        this._pollId = null;
        if (this._asleep) return;
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
            this._pollId = null;
            this._refresh();
            return GLib.SOURCE_REMOVE;
        });
    }

    _refresh() {
        if (this._fetching || !this._cancellable || this._asleep) return;
        this._fetching = true;
        _fetchSnapshot(this._cancellable, (snap, error) => {
            this._fetching = false;
            if (!this._indicator) return;
            if (!snap && !error) {     // skipped for a suspend; wake polls again
                this._schedulePoll(POLL_IDLE_S);
                return;
            }
            const anyActive = this._apply(snap, error);
            this._schedulePoll(anyActive ? POLL_ACTIVE_S : POLL_IDLE_S);
        });
    }

    // Returns whether a backup is active, which sets the next poll interval.
    _apply(snap, error) {
        this._problemItem.visible = !!error;
        if (error) {
            this._problemItem.label.text = error;
            this._panelIcon.style_class = 'system-status-icon bm-icon-error';
            this._stopPulse();
            return false;
        }

        const jobs = snap.jobs ?? [];
        const ids = jobs.map(j => j.id).join('\n');
        if (ids !== this._jobSections.map(s => s._job.id).join('\n'))
            this._rebuildSections(jobs.map(j => ({id: j.id, name: j.name, service: j.service})));

        let anyActive = false;
        let anyError = false;
        jobs.forEach((job, i) => {
            const section = this._jobSections[i];
            section.update(job);
            const st = section.state;
            if (st === 'running' || st === 'paused' || st === 'scanning' || st === 'queued')
                anyActive = true;
            if (st === 'error')
                anyError = true;
        });

        if (anyError) {
            this._panelIcon.style_class = 'system-status-icon bm-icon-error';
            anyActive ? this._startPulse() : this._stopPulse();
        } else if (anyActive) {
            this._panelIcon.style_class = 'system-status-icon bm-icon-active';
            this._startPulse();
        } else {
            this._panelIcon.style_class = 'system-status-icon';
            this._stopPulse();
        }
        return anyActive;
    }

    _startPulse() {
        if (this._pulsing) return;
        this._pulsing = true;
        this._doPulse();
    }

    _doPulse() {
        if (!this._pulsing || !this._panelIcon) return;
        this._panelIcon.ease({
            opacity: 80,
            duration: 1000,
            mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
            onComplete: () => {
                if (!this._pulsing || !this._panelIcon) return;
                this._panelIcon.ease({
                    opacity: 255,
                    duration: 1000,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                    onComplete: () => {
                        // Break synchronous recursion — Clutter may fire
                        // onComplete immediately if the animation is skipped,
                        // causing "too much recursion" stack overflow.
                        GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                            this._doPulse();
                            return GLib.SOURCE_REMOVE;
                        });
                    },
                });
            },
        });
    }

    _stopPulse() {
        this._pulsing = false;
        if (this._panelIcon) {
            this._panelIcon.remove_all_transitions();
            this._panelIcon.opacity = 255;
        }
    }

    _placeIndicator() {
        if (!this._indicator)
            return;

        // Re-adding the very same indicator is how ubuntu-appindicators moves its
        // own icons between boxes; the role has to be cleared first, or
        // addToStatusArea refuses it as a conflict.
        Main.panel.statusArea['backup-monitor'] = null;
        Main.panel.addToStatusArea('backup-monitor', this._indicator, 0,
            trayPanelBox(this._trayPosSettings));
    }

    _watchTrayPos() {
        this._trayPosSettings = getTrayPosSettings();
        this._trayPosChangedId = this._trayPosSettings?.connect(
            `changed::${TRAY_POS_KEY}`, () => this._placeIndicator());
    }

    _unwatchTrayPos() {
        if (this._trayPosChangedId) {
            this._trayPosSettings.disconnect(this._trayPosChangedId);
            this._trayPosChangedId = null;
        }
        this._trayPosSettings = null;
    }
}
