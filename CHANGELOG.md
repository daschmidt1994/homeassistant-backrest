# Changelog

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
