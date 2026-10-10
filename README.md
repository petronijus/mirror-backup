# Mirror Backup

> Scheduled rsync mirrors and restic snapshots, watched live from your panel — GNOME Shell or Omarchy.

Backup for Linux with systemd scheduling, a panel indicator (GNOME Shell, or the Omarchy bar), and a GTK4/libadwaita desktop app — set up jobs once, watch them in the corner of your eye. A job either **mirrors** a folder with rsync (plain files, latest state) or keeps **snapshots** of any set of paths with restic (every version, deduplicated, encrypted) — and runs either as you or, for a backup of the whole system, as root. Several installs can share one set of jobs and one state — the two operating systems of a dual boot, say — and each run counts for all of them.

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
backup-sync (bash)           — runs a job (rsync or restic) with progress tracking, job queue & notifications
systemd user timers          — scheduling (persistent, survives reboots), generated from jobs.json
mirror-backup (command)      — the app, and `status` / `control` / `dry-run` / `sync-units` / `resume` /
                               `system` / `migrate-legacy`
mirror-backup-resume.service — at login: syncs the units with jobs.json, restarts interrupted runs
system part (--system)       — root-owned copy in /usr/local/lib/mirror-backup, system units for
                               system jobs, the shared queue, a polkit rule for their controls
GNOME Shell extension        — panel indicator with live status, controls
Omarchy bar widget           — the same in the Omarchy (Hyprland) bar
Desktop app (GTK4)           — job management, configuration, history, logs
```

## Desktop App

The GTK4/libadwaita desktop app provides full backup management:

### Dashboard
- Real-time status cards for all backup jobs
- Progress through every phase of a run — reading the source, finding deleted files, comparing with the mirror — with ETA and file counts, speed while copying
- Live countdown to next scheduled run (e.g. "in 2h 15m"), relative last-run time
- Start/Pause/Resume/Stop controls
- Click card → detail page, edit pencil → job editor

### Job Management
- Create, edit, delete backup jobs from the UI
- Kind of job: mirror (rsync) or snapshots (restic) — paths to back up, or another job to copy, retention, prune and check intervals; runs as you or as a system job (saving one installs it through pkexec)
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
- Jobs stored in `~/.config/backup-sync/jobs.json`, system jobs in `~/.config/backup-sync/system/jobs.json`
- Generates systemd service+timer units on save
- Dry run: rsync's file list, or what a restic job's next snapshot would add

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

## Snapshots (restic)

A job with `"engine": "restic"` keeps snapshots in a restic repository,
`<destination>/repo`, instead of mirroring. Every run is a point in time to go
back to; unchanged data is stored once, everything is compressed and encrypted,
and owners, modes and extended attributes are kept whatever file system the
repository sits on. The price: the backup is not a folder of plain files —
reading it takes restic and the password ([docs/restore.md](docs/restore.md)).
Mirrors suit big media that rarely change; snapshots suit systems, homes and
projects, where yesterday's version matters.

```jsonc
{ "id": "backup-system", "name": "System", "engine": "restic",
  "destination": "/mnt/nas/backup/omarchy",       // repo + .mirror-backup inside
  "exclude_file": "system.exclude",                            // restic patterns
  "restic": {
    "mode": "backup",                        // or "copy" (see below)
    "paths": ["/", "/boot", "/home"],        // each one on its own file system (--one-file-system)
    "host": "omarchy",                       // snapshots are taken, copied and thinned per host
    "password_file": "/etc/mirror-backup/keys/main.key",
    "keep": {"daily": 7, "weekly": 4, "monthly": 6},
    "prune_every_days": 7, "check_every_days": 7, "check_subset": "5%" },
  "schedule": {"type": "calendar", "expression": "*-*-* 12:00:00"} }
```

A run goes through these phases, each shown in the panels with its own progress:

| Phase | What happens |
|-------|--------------|
| `prepare` | the job's pre-command (if any), then `restic unlock` — only locks of runs that died |
| `snapshot` | `restic backup` of the listed paths (mode `backup`) |
| `copy` | `restic copy` of this host's snapshots from another job's repository (mode `copy`) |
| `forget` | retention: `restic forget --host … --keep-…`, grouped by host |
| `prune` | removes the data no snapshot needs any more — every `prune_every_days` days |
| `verify` | `restic check --read-data-subset` — every `check_every_days` days |

**Copy jobs** give a second, independent copy: `"mode": "copy"` with
`"from_job": "<a restic job>"` copies that job's snapshots into its own
repository (initialised with the same chunker parameters, so the copies
deduplicate the same way) and applies a retention of its own. `"run_after":
"<job>"` starts it right after that job succeeds; its own schedule catches up
when that did not happen.

**Safety rules** on top of those of all jobs: a repository is never created by
a run (`mirror-backup system init <job>` or `restic init` does that once — an
empty directory where a repository should be is a missing mount far more often
than a new job); every listed path must be on a mounted file system, or the
run is refused rather than snapshot an empty mount point; restic's exit 3
(some files unreadable) is a warning with the unreadable paths offered as
excludes, not a failure; a failed `verify` is a critical notification at once.

## System jobs

A user job can read only what its user can. A backup of the whole system has to
read every file and keep every owner — that takes root. Jobs with `"scope":
"system"` run as root, from system units, and are kept apart from the user's:

* **Edited** in `~/.config/backup-sync/system/` — `jobs.json`, the exclude files
  it names, and `mounts/`, systemd mount and automount units for destination
  disks (below `/mnt` or `/media`). It sits in the user's config dir, so the
  private overlay that carries the user jobs carries these too.
* **Installed** with `sudo /usr/local/lib/mirror-backup/mirror-backup system
  apply` (the app asks through pkexec when you save one): checked strictly,
  shown as a diff, copied to `/etc/mirror-backup`, turned into system units
  and the polkit rule. Root never reads its jobs from a file the user can
  write, and never runs a program the user can change — backup-sync, the app
  and the pre-commands it may run come from the root-owned copy in
  `/usr/local/lib/mirror-backup` (`install.sh --system`). A system job's
  `pre_command` can only name a program shipped there; today that is
  `mirror-backup-system-meta`, which writes down partition tables, LUKS
  headers, the btrfs layout and package lists for a bare-metal restore.
* **Controlled** by the user who applied them: a generated polkit rule lets
  that user, in an active local session, start, stop, pause and resume exactly
  these units — so the panels work for them as for any job — and their
  notifications go to that user's session.
* **Keys** never pass through the jobs: `sudo …/mirror-backup system set-key
  <name> < password` stores one as `/etc/mirror-backup/keys/<name>.key` (root,
  0600); a different existing key is only replaced with `--replace`, since the
  repositories created with it would no longer open.
* A job may list `"hosts"`: it gets units only there — the jobs file is shared
  between the systems of a dual boot, a system backup is not.

```bash
./install.sh --system                                   # once: the root-owned part
M=/usr/local/lib/mirror-backup/mirror-backup
op read … | sudo $M system set-key main                 # the repository password
sudo $M system apply                                    # install ~/.config/backup-sync/system
sudo $M system init backup-system                       # create the repository, once per job
mirror-backup control backup-system start               # or the panel's start button
```

At login, `mirror-backup sync-units` warns (and notifies) when the edited system
jobs differ from the installed ones. `mirror-backup-resume-system.service`
restarts interrupted system runs at boot, like its user counterpart does at login.

## Panel indicators

Both read `mirror-backup status --json`, so the rules for where a job's state
lives and when a run counts as alive exist once, in the app.

### GNOME Shell extension

- Panel icon with color-coded status (blue = running, yellow = paused, red = error, gray = queued, light blue = postponed)
- Per-job controls: Start, Stop, Pause/Resume
- Progress bar with the phase, ETA, file counts and speed
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
./install.sh --system           # also the root-owned part system jobs need (sudo)
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
./uninstall.sh            # --system: the root-owned part too; /etc/mirror-backup (keys!) stays
```

## Manual Commands

```bash
# Every job at a glance (--json: the snapshot the panels read)
mirror-backup status

# Bring the units in line with jobs.json after editing it by hand
mirror-backup sync-units

# Run, stop, pause, resume a job — of either scope (what the app and the panels call)
mirror-backup control backup-documents start
mirror-backup control backup-documents pause      # resume, stop

# What a restic job's next snapshot would add
mirror-backup dry-run backup-home

# restic on a system job's repository (snapshots, ls, mount, restore — docs/restore.md)
sudo /usr/local/lib/mirror-backup/mirror-backup system restic backup-system -- snapshots

# Check timer schedule
systemctl --user list-timers 'backup-*'

# View backup logs
cat /mnt/backup/Documents/.mirror-backup/backup.log
```

## Error Handling

- **Unmounted disks**: a destination that does not exist is never created — a missing destination is a missing mount far more often than a new job. Every mount point `fstab` lists on the way to the source or the destination must be mounted: an unmounted one is an empty directory, which as a destination would fill the system disk and as a source would make `--delete` empty the mirror. As a last line, a source that is empty while its mirror is not is refused unless deletion is off.
- **A scheduled run another machine already made** is skipped, and the log says which run covered it (see "Several machines, one set of jobs").

- **Network mounts**: a run whose source or destination sits on NFS, SMB, sshfs or a cloud FUSE mount first waits, up to 120 s (`BACKUP_SYNC_NETWORK_WAIT`), until the server answers — NFS on port 2049, SMB on 445, sshfs on 22, or the `port=` option of the mount; cloud mounts wait for NetworkManager to report a connection. Timers with `Persistent=true` start a missed run right after boot or wake, before the network is back. The server is found from `fstab` and the mount table, and the path is not looked up before the wait, so an `x-systemd.automount` entry is not triggered while the network is still down. That makes automount the recommended way to mount network shares on a system that does not wait for the network at boot (Omarchy masks `NetworkManager-wait-online`). A server still down after the wait fails the run with its name ("Source unreachable: … does not answer on port 2049").
- **Partial transfer tolerance**: rsync exit codes 23 (permission denied on some files) and 24 (files vanished during transfer) are treated as success with warnings, not failures. This prevents backup jobs from showing as "error" due to a single inaccessible file (e.g. Windows system files on NTFS mounts).
- **Default excludes**: new exclude files include `$RECYCLE.BIN`, `System Volume Information`, and `WpSystem` by default — common Windows system directories that are inaccessible or irrelevant on Linux.
- **Suspend**: while rsync runs, `backup-sync` holds a logind *delay* inhibitor. When a suspend is announced it stops rsync, lets the suspend proceed, and starts the run over after wake (state "Postponed" in between). This matters for FUSE sources and destinations (NTFS via ntfs-3g, rclone, sshfs): an rsync blocked in a FUSE request cannot be frozen — the freezer stops the FUSE daemon too — so the kernel would abort the suspend after 20 s. Stopping rsync *before* the freeze, while the FUSE daemon still answers, avoids that. A job the user paused stays paused across the restart. logind waits at most `InhibitDelayMaxSec` (5 s by default) for the inhibitor; rsync normally exits well within that, but a slow FUSE source can make raising it in `logind.conf` worthwhile.
- **Suspend and the readers**: reading a job's state reads the destination, so a status read stuck in a FUSE request at the freeze aborts the suspend just as rsync would. Every reader keeps out of the way: the desktop app and `mirror-backup status --watch` (the Omarchy widget) hold a delay inhibitor too, stop polling between two reads when a suspend is announced and pick up again after wake; a one-shot `mirror-backup status` holds the inhibitor only for its read, and once a suspend is under way it reads nothing and exits 75; the GNOME extension stops polling on the announcement and treats exit 75 as "skipped", not as an error. Without logind every reader simply polls as before.
- **Shutdown / logout**: a stop that arrives while the user's service manager or the system is going down postpones the run instead of failing it — a `<job>.deferred` marker is left in the status directory, and `mirror-backup-resume.service` starts the job again at the next boot or login. Jobs waiting in the queue are postponed the same way.
- **Crashes, power loss, the other OS**: at the next start, `mirror-backup-resume.service` also restarts any job whose status still says running, scanning, paused or queued but was written in another boot — of this machine or another one.
- **Manual stop**: stopping a job yourself (Stop button, `systemctl --user stop`) is a real interruption — it is marked "error" with "Backup interrupted" and not restarted.
- **Atomic status writes**: status JSON is written to a temp file and renamed into place (`mv -f`), preventing UI flickering caused by readers seeing a partially written or truncated file.

## How It Works

1. **Scheduling**: systemd timers trigger `backup-sync` at configured intervals
2. **Queue**: `flock` ensures only one backup runs at a time; others wait in "queued" state
3. **Sync**: `backup-sync` runs rsync with configurable options and tracks progress (see [Progress](#progress))
4. **Status**: Progress written to `<destination>/.mirror-backup/status.json`, with the host and boot that wrote it
5. **History**: Each completed run appends to `<destination>/.mirror-backup/history.jsonl`
6. **Panels**: The GNOME extension polls `mirror-backup status --json` (3 s while active, 15 s idle); the Omarchy widget keeps `mirror-backup status --watch` running
7. **Desktop app**: Polls at 1 second + inotify for instant updates
8. **Signals**: SIGUSR1 pauses rsync (SIGSTOP), SIGUSR2 resumes (SIGCONT); SIGTERM stops it — or postpones the run when the system is going down
9. **Suspend guard**: logind delay inhibitor + `PrepareForSleep` watch while rsync runs; stop before suspend, rerun after wake. The status readers (app, `status --watch`, `status`, the GNOME extension) pause the same way (`services/sleep_guard.py`)
10. **Archive**: Deleted/changed files kept in `.archive/` with configurable retention
11. **Resume**: `mirror-backup-resume.service` (once per user-manager start) runs `mirror-backup sync-units` and `mirror-backup resume`, which restarts postponed and interrupted runs
12. **Notifications**: Desktop notifications on start, finish, pause, resume, suspend, and errors

## Progress

rsync's own progress line counts bytes copied against the size of everything,
so an incremental run of a big mirror sits at 0 % from start to finish. On a
slow destination (NFS) most of the time goes into walking the trees, not into
copying. `backup-sync` therefore follows a run through its phases:

| Phase | What rsync does | Counted from | Total |
|-------|-----------------|--------------|-------|
| `listing` | walks the source | `--info=flist2` (`N files...`) | the previous successful run's file list (estimate, shown as `~`) |
| `deleting` | separate pass over the mirror's directories (`--delete-before`, `--delete-after`) | `--debug=del2` (`delete_in_dir(…)`) | the previous run's directory count (estimate); after the comparison, this run's own |
| `checking` | compares every entry, copies what changed | `-ii`: one itemize line per entry, unchanged ones too | this run's file list, exact |
| `pruning` | removes expired archives | — | — |

The state is `scanning` until rsync starts comparing and `running` from then
on. `status.json` carries `phase`, `phase_done`, `phase_total` and
`phase_estimated`; `progress` is the phase's percentage, and the ETA comes from
the phase's average pace. `mirror-backup status --json` adds the wording every
panel shows (`phase_label`, `phase_count`, `progress_text`), so the app, the
GNOME extension and the Omarchy widget say the same thing. Each history entry
records `files_total` and `dirs_total`, the next run's estimates.

An awk filter between rsync and `$XDG_RUNTIME_DIR/backup-sync/<job>.progress`
does the counting: one short snapshot line per message, so the count shown is
the one rsync stopped at even when it goes quiet on a slow mount.

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
<destination>/.mirror-backup/maintenance.json         # restic: when prune and check last ran
<destination>/repo/                                   # restic: the repository
~/.config/backup-sync/system/                         # system jobs as edited (jobs.json, excludes, mounts/)
/etc/mirror-backup/                                   # system jobs as installed, keys/, generated/
/usr/local/lib/mirror-backup/                         # root-owned backup-sync, mirror-backup, app/, pre-commands
/etc/systemd/system/backup-*.{service,timer}          # system units (generated)
/etc/polkit-1/rules.d/50-mirror-backup.rules          # who may control them (generated)
/etc/tmpfiles.d/mirror-backup.conf                    # /run/mirror-backup and the shared queue lock
/run/mirror-backup/                                   # queue.lock (both scopes), system jobs' run files
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
empty source, scheduled runs covered or due, the shared queue, system scope,
and restic against real throwaway repositories: snapshots, retention, copies,
a missing repository, an unmounted listed path, unreadable files, the
pre-command, the phases). `tests/test_restic_system.py` covers restic and
system units, status wording, job loading, `control` routing and `system
apply` (validation of what would run as root, the plan, the diff, keys).
Nothing touches real backups, the desktop, systemd or /etc.

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
