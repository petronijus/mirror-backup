import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const JOBS_FILE = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'backup-sync', 'jobs.json',
]);

// Read the user's jobs from jobs.json (the same source the desktop app uses)
// so the panel reflects their actual backup jobs rather than a fixed list.
function _loadJobs() {
    try {
        const [ok, contents] = GLib.file_get_contents(JOBS_FILE);
        if (!ok) return [];
        const data = JSON.parse(new TextDecoder().decode(contents));
        return (data.jobs ?? [])
            .filter(j => j && j.id)
            .map(j => ({id: j.id, label: j.name ?? j.id, service: `${j.id}.service`}));
    } catch (_e) {
        return [];
    }
}

const STATUS_DIR = GLib.build_filenamev([
    GLib.get_home_dir(), '.local', 'share', 'backup-sync', 'status',
]);

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

function _isBackupAlive(pid) {
    if (!pid || pid <= 0) return false;
    try {
        const [ok, data] = GLib.file_get_contents(`/proc/${pid}/cmdline`);
        if (!ok) return false;
        return new TextDecoder().decode(data).includes('backup-sync');
    } catch (_e) {
        return false;
    }
}

function _formatCountdown(isoTimestamp) {
    try {
        const target = new Date(isoTimestamp).getTime();
        if (isNaN(target)) return '';
        const now = Date.now();
        let sec = Math.floor((target - now) / 1000);
        if (sec <= 0) return 'now';

        const days = Math.floor(sec / 86400);
        const hours = Math.floor((sec % 86400) / 3600);
        const minutes = Math.floor((sec % 3600) / 60);

        if (days > 0) return `in ${days}d ${hours}h`;
        if (hours > 0) return `in ${hours}h ${minutes}m`;
        if (minutes > 0) return `in ${minutes}m`;
        return `in ${sec}s`;
    } catch (_e) {
        return '';
    }
}

function _parseSystemdTimestamp(ts) {
    // "Thu 2026-03-27 18:00:00 CET" → ISO string
    const parts = ts.split(/\s+/);
    if (parts.length >= 3)
        return `${parts[1]}T${parts[2]}`;
    return '';
}

function _readStatusFile(jobId) {
    const path = GLib.build_filenamev([STATUS_DIR, `${jobId}.json`]);
    try {
        const [ok, contents] = GLib.file_get_contents(path);
        if (!ok) return null;
        const status = JSON.parse(new TextDecoder().decode(contents));

        const st = status.state;
        if ((st === 'running' || st === 'scanning' || st === 'paused' || st === 'queued')
            && !_isBackupAlive(status.pid)) {
            status.state = 'idle';
            status.progress = 0;
            status.speed = '';
            status.eta = '';
            status.current_file = '';
            status.error = '';
        }

        return status;
    } catch (_e) {
        // Missing or invalid — normal for first run
    }
    return null;
}

class BackupJobSection {
    constructor(job, menu) {
        this._job = job;
        this._paused = false;
        this._status = null;
        this._nextRunIso = '';  // ISO timestamp for countdown

        this._item = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
        });
        this._box = new St.BoxLayout({
            vertical: true,
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
            text: job.label,
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
            vertical: true,
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

        // ── countdown row ──
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

        menu.addMenuItem(this._item);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
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

    update() {
        const status = _readStatusFile(this._job.id);
        this._status = status;
        const state = status?.state ?? 'idle';

        // header
        const names = {
            idle: 'Idle',
            queued: 'Queued',
            scanning: 'Scanning',
            running: 'Syncing',
            paused: 'Paused',
            deferred: 'Postponed',
            error: 'Error',
        };
        const isSuccess = state === 'idle' && (status?.progress ?? 0) >= 100;
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

        // detail rows
        this._detailBox.visible = isActive;
        if (isActive && status) {
            if (state === 'scanning') {
                this._fileLabel.text = 'Building file list\u2026';
                this._fileLabel.visible = true;

                const sp = [];
                if (status.scan_read)
                    sp.push(`${status.scan_read} read`);
                if (status.started) {
                    const elapsed = this._formatElapsed(status.started);
                    if (elapsed) sp.push(elapsed);
                }
                this._statsLabel.text = sp.join('  \u00b7  ');
                this._statsLabel.visible = sp.length > 0;
                this._filesLabel.visible = false;
            } else {
                // current file
                if (status.current_file) {
                    this._fileLabel.text = this._shortenPath(status.current_file);
                    this._fileLabel.visible = true;
                } else {
                    this._fileLabel.visible = false;
                }

                // stats: percentage · speed · ETA
                const parts = [];
                if (status.progress > 0) parts.push(`${status.progress}%`);
                if (status.speed) parts.push(status.speed);
                if (status.eta && status.eta !== '0:00:00')
                    parts.push(`ETA ${status.eta}`);
                this._statsLabel.text = parts.join('  \u00b7  ');
                this._statsLabel.visible = parts.length > 0;

                // file counts
                if (status.files_total > 0) {
                    this._filesLabel.text =
                        `${status.files_transferred}\u2009/\u2009${status.files_total} files`;
                    this._filesLabel.visible = true;
                } else {
                    this._filesLabel.visible = false;
                }
            }
        }

        // error
        if (state === 'error' && status?.error) {
            this._errorLabel.text = status.error;
            this._errorLabel.visible = true;
        } else {
            this._errorLabel.visible = false;
        }

        // countdown, or when a postponed run restarts
        if (state === 'deferred') {
            this._countdownLabel.text = status?.deferred_reason === 'suspend'
                ? 'Restarts after wake' : 'Restarts at next start';
            this._countdownLabel.visible = true;
        } else if (!isActive && !isQueued && this._nextRunIso) {
            const cd = _formatCountdown(this._nextRunIso);
            this._countdownLabel.text = cd ? `Next ${cd}` : '';
            this._countdownLabel.visible = !!cd;
        } else {
            this._countdownLabel.visible = false;
        }

        // buttons
        this._startBtn.visible = !isActive && !isQueued;
        this._pauseBtn.visible = isActive;
        this._stopBtn.visible = isActive || isQueued;
        this._pauseBtn.child.icon_name = state === 'paused'
            ? 'media-playback-start-symbolic' : 'media-playback-pause-symbolic';
    }

    _shortenPath(p) {
        if (!p) return '';
        const parts = p.replace(/\/$/, '').split('/');
        if (parts.length <= 2) return p;
        return '\u2026/' + parts.slice(-2).join('/');
    }

    _formatElapsed(isoStarted) {
        try {
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
        } catch (_e) {
            return '';
        }
    }

    // ── actions ──

    _onStart() {
        _runSystemctlAsync(['start', this._job.service]);
        this._paused = false;
    }

    _onStop() {
        if (this._paused || this._status?.state === 'paused')
            _runSystemctlAsync(['kill', '--signal=USR2', this._job.service]);
        _runSystemctlAsync(['stop', this._job.service]);
        this._paused = false;
    }

    _onPause() {
        if (this._paused || this._status?.state === 'paused') {
            _runSystemctlAsync(['kill', '--signal=USR2', this._job.service]);
            this._paused = false;
        } else {
            _runSystemctlAsync(['kill', '--signal=USR1', this._job.service]);
            this._paused = true;
        }
    }

    destroy() {}
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
        // First-run setup: install backup-sync script and create directories
        this._firstRunSetup();

        this._indicator = new PanelMenu.Button(0.0, 'Mirror Backup for GNOME', false);

        this._panelIcon = new St.Icon({
            icon_name: 'drive-harddisk-symbolic',
            style_class: 'system-status-icon',
        });
        this._indicator.add_child(this._panelIcon);
        this._pulsing = false;

        this._indicator.menu.box.add_style_class_name('bm-menu');

        this._jobSections = [];
        for (const job of _loadJobs()) {
            const section = new BackupJobSection(job, this._indicator.menu);
            this._jobSections.push(section);
        }

        // "Open Mirror Backup" button at the bottom
        const openAppItem = new PopupMenu.PopupMenuItem('Open Mirror Backup');
        openAppItem.label.add_style_class_name('bm-open-app');
        const extPath = this.dir.get_path();
        openAppItem.connect('activate', () => {
            try {
                // App is bundled inside the extension directory at app/
                const appSrc = GLib.build_filenamev([extPath, 'app']);
                Gio.Subprocess.new(
                    ['bash', '-c',
                     `PYTHONPATH="${appSrc}:\${PYTHONPATH:-}" exec python3 -m backup_monitor.main`],
                    Gio.SubprocessFlags.NONE,
                );
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

        this._pollId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, 3, () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });

        // Fetch timer info every 30 seconds
        this._timerPollId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, 30, () => {
                this._fetchTimerInfo();
                return GLib.SOURCE_CONTINUE;
            });

        this._refresh();
        this._fetchTimerInfo();
    }

    _firstRunSetup() {
        // Install backup-sync script from the bundled copy if not present
        const extDir = this.dir.get_path();
        const home = GLib.get_home_dir();
        const binDir = GLib.build_filenamev([home, '.local', 'bin']);
        const target = GLib.build_filenamev([binDir, 'backup-sync']);
        const source = GLib.build_filenamev([extDir, 'scripts', 'backup-sync']);

        // Create directories
        for (const dir of [
            binDir,
            GLib.build_filenamev([home, '.local', 'share', 'backup-sync', 'status']),
            GLib.build_filenamev([home, '.local', 'share', 'backup-sync', 'logs']),
            GLib.build_filenamev([home, '.local', 'share', 'backup-sync', 'history']),
            GLib.build_filenamev([home, '.config', 'backup-sync']),
        ]) {
            GLib.mkdir_with_parents(dir, 0o755);
        }

        // Copy backup-sync if bundled and newer or missing
        try {
            const srcFile = Gio.File.new_for_path(source);
            if (srcFile.query_exists(null)) {
                const dstFile = Gio.File.new_for_path(target);
                srcFile.copy(dstFile, Gio.FileCopyFlags.OVERWRITE, null, null);
                // Make executable
                try {
                    Gio.Subprocess.new(
                        ['chmod', '+x', target],
                        Gio.SubprocessFlags.NONE,
                    );
                } catch (_e) { /* ignore */ }
            }
        } catch (_e) {
            // Already exists or no bundled script — fine
        }

        // Install and enable the unit that restarts postponed backups at the
        // next start. Bundled in release zips only; install.sh does it itself.
        try {
            const unitName = 'mirror-backup-resume.service';
            const unitSrc = Gio.File.new_for_path(
                GLib.build_filenamev([extDir, 'systemd', unitName]));
            if (unitSrc.query_exists(null)) {
                const unitDir = GLib.build_filenamev([home, '.config', 'systemd', 'user']);
                const unitDst = Gio.File.new_for_path(
                    GLib.build_filenamev([unitDir, unitName]));
                const wanted = Gio.File.new_for_path(
                    GLib.build_filenamev([unitDir, 'default.target.wants', unitName]));
                const decoder = new TextDecoder();
                const [, srcBytes] = unitSrc.load_contents(null);
                let current = false;
                if (unitDst.query_exists(null)) {
                    const [, dstBytes] = unitDst.load_contents(null);
                    current = decoder.decode(srcBytes) === decoder.decode(dstBytes);
                }
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
            const desktopDir = GLib.build_filenamev([home, '.local', 'share', 'applications']);
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
        this._unwatchTrayPos();
        this._stopPulse();
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = null;
        }
        if (this._timerPollId) {
            GLib.source_remove(this._timerPollId);
            this._timerPollId = null;
        }
        for (const s of this._jobSections) s.destroy();
        this._jobSections = [];
        this._indicator?.destroy();
        this._indicator = null;
    }

    _fetchTimerInfo() {
        // Fetch NextElapseUSecRealtime for each job's timer
        for (const section of this._jobSections) {
            const timerName = section._job.id + '.timer';
            _runSystemctlAsync(
                ['show', timerName,
                 '--property=NextElapseUSecRealtime'],
                (stdout, err) => {
                    if (err || !stdout) return;
                    for (const line of stdout.split('\n')) {
                        if (line.startsWith('NextElapseUSecRealtime=')) {
                            const val = line.split('=')[1]?.trim();
                            if (val && val !== 'n/a')
                                section._nextRunIso = _parseSystemdTimestamp(val);
                        }
                    }
                },
            );
        }
    }

    _refresh() {
        let anyActive = false;
        let anyError = false;

        for (const s of this._jobSections) {
            s.update();
            const st = s._status?.state;
            if (st === 'running' || st === 'paused' || st === 'scanning' || st === 'queued')
                anyActive = true;
            if (st === 'error')
                anyError = true;
        }

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
