"""Data model for the Backrest integration.

Raw API payloads are parsed into small immutable dataclasses right after they
are received. Only the fields the integration needs are copied, which keeps
secrets contained in the Backrest configuration (repository passwords, hook
tokens, ...) out of Home Assistant's memory, logs and diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from typing import Any

from cronsim import CronSim, CronSimError

from .const import OVERDUE_GRACE_FACTOR

_LOGGER = logging.getLogger(__name__)

# Number of upcoming cron runs sampled to determine a schedule's period,
# mirroring Backrest's protoutil.NominalPeriod.
_CRON_PERIOD_SAMPLES = 10
_MAX_MESSAGE_LENGTH = 255


class OperationStatus(StrEnum):
    """Status of a Backrest operation."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    SYSTEM_CANCELLED = "system_cancelled"
    USER_CANCELLED = "user_cancelled"


_STATUS_MAP = {
    "STATUS_UNKNOWN": OperationStatus.UNKNOWN,
    "STATUS_PENDING": OperationStatus.PENDING,
    "STATUS_INPROGRESS": OperationStatus.IN_PROGRESS,
    "STATUS_SUCCESS": OperationStatus.SUCCESS,
    "STATUS_WARNING": OperationStatus.WARNING,
    "STATUS_ERROR": OperationStatus.ERROR,
    "STATUS_SYSTEM_CANCELLED": OperationStatus.SYSTEM_CANCELLED,
    "STATUS_USER_CANCELLED": OperationStatus.USER_CANCELLED,
}
# protojson may also emit enum values as numbers.
_STATUS_NUMBERS = {
    0: OperationStatus.UNKNOWN,
    1: OperationStatus.PENDING,
    2: OperationStatus.IN_PROGRESS,
    3: OperationStatus.SUCCESS,
    4: OperationStatus.ERROR,
    5: OperationStatus.SYSTEM_CANCELLED,
    6: OperationStatus.USER_CANCELLED,
    7: OperationStatus.WARNING,
}

FINISHED_STATUSES = frozenset(
    {
        OperationStatus.SUCCESS,
        OperationStatus.WARNING,
        OperationStatus.ERROR,
        OperationStatus.SYSTEM_CANCELLED,
        OperationStatus.USER_CANCELLED,
    }
)
GOOD_STATUSES = frozenset({OperationStatus.SUCCESS, OperationStatus.WARNING})
# Values of the "last backup status" sensor.
LAST_STATUS_OPTIONS = [
    OperationStatus.SUCCESS.value,
    OperationStatus.WARNING.value,
    OperationStatus.ERROR.value,
    OperationStatus.IN_PROGRESS.value,
    OperationStatus.SYSTEM_CANCELLED.value,
    OperationStatus.USER_CANCELLED.value,
]


def parse_status(value: Any) -> OperationStatus:
    """Parse an OperationStatus enum value."""
    if isinstance(value, str):
        return _STATUS_MAP.get(value, OperationStatus.UNKNOWN)
    if isinstance(value, int) and not isinstance(value, bool):
        return _STATUS_NUMBERS.get(value, OperationStatus.UNKNOWN)
    return OperationStatus.UNKNOWN


def _int(value: Any) -> int | None:
    """Parse a protojson int64 (encoded as string) or int32."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ms_to_datetime(value: Any) -> datetime | None:
    millis = _int(value)
    if not millis or millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _truncate(message: str) -> str:
    message = " ".join(message.split())
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[: _MAX_MESSAGE_LENGTH - 1] + "…"


@dataclass(frozen=True, slots=True)
class Schedule:
    """Backup schedule of a plan."""

    kind: str  # "disabled", "cron", "days", "hours" or "unknown"
    cron: str | None = None
    interval: timedelta | None = None

    @classmethod
    def from_api(cls, data: Any) -> Schedule:
        """Parse a Schedule message."""
        data = _dict(data)
        if not data or data.get("disabled") is True:
            return cls("disabled")
        if cron := _str(data.get("cron")).strip():
            return cls("cron", cron=cron)
        if (days := _int(data.get("maxFrequencyDays"))) and days > 0:
            return cls("days", interval=timedelta(days=days))
        if (hours := _int(data.get("maxFrequencyHours"))) and hours > 0:
            return cls("hours", interval=timedelta(hours=hours))
        return cls("unknown")

    @property
    def enabled(self) -> bool:
        """Return whether the schedule triggers backups."""
        return self.kind in ("cron", "days", "hours")

    def nominal_period(self, now: datetime) -> timedelta | None:
        """Return the longest expected gap between two scheduled runs."""
        if self.interval is not None:
            return self.interval
        if self.cron is None:
            return None
        try:
            iterator = CronSim(self.cron, now.replace(second=0, microsecond=0))
            previous = next(iterator)
            longest = timedelta(0)
            for _ in range(_CRON_PERIOD_SAMPLES):
                current = next(iterator)
                longest = max(longest, current - previous)
                previous = current
        except (CronSimError, StopIteration, ValueError, TypeError):
            _LOGGER.debug("Cannot determine the period of cron %r", self.cron)
            return None
        return longest or None


@dataclass(frozen=True, slots=True)
class Plan:
    """A Backrest backup plan."""

    id: str
    repo_id: str
    schedule: Schedule
    path_count: int
    # Instance ID of a remote (multihost client) instance, empty for local plans.
    instance: str = ""

    @property
    def is_remote(self) -> bool:
        """Return whether the plan belongs to a remote instance."""
        return bool(self.instance)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Plan | None:
        """Parse a Plan message. Returns None for invalid entries."""
        plan_id = _str(data.get("id")).strip()
        if not plan_id:
            return None
        paths = data.get("paths")
        return cls(
            id=plan_id,
            repo_id=_str(data.get("repo")),
            schedule=Schedule.from_api(data.get("schedule")),
            path_count=len(paths) if isinstance(paths, list) else 0,
        )


REMOTE_PLAN_SEPARATOR = "/"


def remote_plan_key(instance: str, plan_id: str) -> str:
    """Return the key of a remote plan in BackrestData.plans."""
    return f"{instance}{REMOTE_PLAN_SEPARATOR}{plan_id}"


class PeerConnection(StrEnum):
    """Multihost connection state of a remote instance."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RETRY_WAIT = "retry_wait"
    ERROR_AUTH = "error_auth"
    ERROR_PROTOCOL = "error_protocol"
    ERROR_INTERNAL = "error_internal"


@dataclass(frozen=True, slots=True)
class RemoteInstance:
    """A Backrest client instance synced to this server (multihost)."""

    instance_id: str
    key_id: str
    connection: PeerConnection
    status_message: str
    last_heartbeat: datetime | None
    plans: tuple[Plan, ...]

    @property
    def connected(self) -> bool:
        """Return whether the instance is currently connected."""
        return self.connection is PeerConnection.CONNECTED

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> RemoteInstance | None:
        """Parse a PeerState message.

        Only plans are taken from the remote config: its repositories carry
        plaintext passwords and are ignored.
        """
        instance_id = _str(data.get("peerInstanceId")).strip()
        if not instance_id or REMOTE_PLAN_SEPARATOR in instance_id:
            return None
        raw_state = _str(data.get("state")).removeprefix("CONNECTION_STATE_").lower()
        try:
            connection = PeerConnection(raw_state)
        except ValueError:
            connection = PeerConnection.UNKNOWN
        plans: dict[str, Plan] = {}
        raw_plans = _dict(data.get("remoteConfig")).get("plans")
        for raw in raw_plans if isinstance(raw_plans, list) else []:
            if isinstance(raw, dict) and (plan := Plan.from_api(raw)):
                plans[plan.id] = replace(plan, instance=instance_id)
        # Without config permission only the plan IDs are known.
        known = data.get("knownPlans")
        for raw in known if isinstance(known, list) else []:
            plan_id = _str(_dict(raw).get("id")).strip()
            if plan_id and plan_id not in plans:
                plans[plan_id] = Plan(plan_id, "", Schedule("unknown"), 0, instance_id)
        return cls(
            instance_id=instance_id,
            key_id=_str(data.get("peerKeyid")),
            connection=connection,
            status_message=_truncate(_str(data.get("statusMessage"))),
            last_heartbeat=_ms_to_datetime(data.get("lastHeartbeatMillis")),
            plans=tuple(plans[key] for key in sorted(plans)),
        )


@dataclass(frozen=True, slots=True)
class Repo:
    """A Backrest repository (credentials are deliberately not kept)."""

    id: str
    guid: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Repo | None:
        """Parse a Repo message. Returns None for invalid entries."""
        repo_id = _str(data.get("id")).strip()
        if not repo_id:
            return None
        return cls(id=repo_id, guid=_str(data.get("guid")))


@dataclass(frozen=True, slots=True)
class BackrestConfig:
    """The parts of the Backrest configuration used by the integration."""

    instance: str
    identity_key_id: str | None
    plans: dict[str, Plan]
    repos: dict[str, Repo]
    auth_enabled: bool
    # Number of multihost peers (authorized clients and known hosts).
    peer_count: int = 0

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> BackrestConfig:
        """Parse a Config message."""
        plans: dict[str, Plan] = {}
        raw_plans = data.get("plans")
        for raw in raw_plans if isinstance(raw_plans, list) else []:
            if isinstance(raw, dict) and (plan := Plan.from_api(raw)):
                plans[plan.id] = plan
        repos: dict[str, Repo] = {}
        raw_repos = data.get("repos")
        for raw in raw_repos if isinstance(raw_repos, list) else []:
            if isinstance(raw, dict) and (repo := Repo.from_api(raw)):
                repos[repo.id] = repo
        sync = _dict(data.get("sync"))
        identity = _dict(sync.get("identity"))
        peer_keys: set[str] = set()
        for field_name in ("authorizedClients", "knownHosts"):
            peers = sync.get(field_name)
            for peer in peers if isinstance(peers, list) else []:
                if key := _str(_dict(peer).get("keyId")) or _str(_dict(peer).get("instanceId")):
                    peer_keys.add(key)
        auth = _dict(data.get("auth"))
        return cls(
            instance=_str(data.get("instance")).strip(),
            identity_key_id=_str(identity.get("keyId")) or None,
            plans=plans,
            repos=repos,
            auth_enabled=bool(auth) and auth.get("disabled") is not True,
            peer_count=len(peer_keys),
        )

    @property
    def unique_id(self) -> str | None:
        """Return a stable identifier of the Backrest installation."""
        return self.identity_key_id or self.instance or None


@dataclass(frozen=True, slots=True)
class ChartEntry:
    """One backup of the summary dashboard chart."""

    start: datetime
    duration: timedelta
    status: OperationStatus


@dataclass(frozen=True, slots=True)
class Summary:
    """Summary dashboard numbers of a plan or repository (last 30 days)."""

    id: str
    success_30d: int
    warning_30d: int
    failed_30d: int
    bytes_added_30d: int | None
    bytes_scanned_30d: int | None
    next_backup: datetime | None
    protected_bytes: int | None
    recent_backups: tuple[ChartEntry, ...]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Summary | None:
        """Parse a SummaryDashboardResponse.Summary message."""
        summary_id = _str(data.get("id"))
        if not summary_id:
            return None
        chart = _dict(data.get("recentBackups"))
        starts = chart.get("timestampMs")
        durations = chart.get("durationMs")
        statuses = chart.get("status")
        entries: list[ChartEntry] = []
        if isinstance(starts, list) and isinstance(durations, list) and isinstance(statuses, list):
            for start_ms, duration_ms, status in zip(starts, durations, statuses, strict=False):
                start = _ms_to_datetime(start_ms)
                duration = _int(duration_ms)
                if start is None or duration is None:
                    continue
                entries.append(
                    ChartEntry(start, timedelta(milliseconds=duration), parse_status(status))
                )
        return cls(
            id=summary_id,
            success_30d=_int(data.get("backupsSuccessLast30days")) or 0,
            warning_30d=_int(data.get("backupsWarningLast30days")) or 0,
            failed_30d=_int(data.get("backupsFailed30days")) or 0,
            bytes_added_30d=_int(data.get("bytesAddedLast30days")) or 0,
            bytes_scanned_30d=_int(data.get("bytesScannedLast30days")) or 0,
            next_backup=_ms_to_datetime(data.get("nextBackupTimeMs")),
            protected_bytes=_int(data.get("protectedBytes")) or None,
            recent_backups=tuple(entries),
        )


@dataclass(frozen=True, slots=True)
class SummaryDashboard:
    """Parsed GetSummaryDashboard response."""

    plans: dict[str, Summary]
    repos: dict[str, Summary]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SummaryDashboard:
        """Parse a SummaryDashboardResponse (paths are intentionally dropped)."""

        def _parse(raw: Any) -> dict[str, Summary]:
            result: dict[str, Summary] = {}
            for item in raw if isinstance(raw, list) else []:
                if isinstance(item, dict) and (summary := Summary.from_api(item)):
                    result[summary.id] = summary
            return result

        return cls(
            plans=_parse(data.get("planSummaries")),
            repos=_parse(data.get("repoSummaries")),
        )


@dataclass(frozen=True, slots=True)
class BackupOperation:
    """A backup operation (one execution of a plan)."""

    id: int
    flow_id: int | None
    status: OperationStatus
    start: datetime | None
    end: datetime | None
    message: str
    snapshot_id: str | None
    dry_run: bool
    bytes_processed: int | None
    bytes_added: int | None
    files_processed: int | None
    percent_done: float | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> BackupOperation | None:
        """Parse an Operation message. Returns None for non-backup operations."""
        backup = data.get("operationBackup")
        op_id = _int(data.get("id"))
        if not isinstance(backup, dict) or op_id is None:
            return None
        last_status = _dict(backup.get("lastStatus"))
        summary = _dict(last_status.get("summary"))
        progress = _dict(last_status.get("status"))
        percent = _float(progress.get("percentDone"))
        return cls(
            id=op_id,
            flow_id=_int(data.get("flowId")),
            status=parse_status(data.get("status")),
            start=_ms_to_datetime(data.get("unixTimeStartMs")),
            end=_ms_to_datetime(data.get("unixTimeEndMs")),
            message=_truncate(_str(data.get("displayMessage"))),
            snapshot_id=_str(data.get("snapshotId")) or None,
            dry_run=backup.get("dryRun") is True,
            bytes_processed=_int(summary.get("totalBytesProcessed")),
            bytes_added=_int(summary.get("dataAdded")),
            files_processed=_int(summary.get("totalFilesProcessed")),
            percent_done=max(0.0, min(percent * 100, 100.0)) if percent is not None else None,
        )

    @property
    def finished(self) -> bool:
        """Return whether the operation has completed."""
        return self.status in FINISHED_STATUSES

    @property
    def duration(self) -> timedelta | None:
        """Return the runtime of a finished operation."""
        if self.start is None or self.end is None or self.end < self.start:
            return None
        return self.end - self.start

    @property
    def completed_at(self) -> datetime | None:
        """Return when the operation finished (or started, if unknown)."""
        return self.end or self.start


class OperationKind(StrEnum):
    """Type of a Backrest operation."""

    BACKUP = "backup"
    CHECK = "check"
    PRUNE = "prune"
    FORGET = "forget"
    RESTORE = "restore"
    STATS = "stats"
    INDEX = "index"
    HOOK = "hook"
    COMMAND = "command"


_OPERATION_KINDS = {
    "operationBackup": OperationKind.BACKUP,
    "operationCheck": OperationKind.CHECK,
    "operationPrune": OperationKind.PRUNE,
    "operationForget": OperationKind.FORGET,
    "operationRestore": OperationKind.RESTORE,
    "operationStats": OperationKind.STATS,
    "operationIndexSnapshot": OperationKind.INDEX,
    "operationRunHook": OperationKind.HOOK,
    "operationRunCommand": OperationKind.COMMAND,
}
IDLE = "idle"
# Values of the "current task" and "activity" sensors.
TASK_OPTIONS = [IDLE, *(kind.value for kind in OperationKind)]
# Backrest attributes repository maintenance (check, prune, stats, ...) to
# this pseudo plan instead of a real one.
SYSTEM_PLAN_ID = "_system_"


@dataclass(frozen=True, slots=True)
class Operation:
    """Any Backrest operation, reduced to what the activity sensors need."""

    id: int
    kind: OperationKind
    status: OperationStatus
    plan_id: str
    repo_id: str
    start: datetime | None
    end: datetime | None
    message: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Operation | None:
        """Parse an Operation message. Returns None for unknown types."""
        op_id = _int(data.get("id"))
        kind = next((k for key, k in _OPERATION_KINDS.items() if key in data), None)
        if op_id is None or kind is None:
            return None
        return cls(
            id=op_id,
            kind=kind,
            status=parse_status(data.get("status")),
            plan_id=_str(data.get("planId")),
            repo_id=_str(data.get("repoId")),
            start=_ms_to_datetime(data.get("unixTimeStartMs")),
            end=_ms_to_datetime(data.get("unixTimeEndMs")),
            message=_truncate(_str(data.get("displayMessage"))),
        )

    @property
    def is_running(self) -> bool:
        """Return whether the operation is in progress."""
        return self.status is OperationStatus.IN_PROGRESS

    @property
    def completed_at(self) -> datetime | None:
        """Return when the operation finished (or started, if unknown)."""
        return self.end or self.start

    def as_dict(self) -> dict[str, Any]:
        """Return the operation as state attribute."""
        return {
            "task": self.kind.value,
            "repository": self.repo_id or None,
            "plan": None if self.plan_id.startswith("_") else self.plan_id or None,
            "started": self.start.isoformat() if self.start else None,
        }


def current_task(running: list[Operation]) -> str:
    """Return the task shown for a set of running operations.

    Hooks only run briefly around other tasks, so a running backup or check
    wins over them. Among the rest, the most recently started one is shown.
    """
    if not running:
        return IDLE
    main = [op for op in running if op.kind is not OperationKind.HOOK] or running
    return max(main, key=lambda op: op.id).kind.value


@dataclass(frozen=True, slots=True)
class PlanState:
    """Derived state of a backup plan."""

    plan: Plan
    last_status: OperationStatus | None
    last_attempt: datetime | None
    last_finished: BackupOperation | None
    last_success_time: datetime | None
    last_success: BackupOperation | None
    last_error: BackupOperation | None
    last_status_change: datetime | None
    next_backup: datetime | None
    running: tuple[BackupOperation, ...]
    queued: int
    history_complete: bool
    summary: Summary | None
    overdue: bool | None

    @property
    def is_running(self) -> bool:
        """Return whether a backup of the plan is in progress."""
        return bool(self.running)

    @property
    def last_failed(self) -> bool | None:
        """Return whether the most recent finished backup failed."""
        if self.last_finished is not None:
            return self.last_finished.status is OperationStatus.ERROR
        if self.last_status in FINISHED_STATUSES:
            # Known from the summary dashboard only.
            return self.last_status is OperationStatus.ERROR
        return False if self.history_complete else None

    @property
    def never_succeeded(self) -> bool | None:
        """Return whether no successful backup is known for the plan.

        Unknown when the fetched operations do not cover the plan's whole
        history and no summary dashboard (last 30 days) is available.
        """
        if self.last_success_time is not None:
            return False
        if self.history_complete or self.summary is not None:
            return True
        return None

    @property
    def progress(self) -> float | None:
        """Return the progress of the running backup in percent."""
        for operation in self.running:
            if operation.percent_done is not None:
                return round(operation.percent_done, 1)
        return None


def build_plan_state(
    plan: Plan,
    raw_operations: list[dict[str, Any]],
    summary: Summary | None,
    requested: int,
    now: datetime,
) -> PlanState:
    """Derive the state of a plan from its most recent operations."""
    operations = sorted(
        (op for raw in raw_operations if (op := BackupOperation.from_api(raw)) is not None),
        # Order by start time, not by ID: operations synced from a multihost
        # peer get a new, higher ID whenever the peer updates them (e.g. when
        # retention forgets an old snapshot), so the newest ID can belong to a
        # week-old backup.
        key=lambda op: (op.start or datetime.min.replace(tzinfo=UTC), op.id),
    )
    # GetOperations returned fewer entries than requested: the complete history
    # of the plan is known.
    history_complete = len(raw_operations) < requested

    running = tuple(op for op in operations if op.status is OperationStatus.IN_PROGRESS)
    pending = [op for op in operations if op.status is OperationStatus.PENDING]
    # Backrest keeps the next scheduled run as a PENDING operation that starts
    # in the future. PENDING operations due now are waiting in the task queue.
    upcoming = [op.start for op in pending if op.start is not None and op.start > now]
    queued = len(pending) - len(upcoming)
    next_backup = min(upcoming, default=None)
    if (
        next_backup is None
        and summary is not None
        and summary.next_backup is not None
        and summary.next_backup > now
    ):
        next_backup = summary.next_backup

    real = [op for op in operations if not op.dry_run]
    finished = [op for op in real if op.finished]
    last_finished = finished[-1] if finished else None
    latest_running = [op for op in real if op.status is OperationStatus.IN_PROGRESS]

    last_status: OperationStatus | None = None
    last_attempt: datetime | None = None
    if latest_running:
        last_status = OperationStatus.IN_PROGRESS
        last_attempt = latest_running[-1].start
    elif last_finished is not None:
        last_status = last_finished.status
        last_attempt = last_finished.start

    successes = [op for op in finished if op.status in GOOD_STATUSES]
    last_success = successes[-1] if successes else None
    last_success_time = last_success.completed_at if last_success else None
    if summary is not None:
        chart_success = max(
            (
                entry.start + entry.duration
                for entry in summary.recent_backups
                if entry.status in GOOD_STATUSES
            ),
            default=None,
        )
        if last_success_time is None:
            # Fallback when the plan's recent operations contain no successful
            # backup but the 30 day summary does.
            last_success_time = chart_success
        if last_status is None and summary.recent_backups:
            newest = max(summary.recent_backups, key=lambda entry: entry.start)
            if newest.status in FINISHED_STATUSES:
                last_status = newest.status
                last_attempt = newest.start

    errors = [op for op in finished if op.status is OperationStatus.ERROR]
    last_error = errors[-1] if errors else None

    return PlanState(
        plan=plan,
        last_status=last_status,
        last_attempt=last_attempt,
        last_finished=last_finished,
        last_success_time=last_success_time,
        last_success=last_success,
        last_error=last_error,
        last_status_change=_last_status_change(finished, history_complete),
        next_backup=next_backup,
        running=running,
        queued=queued,
        history_complete=history_complete,
        summary=summary,
        overdue=_overdue(plan, last_success_time, finished, history_complete, now),
    )


def _last_status_change(finished: list[BackupOperation], history_complete: bool) -> datetime | None:
    """Return when the result of the plan's backups last changed.

    Only finished backups are considered. The value is unknown when the
    current status streak reaches beyond the fetched history.
    """
    if not finished:
        return None
    current = finished[-1].status
    for previous, following in zip(reversed(finished[:-1]), reversed(finished[1:]), strict=True):
        if previous.status is not current:
            return following.completed_at
    return finished[0].completed_at if history_complete else None


def _overdue(
    plan: Plan,
    last_success_time: datetime | None,
    finished: list[BackupOperation],
    history_complete: bool,
    now: datetime,
) -> bool | None:
    """Return whether the plan missed its expected backup window."""
    if not plan.schedule.enabled:
        return False
    if last_success_time is None:
        # Like Backrest, a plan is not overdue before its first good backup.
        return False if history_complete or finished else None
    period = plan.schedule.nominal_period(now)
    if period is None:
        return None
    return now - last_success_time > period * OVERDUE_GRACE_FACTOR


@dataclass(frozen=True, slots=True)
class RepoState:
    """Derived state of a repository."""

    repo: Repo
    plan_ids: tuple[str, ...]
    summary: Summary | None
    running: tuple[Operation, ...] = ()
    last_check: Operation | None = None
    last_prune: Operation | None = None

    @property
    def current_task(self) -> str:
        """Return the task currently running on the repository."""
        return current_task(list(self.running))

    @property
    def last_check_failed(self) -> bool:
        """Return whether the most recent repository check failed."""
        return self.last_check is not None and self.last_check.status is OperationStatus.ERROR


@dataclass(slots=True)
class BackrestData:
    """Snapshot of everything the integration knows about one instance."""

    config: BackrestConfig
    plans: dict[str, PlanState]
    repos: dict[str, RepoState]
    summary_available: bool
    fetched_at: datetime
    plan_errors: dict[str, str] = field(default_factory=dict)
    # Remote instances (multihost clients) keyed by instance ID. Their plans are
    # part of `plans` under remote_plan_key(instance, plan_id).
    remotes: dict[str, RemoteInstance] = field(default_factory=dict)

    @property
    def running_operations(self) -> list[Operation]:
        """Return all running operations, oldest first."""
        return sorted(
            (op for state in self.repos.values() for op in state.running), key=lambda op: op.id
        )

    @property
    def current_task(self) -> str:
        """Return the task currently running on the instance."""
        return current_task(self.running_operations)

    @property
    def failed_check_repos(self) -> list[str]:
        """Return repositories whose last check failed."""
        return sorted(repo_id for repo_id, state in self.repos.items() if state.last_check_failed)

    @property
    def running_count(self) -> int:
        """Return the number of running backups across all plans."""
        return sum(len(state.running) for state in self.plans.values())

    @property
    def last_success_time(self) -> datetime | None:
        """Return the most recent successful backup across all plans."""
        return max(
            (
                state.last_success_time
                for state in self.plans.values()
                if state.last_success_time is not None
            ),
            default=None,
        )

    def plans_with_last_status(self, statuses: frozenset[OperationStatus]) -> list[str]:
        """Return the plans whose latest finished backup has one of the statuses."""
        return sorted(
            plan_id
            for plan_id, state in self.plans.items()
            if state.last_finished is not None and state.last_finished.status in statuses
        )

    @property
    def problem_plans(self) -> list[str]:
        """Return plans whose last backup failed or that are overdue."""
        return sorted(
            plan_id for plan_id, state in self.plans.items() if state.last_failed or state.overdue
        )


def build_repo_states(
    config: BackrestConfig,
    summary: SummaryDashboard | None,
    raw_operations: list[dict[str, Any]],
) -> dict[str, RepoState]:
    """Derive repository states from the operations of all plans and the system."""
    operations: dict[int, Operation] = {}
    for raw in raw_operations:
        if (op := Operation.from_api(raw)) is not None:
            operations[op.id] = op
    by_repo: dict[str, list[Operation]] = {}
    for op in sorted(operations.values(), key=lambda op: op.id):
        repo_id = op.repo_id
        if not repo_id and (plan := config.plans.get(op.plan_id)):
            repo_id = plan.repo_id
        by_repo.setdefault(repo_id, []).append(op)

    def last_finished(ops: list[Operation], kind: OperationKind) -> Operation | None:
        return next(
            (op for op in reversed(ops) if op.kind is kind and op.status in FINISHED_STATUSES),
            None,
        )

    states: dict[str, RepoState] = {}
    for repo_id, repo in config.repos.items():
        ops = by_repo.get(repo_id, [])
        states[repo_id] = RepoState(
            repo=repo,
            plan_ids=tuple(sorted(p.id for p in config.plans.values() if p.repo_id == repo_id)),
            summary=summary.repos.get(repo_id) if summary else None,
            running=tuple(op for op in ops if op.is_running),
            last_check=last_finished(ops, OperationKind.CHECK),
            last_prune=last_finished(ops, OperationKind.PRUNE),
        )
    return states
