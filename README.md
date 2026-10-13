# Backrest for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/daschmidt1994/homeassistant-backrest/actions/workflows/validate.yml/badge.svg)](https://github.com/daschmidt1994/homeassistant-backrest/actions/workflows/validate.yml)
[![CI](https://github.com/daschmidt1994/homeassistant-backrest/actions/workflows/ci.yml/badge.svg)](https://github.com/daschmidt1994/homeassistant-backrest/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/daschmidt1994/homeassistant-backrest)](https://github.com/daschmidt1994/homeassistant-backrest/releases)

Monitor and control [Backrest](https://github.com/garethgeorge/backrest), the web UI and
orchestrator for [restic](https://restic.net/) backups, from Home Assistant.

The integration talks to Backrest's own API (Connect RPC, the same API the Backrest web UI
uses), discovers all backup plans and repositories automatically, keeps up with plans you
add or delete later and lets you start or cancel backups from Home Assistant.

> This is a community project and not affiliated with the Backrest project.

## Features

- Setup from the UI (config flow) with connection test, HTTP or HTTPS, optional
  certificate verification and optional username/password
- Several Backrest instances side by side
- Automatic discovery of backup plans and repositories, including plans created,
  re-created or deleted **while Home Assistant is running** (no restart)
- One device per Backrest server, per backup plan and per repository with stable unique IDs
- Sensors for status, last success, last attempt, next scheduled run, duration, size,
  data added, progress, 30-day success/failure counts, last error and more
- Live activity: what Backrest is doing right now (backup, check, prune, restore, ...)
  per server and per repository, plus when the last check and prune ran
- Binary sensors for reachability, running backups, failed backups, overdue plans
  and an overall "problem" flag
- Buttons and actions to start or cancel a backup, optional repository check/prune buttons
- Real-time updates through Backrest's operation event stream (with regular polling as
  the source of truth), configurable polling interval (default 60 s)
- Re-authentication and reconfiguration flows, diagnostics with redacted data
- English and German translations

## Requirements

| Component | Supported |
| --- | --- |
| Home Assistant | 2025.6.0 or newer (CI tests 2025.6 and 2026.10) |
| Backrest | 1.6.0 or newer recommended, tested with **1.6.2** and **1.14.1** |

Backrest versions before 1.6.0 lack the summary dashboard API. The integration detects
this and keeps working, but the 30-day counters and data-volume sensors are not created.
Versions before 1.6.0 have not been tested.

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant.
2. Open the menu (⋮) in the top right and choose **Custom repositories**.
3. Add `https://github.com/daschmidt1994/homeassistant-backrest` with type **Integration**.
4. Search for **Backrest** in HACS and download it.
5. Restart Home Assistant.

[![Open your Home Assistant instance and open a repository inside HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=daschmidt1994&repository=homeassistant-backrest&category=integration)

### Manual

Copy `custom_components/backrest` into the `custom_components` folder of your Home
Assistant configuration and restart Home Assistant.

## Configuration

Go to **Settings → Devices & services → Add integration → Backrest**.

| Field | Description |
| --- | --- |
| Host | Hostname or IP address of the Backrest server |
| Port | Port of the Backrest web UI, `9898` by default |
| Use HTTPS | Enable when Backrest is served via HTTPS (for example behind a reverse proxy) |
| Verify SSL certificate | Disable only for self-signed certificates |
| Username / Password | Only needed when authentication is enabled in Backrest |

By default Backrest only listens on `127.0.0.1`. To reach it from Home Assistant, start
Backrest with `--bind-address :9898` (or `BACKREST_PORT=0.0.0.0:9898`), or put it behind
a reverse proxy. Reverse proxies must forward the `/v1.Backrest/` and
`/v1.Authentication/` paths and allow long-running and streaming requests (see
[Known limitations](#known-limitations)). Serving Backrest under a sub-path is not
supported.

**Options** (Settings → Devices & services → Backrest → Configure):

| Option | Default | Description |
| --- | --- | --- |
| Update interval | 60 s | Polling interval, 15–3600 seconds |
| Real-time updates | on | Subscribe to Backrest's event stream to refresh as soon as a backup starts or ends |

Changed credentials can be entered via the re-authentication prompt that appears
automatically; a changed address via **Reconfigure**.

## Entities

Entity IDs below are what Home Assistant generates for a Backrest instance named
`homeserver`, a plan with the ID `nas` and a repository `offsite`, with Home Assistant
set to English. Home Assistant derives entity IDs from the entity names in its configured
language, so in German they read e.g. `sensor.backrest_nas_letztes_backup`. Entity IDs are
created once; renaming entities in a later version does not change existing IDs. If two devices would
produce the same entity ID (for example a plan named like the instance), Home Assistant
appends `_2`.

### Backrest server (`Backrest homeserver`)

| Entity | Example entity ID | Notes |
| --- | --- | --- |
| Reachable | `binary_sensor.backrest_homeserver_reachable` | `off` when the last request failed; never unavailable |
| Problem | `binary_sensor.backrest_homeserver_problem` | `on` if unreachable, a plan's last backup failed, a plan is overdue or a repository's last check failed; attributes `failed_plans`, `overdue_plans`, `failed_checks` |
| Backup running | `binary_sensor.backrest_homeserver_backup_running` | Any plan |
| Activity | `sensor.backrest_homeserver_activity` | What Backrest is doing right now: `idle`, `backup`, `check`, `prune`, `forget`, `restore`, `stats`, `index`, `hook`, `command`; attribute `operations` lists all running tasks |
| Backup plans | `sensor.backrest_homeserver_backup_plans` | Attribute `plans` |
| Repositories | `sensor.backrest_homeserver_repositories` | |
| Running backups | `sensor.backrest_homeserver_running_backups` | |
| Plans with successful last backup | `sensor.backrest_homeserver_plans_with_successful_last_backup` | Success or warning |
| Plans with failed last backup | `sensor.backrest_homeserver_plans_with_failed_last_backup` | |
| Plans without successful backup | `sensor.backrest_homeserver_plans_without_successful_backup` | See [How values are determined](#how-values-are-determined) |
| Last successful backup | `sensor.backrest_homeserver_last_successful_backup` | Across all plans, attribute `plan` |

### Backup plan (`Backrest nas`)

| Entity | Example entity ID | Notes |
| --- | --- | --- |
| Start backup | `button.backrest_nas_start_backup` | |
| Cancel backup | `button.backrest_nas_cancel_backup` | |
| Backup running | `binary_sensor.backrest_nas_backup_running` | |
| Last backup failed | `binary_sensor.backrest_nas_last_backup_failed` | Attribute `error_message` |
| Backup overdue | `binary_sensor.backrest_nas_backup_overdue` | Attribute `last_success` |
| Last backup status | `sensor.backrest_nas_last_backup_status` | `success`, `warning`, `error`, `in_progress`, `system_cancelled`, `user_cancelled` |
| Last successful backup | `sensor.backrest_nas_last_successful_backup` | Timestamp, attribute `snapshot_id` |
| Last backup attempt | `sensor.backrest_nas_last_backup_attempt` | Start time of the latest run; diagnostic |
| Next backup | `sensor.backrest_nas_next_backup` | Next scheduled run, unknown if the schedule is disabled |
| Last backup duration | `sensor.backrest_nas_last_backup_duration` | Shown in minutes |
| Backup progress | `sensor.backrest_nas_backup_progress` | Percent while running, `0` when idle |
| Running backups | `sensor.backrest_nas_running_backups` | Diagnostic, attribute `queued` |
| Successful backups (30 days) | `sensor.backrest_nas_successful_backups_30_days` | Includes warnings (attribute `with_warnings`); Backrest ≥ 1.6 |
| Failed backups (30 days) | `sensor.backrest_nas_failed_backups_30_days` | Backrest ≥ 1.6 |
| Last backup size | `sensor.backrest_nas_last_backup_size` | Bytes processed by the last successful backup |
| Last backup data added | `sensor.backrest_nas_last_backup_data_added` | New data stored by the last successful backup |
| Last backup files | `sensor.backrest_nas_last_backup_files` | Disabled by default |
| Last status change | `sensor.backrest_nas_last_status_change` | Diagnostic |
| Last error message | `sensor.backrest_nas_last_error_message` | Diagnostic, attribute `occurred_at` |

### Repository (`Backrest repository offsite`)

| Entity | Example entity ID | Notes |
| --- | --- | --- |
| Backup plans | `sensor.backrest_repository_offsite_backup_plans` | |
| Current task | `sensor.backrest_repository_offsite_current_task` | Task running on the repository, same values as Activity |
| Last check | `sensor.backrest_repository_offsite_last_check` | When the last check finished; attributes `status`, `error_message` |
| Last prune | `sensor.backrest_repository_offsite_last_prune` | When the last prune finished; attributes `status`, `error_message` |
| Successful backups (30 days) | `sensor.backrest_repository_offsite_successful_backups_30_days` | Backrest ≥ 1.6 |
| Failed backups (30 days) | `sensor.backrest_repository_offsite_failed_backups_30_days` | Backrest ≥ 1.6 |
| Data added (30 days) | `sensor.backrest_repository_offsite_data_added_30_days` | Backrest ≥ 1.6 |
| Protected data | `sensor.backrest_repository_offsite_protected_data` | Size of the latest good backup; Backrest ≥ 1.14 |
| Check repository | `button.backrest_repository_offsite_check_repository` | Disabled by default |
| Prune repository | `button.backrest_repository_offsite_prune_repository` | Disabled by default |

### Actions

| Action | Fields | Description |
| --- | --- | --- |
| `backrest.start_backup` | `config_entry_id`, `plan_id` | Start a backup of a plan |
| `backrest.cancel_backup` | `config_entry_id`, `plan_id` | Request cancellation of the running backup of a plan |

```yaml
action: backrest.start_backup
data:
  config_entry_id: 01JABCDEF0123456789ABCDEFG  # pick the instance in the UI editor
  plan_id: nas
```

## How values are determined

- **Plans and repositories** come from `GetConfig` on every refresh. New plans get their
  entities on the next refresh; deleted plans and repositories are removed together with
  their device. Plan IDs cannot be renamed in Backrest, so unique IDs stay stable. A plan
  that is deleted and re-created with the same ID gets the same entity IDs again.
- **Backup runs** come from `GetOperations` (the 50 most recent operations of each plan).
  New runs update the existing entities; they never create new ones. Dry runs are ignored.
- **30-day counters, next run fallback and data volumes** come from
  `GetSummaryDashboard` (Backrest ≥ 1.6).
- **Last successful backup** uses the recent operations and falls back to the 30-day
  summary. If neither contains a success, the value is unknown and the plan counts as
  "without successful backup". A plan whose last success is older than 30 days *and*
  more than ~50 operations back is therefore reported as without success.
- **Overdue** uses the same rule as the Backrest dashboard: the schedule's nominal period
  (cron, every N hours or every N days) × 1.25 has passed since the last good backup
  (success or warning). Plans with a disabled schedule are never overdue; plans that never
  had a good backup are not overdue (like in Backrest), but show up in "Last backup
  failed" or "Plans without successful backup".
- **Last status change** is the time the result of the plan's finished backups last
  changed (for example from `success` to `error`). It is unknown if the current status
  goes back further than the fetched history.
- A **started backup** is never reported as successful. Pressing *Start backup* only
  means that Backrest accepted the request; the status sensors switch to `in_progress`
  and later to the real result reported by Backrest.

## Automation examples

These examples live in [`examples/automations.yaml`](examples/automations.yaml) and are
run against the integration in the test suite (`tests/test_automation_examples.py`).
Replace `homeserver` and `nas` with your instance name and plan ID, and `notify.notify`
with your notification action.

### Backup failed

```yaml
- id: backrest_backup_failed
  alias: "Backrest: backup failed"
  triggers:
    - trigger: state
      entity_id: binary_sensor.backrest_nas_last_backup_failed
      from: "off"
      to: "on"
  actions:
    - action: notify.notify
      data:
        title: "Backup failed"
        message: >-
          Backup plan "nas" failed:
          {{ trigger.to_state.attributes.error_message or 'no error message' }}
```

### Backup overdue

Based on the plan's schedule:

```yaml
- id: backrest_backup_overdue
  alias: "Backrest: backup overdue"
  triggers:
    - trigger: state
      entity_id: binary_sensor.backrest_nas_backup_overdue
      from: "off"
      to: "on"
  actions:
    - action: notify.notify
      data:
        title: "Backup overdue"
        message: >-
          No successful backup of "nas" since
          {{ as_timestamp(trigger.to_state.attributes.last_success, none)
             | timestamp_custom('%Y-%m-%d %H:%M', default='never') }}.
```

With your own threshold:

```yaml
- id: backrest_backup_older_than_48h
  alias: "Backrest: no backup for 48 hours"
  triggers:
    - trigger: template
      value_template: >-
        {% set last = states('sensor.backrest_nas_last_successful_backup') | as_datetime(none) %}
        {{ last is not none and now() - last > timedelta(hours=48) }}
  actions:
    - action: notify.notify
      data:
        title: "Backup older than 48 hours"
        message: The last successful backup of "nas" is older than 48 hours.
```

### Start a backup

```yaml
- id: backrest_start_backup_nightly
  alias: "Backrest: start NAS backup at night"
  triggers:
    - trigger: time
      at: "22:00:00"
  conditions:
    - condition: state
      entity_id: binary_sensor.backrest_nas_backup_running
      state: "off"
  actions:
    - action: button.press
      target:
        entity_id: button.backrest_nas_start_backup
```

### Backrest offline

```yaml
- id: backrest_offline
  alias: "Backrest: server offline"
  triggers:
    - trigger: state
      entity_id: binary_sensor.backrest_homeserver_reachable
      to: "off"
      for: "00:05:00"
  actions:
    - action: notify.notify
      data:
        title: "Backrest offline"
        message: The Backrest server "homeserver" is not reachable.
```

## Screenshots

No screenshots yet. Contributions are welcome.

## Troubleshooting

| Problem | Solution |
| --- | --- |
| *Failed to connect* | Check host, port and HTTP/HTTPS. Backrest listens on `127.0.0.1` only by default, see [Configuration](#configuration). |
| *The server did not respond like Backrest* | Host/port point to another service, or a reverse proxy rewrites the API paths. |
| *This Backrest instance requires a username and password* | Authentication is enabled in Backrest; enter a Backrest user. |
| *SSL certificate could not be verified* | Self-signed certificate: disable *Verify SSL certificate*. |
| Entities are unavailable | Backrest is unreachable; `binary_sensor.backrest_<instance>_reachable` is `off`. The integration retries on every polling interval. |
| A new plan does not show up | It appears on the next refresh (polling interval). Check that it exists in Backrest's config. |
| Start backup says a backup is already running | Backrest has a running or queued backup for the plan, or a previous start request is still waiting for Backrest. |

**Debug logging**

```yaml
logger:
  logs:
    custom_components.backrest: debug
```

**Diagnostics**: Settings → Devices & services → Backrest → ⋮ → *Download diagnostics*.
The file contains the integration version, reachability, the last successful refresh, the
detected API features, the last error and the derived state of every plan. Host,
username, password and the instance key are redacted; repository URIs, passwords, paths
and hook settings are never stored by the integration in the first place.

## Known limitations

- **No version sensor.** Backrest does not expose its version through the API (it is only
  compiled into the web UI bundle), so the integration does not report it.
- **Start backup waits for completion server-side.** Backrest's `Backup` call only
  returns once the backup has finished. The integration sends the request in the
  background and treats it as accepted when Backrest has not rejected it within 10 seconds.
  If Home Assistant restarts or the connection drops, the backup keeps running in Backrest;
  only the waiting request is dropped. Reverse proxies with short read timeouts may close
  that request; this does not stop the backup either.
- **Cancel is best effort.** Backrest does not guarantee that a cancellation succeeds.
- **Dry runs** and restore operations are not offered.
- **Event stream behind proxies:** proxies that buffer responses delay or break the event
  stream. Updates then arrive with the polling interval; disable *Real-time updates* if
  your proxy logs errors.
- **Multihost sync:** only operations of the local instance are evaluated; plans of
  synced peer instances are not shown.
- Authentication uses Backrest's username/password login (JWT). Backrest offers no API keys.
- Backrest returns repository passwords in `GetConfig` to every authenticated client. The
  integration discards them immediately, but the Backrest user you configure can read them.

## Backrest API usage

All calls are Connect RPC requests (`POST /<service>/<method>`, JSON body) as defined in
[`proto/v1/service.proto`](https://github.com/garethgeorge/backrest/blob/main/proto/v1/service.proto):

| Call | Used for |
| --- | --- |
| `v1.Authentication/Login` | Exchange username/password for a JWT (only if Backrest answers 401) |
| `v1.Backrest/GetConfig` | Plans, repositories, instance name |
| `v1.Backrest/GetOperations` | Recent operations per plan |
| `v1.Backrest/GetSummaryDashboard` | 30-day statistics (Backrest ≥ 1.6) |
| `v1.Backrest/GetOperationEvents` | Server stream that triggers immediate refreshes |
| `v1.Backrest/Backup` | Start backup button / action |
| `v1.Backrest/Cancel` | Cancel backup button / action |
| `v1.Backrest/DoRepoTask` | Check / prune repository buttons |

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt -r requirements_lint.txt
ruff check . && ruff format --check .
pytest tests --cov
```

`tests/test_live.py` runs the whole integration against a real Backrest server and is
skipped unless `BACKREST_LIVE_HOST` is set (see the module docstring).

## License

[MIT](LICENSE)
