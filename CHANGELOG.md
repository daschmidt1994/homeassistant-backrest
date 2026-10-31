# Changelog

## 0.3.2 - 2026-10-31

- Fix remote (multihost) plans flipping to "overdue" once a day: Backrest re-syncs a
  peer operation with a new, higher ID whenever it changes (for example when retention
  forgets a week-old snapshot), and the integration took the operation with the highest
  ID as the latest backup. Plan operations are now ordered by start time.

## 0.3.1 - 2026-10-14

- Fix remote instances missing at random (and their devices flapping) when a Backrest
  server has more than one: Backrest drops queued peer states when the peer stream is
  requested without subscription. The integration now subscribes and reads until every
  configured peer has arrived, and skips the request entirely without peers.

## 0.3.0 - 2026-10-14

- Multihost: when Backrest is a multihost server, its client instances (for example VPS
  backed up via Backrest) appear as devices with *Connected*, *Last contact* and
  *Connection* entities, and their plans get the usual plan sensors (last backup, next
  backup, progress, failed, overdue). Remote plans are read-only: no start/cancel buttons.
- Failed or overdue remote plans turn on the server's *Problem* binary sensor
- Diagnostics list remote instances without key IDs or remote configs

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
