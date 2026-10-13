# Changelog

## 0.2.1 - 2026-10-13

- Fix plan and repository entities disappearing after renaming one of their entities on
  Home Assistant 2026.10: devices are now linked to the server device by its registry ID
  instead of the deprecated `via_device` identifier
- README: entity IDs follow Home Assistant's configured language

## 0.2.0 - 2026-10-12

- New Activity sensor on the server device shows what Backrest is doing right now
  (backup, check, prune, forget old snapshots, restore, statistics, ...)
- New Current task, Last check and Last prune sensors per repository, including
  checks and prunes scheduled by Backrest itself
- The Problem binary sensor also turns on when a repository's last check failed
  (attribute `failed_checks`)
- Fix the diagnostics test that failed after the 0.1.1 version bump

## 0.1.1 - 2026-10-11

- Shorter German entity names so they are no longer cut off on phones
- "Backup running", "Last backup failed" and "Backup overdue" show Yes/No instead of
  "Not running"/"OK", with icons that change with the state
- Backup progress shows 0 % when no backup is running instead of unknown
- Last backup duration is shown in minutes for new installations
- Last backup attempt and per-plan running backups count moved to diagnostics

## 0.1.0 - 2026-10-10

First release.

- Config flow with connection test, HTTP/HTTPS, optional certificate verification and
  optional Backrest username/password; re-authentication, reconfiguration and options flow
- Automatic discovery of backup plans and repositories, including plans added or removed
  at runtime; stale devices are cleaned up
- Server, plan and repository sensors and binary sensors
- Start/cancel backup buttons and `backrest.start_backup` / `backrest.cancel_backup` actions
- Optional repository check/prune buttons (disabled by default)
- Real-time refresh via Backrest's operation event stream
- Diagnostics with redacted data
- English and German translations
- Tested against Backrest 1.6.2 and 1.14.1
