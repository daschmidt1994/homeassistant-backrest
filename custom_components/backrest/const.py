"""Constants for the Backrest integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "backrest"

CONF_EVENT_STREAM: Final = "event_stream"

DEFAULT_PORT: Final = 9898
DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 15
MAX_SCAN_INTERVAL: Final = 3600
DEFAULT_EVENT_STREAM: Final = True

# Number of most recent operations fetched per plan on every refresh. Backup
# flows also contain forget/index/hook operations, so this covers roughly the
# last 10-20 backup runs of a plan.
OPERATIONS_PER_PLAN: Final = 50
# Upper bound of concurrent GetOperations requests during a refresh.
MAX_PARALLEL_REQUESTS: Final = 4

REQUEST_TIMEOUT: Final = 30
# How long a button press waits for Backrest to reject a start request before
# the task is considered accepted. Backrest only answers the Backup RPC once
# the backup has finished, so a successful start never returns quickly.
START_CONFIRM_TIMEOUT: Final = 10
# Backrest sends a keep-alive on the event stream every 60 seconds.
EVENT_STREAM_READ_TIMEOUT: Final = 150
EVENT_STREAM_MIN_BACKOFF: Final = 5
EVENT_STREAM_MAX_BACKOFF: Final = 300

# Backrest flags a plan as overdue once the gap since its last good backup
# exceeds the schedule's nominal period by this factor (see
# internal/api/summarydashboard.go in the Backrest sources).
OVERDUE_GRACE_FACTOR: Final = 1.25

SERVICE_START_BACKUP: Final = "start_backup"
SERVICE_CANCEL_BACKUP: Final = "cancel_backup"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_PLAN_ID: Final = "plan_id"

SCAN_INTERVAL: Final = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
