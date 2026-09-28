# Mirror Backup

> Scheduled rsync mirroring, watched live from your panel — GNOME Shell or Omarchy.

Rsync-based backup for Linux with systemd scheduling, a panel indicator (GNOME Shell, or the Omarchy bar), and a GTK4/libadwaita desktop app — set up jobs once, watch them in the corner of your eye. Several installs can share one set of jobs and one state — the two operating systems of a dual boot, say — and each run counts for all of them.

## Screenshots

*Coming soon.*

<!-- Add PNGs to docs/screenshots/ (see that folder's README for capture commands),
     then uncomment the gallery below:
| Dashboard | Job editor | Panel indicator |
|---|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Job editor](docs/screenshots/job-editor.png) | ![Panel indicator](docs/screenshots/panel.png) |
-->

## Components

```
backup-sync (bash)           — rsync wrapper with progress tracking, job queue & notifications
systemd user timers          — scheduling (persistent, survives reboots), generated from jobs.json
mirror-backup (command)      — the app, and `status` / `sync-units` / `resume` / `migrate-legacy`
mirror-backup-resume.service — at login: syncs the units with jobs.json, restarts interrupted runs
GNOME Shell extension        — panel indicator with live status, controls
Omarchy bar widget           — the same in the Omarchy (Hyprland) bar
Desktop app (GTK4)           — job management, configuration, history, logs
```

## Desktop App

The GTK4/libadwaita desktop app provides full backup management:

### Dashboard
- Real-time status cards for all backup jobs
- Progress bars with speed, ETA, file counts
- Live countdown to next scheduled run (e.g. "in 2h 15m"), relative last-run time
- Start/Pause/Resume/Stop controls
- Click card → detail page, edit pencil → job editor

### Job Management
- Create, edit, delete backup jobs from the UI
- Source/destination folder pickers
- Visual schedule editor:
  - **Weekly**: weekday pill buttons (multi-select), interval spinner ("every N weeks")
  - **Monthly**: day-of-month picker, interval spinner ("every N months")
  - **Time**: clean HH:MM display with ±30min step buttons
  - **Custom**: raw systemd calendar expression
  - **Manual**: no automatic scheduling
  - Human-readable summary (e.g. "Every week on Mon, Wed, Fri at 22:00")
  - Daily = weekly with all 7 days selected
- Visual exclusion pattern editor (add/remove/toggle patterns)
- Jobs stored in `~/.config/backup-sync/jobs.json`
- Generates systemd service+timer units on save

### Advanced Rsync Options (per job)
- **Delete mode**: before/during/after transfer, or disabled (additive backup)
- **Compression**: compress data during transfer (useful for slow links)
- **Checksum verification**: compare by content instead of time+size
- **Hard links**: detect and preserve hard links
- **Extended attributes**: preserve xattrs
- **ACLs**: preserve filesystem access control lists
- **Partial transfers**: resume interrupted file transfers
- **Skip newer**: don't overwrite newer files on destination
- **File size filters**: max-size and min-size limits
- **Bandwidth limiting**: throttle transfer speed (KB/s)
- **Archive retention**: keep deleted/changed files for N days

### History & Logs
- Per-job run history with duration, file counts, exit codes
- Statistics: success rate, average duration, last success/failure
- Full log viewer with search highlighting and auto-tail
- History stored as JSONL in each destination (`<destination>/.mirror-backup/history.jsonl`)

### Preferences
- Notification settings (on start, complete, error)
- Default values for new jobs (archive, bandwidth, nice, I/O priority)
- Default rsync options for new jobs
- Log retention settings

### Other
- Dry-run preview (see what would change without transferring)
- Keyboard shortcuts (Ctrl+N, Ctrl+R, Ctrl+,)
- About dialog
- Job queue: backups run one at a time (flock-based), "Queued" state visible in UI

### On Omarchy

Omarchy leaves GTK apps on stock Adwaita; this one takes the current Omarchy
theme instead — its palette (`colors.toml`), the control tokens the shell
draws with (`shell.toml`), the monospace UI font, square corners, flat
hairline borders and the shell's small-caps section headers, dark or light as
the theme says. Switching the theme restyles the open window. The look is
`src/backup_monitor/adw_omarchy.py` — a self-contained module other libadwaita
apps can carry too (gdrive-for-linux does): `scripts/vendor-adw-omarchy.sh
<app>/adw_omarchy.py` copies it there, stamped with its hash, and `--check`
tells a current copy from an outdated or hand-edited one. `desktop_theme.py`
adds Mirror Backup's own widgets. `MIRROR_BACKUP_THEME=adwaita` (or `omarchy`) overrides the detection.

### Launch

```bash
./run.sh
```

## Panel indicators

Both read `mirror-backup status --json`, so the rules for where a job's state
lives and when a run counts as alive exist once, in the app.

### GNOME Shell extension

- Panel icon with color-coded status (blue = running, yellow = paused, red = error, gray = queued, light blue = postponed)
- Per-job controls: Start, Stop, Pause/Resume
- Progress bar with speed and ETA
- Live countdown to next run per job
- Pulsing icon when backups are active
- Last run and where it ran ("last 2h ago on omarchy")
- "Open Mirror Backup" button to launch the desktop app
- Polls every 3 s while a backup runs, every 15 s otherwise

### Omarchy bar widget

`omarchy-plugin/` — a bar widget for [Omarchy](https://omarchy.org) 4's shell
(`petronijus.mirror-backup`). One long-running `mirror-backup status --watch`
feeds it a JSON line per change.

- Icon turns urgent on an error and pulses while a backup runs
- Panel: every job with route, progress, speed/ETA, schedule and last run; start / pause / resume / stop
- Bar icon: left = panel, right = open the app, middle = refresh
- Panel keys: `j`/`k` move, Enter = the row's main action, `x` stop, `o` open the app, `r` refresh
- IPC: `omarchy-shell petronijus.mirror-backup open|close|toggle|refresh|status`

## Backup Jobs

Jobs are created and managed in the desktop app and stored in
`~/.config/backup-sync/jobs.json` (the source of truth — systemd units are
generated from it). Exclude files may be named relative to that directory. A typical setup looks like:

| Job | Source | Destination | Schedule | Archive |
|-----|--------|-------------|----------|---------|
| `backup-documents` | `/mnt/data/Documents/` | `/mnt/backup/Documents/` | Every 2 days | 60 days |
| `backup-photos` | `/mnt/data/Pictures/` | `/mnt/backup/Photos/` | Daily | None |
| `backup-projects` | `/mnt/data/Projects/` | `/mnt/fast/Projects/` | Every 6 hours | None |

## Install

**From a release** (recommended): download the extension zip from
[Releases](../../releases) and

```bash
gnome-extensions install --force backup-monitor@petronijus.zip
```

**From source**:

```bash
./install.sh                    # picks the panel for the desktop in use
./install.sh --desktop omarchy  # or say which: gnome | omarchy | none
```

On GNOME, log out and in (Wayland) for a newly installed extension to appear;
on Omarchy the widget is live at once. Then create your backup jobs in the app
(`mirror-backup`, or the panel's "Open Mirror Backup").

Re-running `install.sh` updates an install in place. An install from before
0.6 is migrated: its state in `~/.local/share/backup-sync` moves into the
destinations (`mirror-backup migrate-legacy --retire`, idempotent) and the
old directory goes to the trash.

## Several machines, one set of jobs

Everything a job knows about itself lives **in its destination**, in
`<destination>/.mirror-backup/`: the live status, the run history, the log and
the postponed-run marker. Whatever mounts the destination — another machine,
or the other operating system of a dual boot — sees the same state:

- **One history.** A run counts wherever it happened; history entries and the
  panels name the host (`last 2h ago on petronijus-PC`).
- **No double runs.** Persistent timers catch up on runs missed while an
  install was down. A catch-up (systemd passes `TRIGGER_UNIT` to timer-started
  runs) is skipped when the first scheduled time after the last *successful*
  run — from any machine — still lies in the future. Manual starts always run.
- **Interrupted runs carry over.** The status records the boot it was written
  in; an active status from another boot is an interrupted run, and
  `mirror-backup resume` restarts it at the next login, on whichever machine.
- **rsync leaves the state alone.** `--exclude=.mirror-backup/` keeps it out of
  `--delete`, and out of mirrors of sources that contain another job's destination.

The **jobs** are shared by pointing `~/.config/backup-sync` at the same
directory — `install.sh` links it to `private/configs/backup-sync` when that
private overlay holds a `jobs.json`. Each install generates its own systemd
units from it; `mirror-backup-resume.service` re-syncs them at every login, so
a job added or changed on one machine appears on the other.

The queue lock, PID and rsync progress files are per boot and stay local, in
`$XDG_RUNTIME_DIR/backup-sync/`.

## Uninstall

```bash
chmod +x uninstall.sh
./uninstall.sh
```

## Manual Commands

```bash
# Every job at a glance (--json: the snapshot the panels read)
mirror-backup status

# Bring the units in line with jobs.json after editing it by hand
mirror-backup sync-units

# Run a backup now
systemctl --user start backup-documents

# Stop a running backup
systemctl --user stop backup-documents

# Pause / resume
systemctl --user kill --signal=USR1 backup-documents   # pause
systemctl --user kill --signal=USR2 backup-documents   # resume

# Check timer schedule
systemctl --user list-timers 'backup-*'

# View backup logs
cat /mnt/backup/Documents/.mirror-backup/backup.log
```

## Error Handling

- **Unmounted disks**: a destination that does not exist is never created — a missing destination is a missing mount far more often than a new job. Every mount point `fstab` lists on the way to the source or the destination must be mounted: an unmounted one is an empty directory, which as a destination would fill the system disk and as a source would make `--delete` empty the mirror. As a last line, a source that is empty while its mirror is not is refused unless deletion is off.
- **A scheduled run another machine already made** is skipped, and the log says which run covered it (see "Several machines, one set of jobs").

- **Partial transfer tolerance**: rsync exit codes 23 (permission denied on some files) and 24 (files vanished during transfer) are treated as success with warnings, not failures. This prevents backup jobs from showing as "error" due to a single inaccessible file (e.g. Windows system files on NTFS mounts).
- **Default excludes**: new exclude files include `$RECYCLE.BIN`, `System Volume Information`, and `WpSystem` by default — common Windows system directories that are inaccessible or irrelevant on Linux.
- **Suspend**: while rsync runs, `backup-sync` holds a logind *delay* inhibitor. When a suspend is announced it stops rsync, lets the suspend proceed, and starts the run over after wake (state "Postponed" in between). This matters for FUSE sources and destinations (NTFS via ntfs-3g, rclone, sshfs): an rsync blocked in a FUSE request cannot be frozen — the freezer stops the FUSE daemon too — so the kernel would abort the suspend after 20 s. Stopping rsync *before* the freeze, while the FUSE daemon still answers, avoids that. A job the user paused stays paused across the restart. logind waits at most `InhibitDelayMaxSec` (5 s by default) for the inhibitor; rsync normally exits well within that, but a slow FUSE source can make raising it in `logind.conf` worthwhile.
- **Shutdown / logout**: a stop that arrives while the user's service manager or the system is going down postpones the run instead of failing it — a `<job>.deferred` marker is left in the status directory, and `mirror-backup-resume.service` starts the job again at the next boot or login. Jobs waiting in the queue are postponed the same way.
- **Crashes, power loss, the other OS**: at the next start, `mirror-backup-resume.service` also restarts any job whose status still says running, scanning, paused or queued but was written in another boot — of this machine or another one.
- **Manual stop**: stopping a job yourself (Stop button, `systemctl --user stop`) is a real interruption — it is marked "error" with "Backup interrupted" and not restarted.
- **Atomic status writes**: status JSON is written to a temp file and renamed into place (`mv -f`), preventing UI flickering caused by readers seeing a partially written or truncated file.

## How It Works

1. **Scheduling**: systemd timers trigger `backup-sync` at configured intervals
2. **Queue**: `flock` ensures only one backup runs at a time; others wait in "queued" state
3. **Sync**: `backup-sync` runs rsync with configurable options and tracks progress
4. **Status**: Progress written to `<destination>/.mirror-backup/status.json`, with the host and boot that wrote it
5. **History**: Each completed run appends to `<destination>/.mirror-backup/history.jsonl`
6. **Panels**: The GNOME extension polls `mirror-backup status --json` (3 s while active, 15 s idle); the Omarchy widget keeps `mirror-backup status --watch` running
7. **Desktop app**: Polls at 1 second + inotify for instant updates
8. **Signals**: SIGUSR1 pauses rsync (SIGSTOP), SIGUSR2 resumes (SIGCONT); SIGTERM stops it — or postpones the run when the system is going down
9. **Suspend guard**: logind delay inhibitor + `PrepareForSleep` watch while rsync runs; stop before suspend, rerun after wake
10. **Archive**: Deleted/changed files kept in `.archive/` with configurable retention
11. **Resume**: `mirror-backup-resume.service` (once per user-manager start) runs `mirror-backup sync-units` and `mirror-backup resume`, which restarts postponed and interrupted runs
12. **Notifications**: Desktop notifications on start, finish, pause, resume, suspend, and errors

## File Layout

```
~/.local/bin/backup-sync                              # sync script
~/.local/bin/mirror-backup                            # the app, and its commands
~/.local/share/mirror-backup/app/                     # the app (Python package)
~/.config/backup-sync/jobs.json                       # job configuration (source of truth; may be a link)
~/.config/backup-sync/settings.json                   # app preferences
~/.config/backup-sync/*.exclude                       # rsync exclude patterns
~/.config/systemd/user/backup-*.{service,timer}       # systemd units (generated from jobs.json)
~/.config/systemd/user/mirror-backup-resume.service   # syncs units, restarts interrupted runs at start
<destination>/.mirror-backup/status.json              # the job's status (shared by every machine)
<destination>/.mirror-backup/history.jsonl            # run history
<destination>/.mirror-backup/backup.log               # rsync log
<destination>/.mirror-backup/deferred                 # marker: run postponed, restart at next start
$XDG_RUNTIME_DIR/backup-sync/queue.lock               # job queue lock (this boot)
$XDG_RUNTIME_DIR/backup-sync/<job>.{pid,progress}     # live run files (this boot)
~/.local/share/gnome-shell/extensions/backup-monitor@petronijus/   # GNOME panel
~/.config/omarchy/plugins/petronijus.mirror-backup/                # Omarchy panel
```

## Tests

```bash
tests/run.sh
```

Shell syntax, the GTK-free Python core (`tests/test_backup_monitor.py`:
paths, status rules, unit generation, migration, resume), the Omarchy widget's
formatting under node (`tests/test_omarchy_model.mjs`), and `backup-sync` end
to end against throwaway directories (`tests/test_backup_sync.sh`: state in
the destination, `--delete` sparing it, unmounted source and destination,
empty source, scheduled runs covered or due). Nothing touches real backups,
the desktop or systemd.

## Releases

Everything ships as a single GNOME Shell extension with the desktop app
bundled inside. Tags `vX.Y.Z` build two artifacts in CI and attach them to the
GitHub release:

- `backup-monitor@petronijus.zip` — the extension, installable with
  `gnome-extensions install --force <zip>` (no root needed; log out/in to
  activate). Includes the panel indicator, the bundled GTK4 app and the
  `backup-sync` script.
- `mirror-backup-vX.Y.Z.tar.gz` — source tarball for `./install.sh` installs.

See [CHANGELOG.md](CHANGELOG.md) for version history.

## License

[MIT](LICENSE)
