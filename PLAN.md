# Mirror Backup — Desktop App — Implementation Plan

## Architecture Philosophy
- **GNOME Shell extension** = primary daily interface (panel indicator, quick status, controls)
- **Desktop app** = configuration, detailed monitoring, job management, logs
- Both share the same backend: `backup-sync` script, systemd timers, JSON status files
- Extension is never broken or slimmed — it stays fully functional
- **Job queue**: backups run one at a time via flock, no random delay jitter

## Technology Stack
GTK4 + libadwaita with Python. Native GNOME look and feel.

## Phase 1 — Dashboard MVP (done)
- [x] Adw.Application with NavigationView
- [x] Dashboard with job cards (name, status dot, progress bar, speed/ETA)
- [x] Pause/Resume/Stop/Start controls
- [x] Next scheduled run time per job
- [x] Auto-discover jobs from existing systemd units
- [x] .desktop file and launcher

## Phase 2 — Job Management (done)
- [x] jobs.json config with first-run migration from existing units
- [x] Job editor form (source/dest pickers, schedule type, archive days, bandwidth limit)
- [x] Visual exclusion pattern editor
- [x] App generates systemd units on save

## Phase 2.5 — Job Queue (done)
- [x] flock-based serialization (one job at a time)
- [x] "Queued" state in extension and desktop app
- [x] Removed RandomizedDelaySec from all timers

## Phase 3 — History, Logs (done)
- [x] backup-sync writes history.jsonl after each run
- [x] Job detail page with run history + statistics
- [x] Log viewer with search/filter + auto-tail
- [ ] UDisks2 D-Bus mount detection (deferred to Phase 5)

## Phase 4 — Polish & Preferences (done)
- [x] AdwPreferencesWindow (notifications, job defaults, rsync defaults, log retention)
- [x] Advanced rsync options per job:
  - Delete mode (before/during/after/disabled)
  - Compression, checksum verification
  - Hard links, extended attributes, ACLs preservation
  - Partial transfer resume
  - Skip newer files on destination
  - Max/min file size filters
  - Bandwidth limiting
- [x] Rsync options exposed via environment variables in systemd services
- [x] Dry-run preview (rsync --dry-run from job editor)
- [x] Keyboard shortcuts (Ctrl+N, Ctrl+R, Ctrl+,, Ctrl+?)
- [x] App menu (hamburger) with Preferences, Shortcuts, About
- [x] Settings stored as JSON (~/.config/backup-sync/settings.json)
- [x] Live countdown timers (next run "in 2h 15m") in both app and extension
- [x] Visual schedule editor: weekday pills, interval spinner, ±30min time stepper, summary line
- [x] Extension "Open Mirror Backup" button to launch desktop app

## Phase 5 — Future
- Flatpak packaging
- D-Bus service for extension↔app communication
- Remote destinations (rsync over SSH)
- Multi-destination jobs
- UDisks2 mount detection for auto-trigger jobs
