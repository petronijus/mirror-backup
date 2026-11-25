# Changelog

All notable changes to Mirror Backup. Format follows
[Keep a Changelog](https://keepachangelog.com/), versioning follows
[SemVer](https://semver.org/) (0.x = no stability promises).

## [Unreleased]
### Changed
- A run that meets disk errors (I/O error, read-only file system, corruption, no
  space, quota) now fails and names them. Files that are locked or unreadable to the
  user stay a warning.

### Fixed
- A job whose destination is on a read-only file system, or whose source the kernel
  switched to read-only, is shown as an error with the reason instead of its last
  good run; a run refuses a destination it cannot write to.

## [0.6.0] - 2026-11-23

The project is no longer GNOME-only: it is **Mirror Backup** now (repository
`petronijus/mirror-backup`, was `mirror-backup-gnome`). The GNOME Shell extension
keeps its name and UUID; the release tarball is `mirror-backup-vX.Y.Z.tar.gz`.

### Added
- **One set of jobs for several machines.** A job's state (status, history, log)
  lives in its destination, under `<destination>/.mirror-backup`, so every machine
  or OS that mounts it sees the same state. A scheduled run is skipped when another
  machine already covered that slot, and a run cut off by a crash or power loss is
  restarted at the next login.
- **`mirror-backup` command:** `status [--json|--watch]`, `sync-units`, `resume`
  and `migrate-legacy`. The panels read their data from it.
- **Omarchy support:** a bar widget, and the app follows the current Omarchy
  theme, including theme switches.
- **Backups survive suspend and shutdown.** A running rsync holds a sleep delay
  inhibitor, stops cleanly when a suspend is announced and starts over after wake;
  a run interrupted by shutdown is postponed (new "Postponed" state) and resumed at
  the next start. This keeps rsync stuck on a FUSE mount (NTFS, rclone, sshfs) from
  aborting a suspend.
- **Progress through every phase of a run:** listing the source, the delete pass,
  the comparison and pruning old archives, each with its own count and ETA. An
  incremental run of a big mirror no longer sits at "Scanning 0 %".
- **restic engine and system jobs:** a job can take restic snapshots instead of an
  rsync mirror, and system jobs run as root from a root-owned copy
  (`install.sh --system`, then `mirror-backup system apply`).
- The GNOME panel indicator follows the tray icon position of Ubuntu's
  AppIndicators extension.
- GNOME Shell 51 support.

### Changed
- Before a run, backup-sync waits up to 120 s for the servers behind network
  mounts (NFS, SMB, sshfs) to answer, so a missed run started right after boot or
  wake no longer fails as "not mounted". A server still down fails the run by name.
- Trash folders (`.Trash-<uid>`, `.Trash`, `$RECYCLE.BIN`) are never mirrored. A
  trash a mirror already holds is left in place and has to be removed by hand.

### Fixed
- The panel reads the user's own jobs from `jobs.json` instead of a hardcoded list
  of job names; `uninstall.sh` removes whatever job units exist.
- The archive no longer nests itself one level deeper on every run.
- Status reads no longer get in the way of a suspend: `status --watch` and the app
  pause while the system sleeps.
- A job's state is kept out of `--delete`, and safety checks were added: a missing
  destination is never created, mount points on the way to source and destination
  must be mounted, and an empty source is refused while its mirror is not.
- A job whose mirror held millions of trashed files failed with rsync's 1 GiB memory
  limit on the run that had to delete them; leaving trash out avoids such runs.
- The job detail page no longer crashes on history timestamps with an offset; a new
  job keeps the exclude file created in the editor.

## [0.5.1] - 2026-06-13
### Changed
- Renamed to **Mirror Backup for GNOME** (was "Backup Monitor") with the tagline
  "Scheduled rsync mirroring, watched live from your GNOME panel". Display names,
  README and repo renamed; technical identifiers (extension UUID, `backup-sync`
  config paths, desktop file id) are unchanged, so existing installs keep working.

## [0.5.0] - 2026-06-12

First public release.

### Changed
- `install.sh` no longer ships or auto-enables any backup jobs — a fresh
  install starts clean and you create jobs in the desktop app. (Previously the
  installer bundled the author's personal job units and enabled their timers.)
- Extension zip no longer bundles systemd unit templates; units are generated
  from `jobs.json` by the app.

### Added
- MIT LICENSE, changelog, CI release pipeline (tag → extension zip + source
  tarball on GitHub Releases)
- `config/example.exclude` — generic rsync exclude template

## [0.4.1] - 2026-05-16
### Fixed
- User schedule edits survive reinstall (existing `jobs.json` is authoritative;
  units regenerate from it after the app bundle is installed)

## [0.4.0] - 2026-05-09
### Added
- Persistent failure escalation, run-history rotation, suggested excludes

## [0.3.2] - 2026-05-09
### Fixed
- Daily job failing on a Windows-ACL'd file

## [0.3.1] - 2026-04-12
### Fixed
- UI flickering — status JSON now written atomically (temp file + rename)

## [0.3.0] - 2026-04-12
### Added
- Job queue (`flock` — one backup at a time, others wait as "queued")
- Default excludes for Windows system dirs on NTFS mounts
### Changed
- rsync exit codes 23/24 (partial transfer) treated as success with warnings

## [0.2.0] - 2026-03-31
### Added
- Build system (`build.sh`: extension zip + source tarball), self-setup

## [0.1.0] - 2026-03

Initial version: rsync wrapper with progress tracking, systemd user timers,
GNOME Shell panel indicator, GTK4/libadwaita desktop app.
