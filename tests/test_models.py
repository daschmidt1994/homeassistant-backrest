"""Tests for parsing and deriving plan state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from custom_components.backrest.models import (
    BackrestConfig,
    BackupOperation,
    OperationStatus,
    Plan,
    Schedule,
    Summary,
    build_plan_state,
    parse_status,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _ms(value: datetime) -> str:
    return str(int(value.timestamp() * 1000))


def _op(
    op_id: int,
    status: str,
    start: datetime,
    end: datetime | None = None,
    **backup: Any,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "id": str(op_id),
        "status": status,
        "unixTimeStartMs": _ms(start),
        "operationBackup": backup,
    }
    if end:
        operation["unixTimeEndMs"] = _ms(end)
    return operation


def _plan(schedule: dict[str, Any] | None = None) -> Plan:
    plan = Plan.from_api(
        {"id": "nas", "repo": "r", "schedule": schedule or {"maxFrequencyDays": 1}}
    )
    assert plan is not None
    return plan


def test_parse_status() -> None:
    """Enum names and numbers are understood, unknown values are tolerated."""
    assert parse_status("STATUS_SUCCESS") is OperationStatus.SUCCESS
    assert parse_status(7) is OperationStatus.WARNING
    assert parse_status("STATUS_FROM_THE_FUTURE") is OperationStatus.UNKNOWN
    assert parse_status(None) is OperationStatus.UNKNOWN
    assert parse_status(True) is OperationStatus.UNKNOWN


def test_config_drops_secrets_and_invalid_entries() -> None:
    """Only IDs, repo links and schedules are kept."""
    config = BackrestConfig.from_api(
        {
            "instance": "inst",
            "repos": [{"id": "r", "password": "secret", "guid": "g"}, {"uri": "no-id"}, "junk"],
            "plans": [{"id": "nas", "repo": "r", "paths": ["/a", "/b"]}, {"id": ""}, None],
            "auth": {"users": [{"name": "admin", "passwordBcrypt": "hash"}]},
            "sync": {"identity": {"keyId": "ed25519.key"}},
        }
    )
    assert list(config.plans) == ["nas"]
    assert list(config.repos) == ["r"]
    assert config.plans["nas"].path_count == 2
    assert config.unique_id == "ed25519.key"
    assert config.auth_enabled
    assert "secret" not in repr(config)
    assert "hash" not in repr(config)


def test_config_without_identity() -> None:
    """Older versions without sync identity fall back to the instance name."""
    config = BackrestConfig.from_api({"instance": "inst", "auth": {"disabled": True}})
    assert config.unique_id == "inst"
    assert not config.auth_enabled
    assert BackrestConfig.from_api({}).unique_id is None


@pytest.mark.parametrize(
    ("schedule", "kind", "period"),
    [
        ({"disabled": True}, "disabled", None),
        ({}, "disabled", None),
        ({"maxFrequencyDays": 2}, "days", timedelta(days=2)),
        ({"maxFrequencyHours": 6}, "hours", timedelta(hours=6)),
        ({"cron": "0 3 * * *"}, "cron", timedelta(days=1)),
        ({"cron": "0 3 * * 1"}, "cron", timedelta(days=7)),
        ({"cron": "*/15 * * * *"}, "cron", timedelta(minutes=15)),
        ({"cron": "not a cron"}, "cron", None),
        ({"maxFrequencyDays": 0}, "unknown", None),
    ],
)
def test_schedule_period(schedule: dict[str, Any], kind: str, period: timedelta | None) -> None:
    """Nominal periods match Backrest's definition."""
    parsed = Schedule.from_api(schedule)
    assert parsed.kind == kind
    assert parsed.nominal_period(NOW) == period


def test_operation_parsing() -> None:
    """Backup operations are parsed defensively; other operations are skipped."""
    assert BackupOperation.from_api({"id": "1", "operationForget": {}}) is None
    assert BackupOperation.from_api({"operationBackup": {}}) is None
    operation = BackupOperation.from_api(
        {
            "id": "7",
            "status": "STATUS_INPROGRESS",
            "unixTimeStartMs": "not-a-number",
            "displayMessage": "x" * 400,
            "operationBackup": {"lastStatus": {"status": {"percentDone": 1.7}}},
        }
    )
    assert operation is not None
    assert operation.start is None
    assert operation.percent_done == 100.0
    assert len(operation.message) == 255


def test_successful_plan() -> None:
    """Last status, success, duration, size and next run come from operations."""
    ops = [
        _op(
            1,
            "STATUS_SUCCESS",
            NOW - timedelta(hours=3),
            NOW - timedelta(hours=3) + timedelta(seconds=90),
            lastStatus={
                "summary": {
                    "totalBytesProcessed": "1000",
                    "dataAdded": "10",
                    "totalFilesProcessed": "3",
                }
            },
        ),
        _op(2, "STATUS_PENDING", NOW + timedelta(hours=4)),
    ]
    state = build_plan_state(_plan(), ops, None, 50, NOW)
    assert state.last_status is OperationStatus.SUCCESS
    assert state.last_finished.duration == timedelta(seconds=90)
    assert state.last_success.bytes_processed == 1000
    assert state.last_success_time == NOW - timedelta(hours=3) + timedelta(seconds=90)
    assert state.next_backup == NOW + timedelta(hours=4)
    assert state.queued == 0
    assert state.last_failed is False
    assert state.overdue is False
    assert state.never_succeeded is False
    assert state.history_complete


def test_running_backup_is_not_success() -> None:
    """An in-progress backup is reported as running, not as successful."""
    ops = [
        _op(1, "STATUS_ERROR", NOW - timedelta(days=1), NOW - timedelta(days=1)),
        _op(
            2,
            "STATUS_INPROGRESS",
            NOW - timedelta(minutes=2),
            lastStatus={"status": {"percentDone": 0.25}},
        ),
    ]
    state = build_plan_state(_plan(), ops, None, 50, NOW)
    assert state.last_status is OperationStatus.IN_PROGRESS
    assert state.is_running
    assert state.progress == 25.0
    assert state.last_attempt == NOW - timedelta(minutes=2)
    assert state.last_success_time is None
    assert state.last_failed is True


def test_queued_backup() -> None:
    """PENDING operations that are due count as queued, not as next backup."""
    ops = [_op(1, "STATUS_PENDING", NOW - timedelta(seconds=10))]
    state = build_plan_state(_plan(), ops, None, 50, NOW)
    assert state.queued == 1
    assert state.next_backup is None


def test_dry_runs_are_ignored() -> None:
    """Dry runs do not count as successful backups."""
    ops = [_op(1, "STATUS_SUCCESS", NOW - timedelta(hours=1), NOW, dryRun=True)]
    state = build_plan_state(_plan(), ops, None, 50, NOW)
    assert state.last_status is None
    assert state.last_success_time is None


def test_overdue() -> None:
    """A plan is overdue once 1.25 periods passed since its last good backup."""
    plan = _plan({"maxFrequencyDays": 1})
    ok = [_op(1, "STATUS_SUCCESS", NOW - timedelta(hours=29), NOW - timedelta(hours=29))]
    late = [_op(1, "STATUS_SUCCESS", NOW - timedelta(hours=31), NOW - timedelta(hours=31))]
    assert build_plan_state(plan, ok, None, 50, NOW).overdue is False
    assert build_plan_state(plan, late, None, 50, NOW).overdue is True
    # Warnings still count as good backups.
    warn = [_op(1, "STATUS_WARNING", NOW - timedelta(hours=2), NOW - timedelta(hours=2))]
    assert build_plan_state(plan, warn, None, 50, NOW).overdue is False
    # Disabled schedules are never overdue.
    assert build_plan_state(_plan({"disabled": True}), late, None, 50, NOW).overdue is False


def test_last_success_ignores_operation_id_order() -> None:
    """A re-synced old peer operation with a higher ID is not the newest backup.

    Multihost peers resend an operation with a new ID when it changes, e.g.
    when retention forgets its snapshot a week later.
    """
    plan = _plan({"maxFrequencyDays": 1})
    recent = NOW - timedelta(hours=18)
    week_old = NOW - timedelta(days=7, hours=18)
    operations = [
        _op(10, "STATUS_SUCCESS", recent, recent + timedelta(seconds=9)),
        _op(20, "STATUS_SUCCESS", week_old, week_old + timedelta(seconds=9)),
    ]
    state = build_plan_state(plan, operations, None, 50, NOW)
    assert state.last_success_time == recent + timedelta(seconds=9)
    assert state.overdue is False


def test_overdue_unknown_without_history() -> None:
    """Without a known success in a truncated history overdue is unknown."""
    plan = _plan()
    assert build_plan_state(plan, [], None, 50, NOW).overdue is False
    truncated = [{"id": str(i), "operationForget": {}} for i in range(50)]
    state = build_plan_state(plan, truncated, None, 50, NOW)
    assert not state.history_complete
    assert state.overdue is None
    assert state.never_succeeded is None
    assert state.last_failed is None


def test_summary_fallback() -> None:
    """The 30 day summary fills gaps in the fetched operations."""
    summary = Summary.from_api(
        {
            "id": "nas",
            "backupsSuccessLast30days": "4",
            "backupsFailed30days": "1",
            "nextBackupTimeMs": _ms(NOW + timedelta(hours=1)),
            "protectedBytes": "123",
            "recentBackups": {
                "timestampMs": [_ms(NOW - timedelta(days=2)), _ms(NOW - timedelta(days=3))],
                "durationMs": ["60000", "1000"],
                "status": ["STATUS_ERROR", "STATUS_SUCCESS"],
            },
        }
    )
    truncated = [{"id": str(i), "operationForget": {}} for i in range(50)]
    state = build_plan_state(_plan(), truncated, summary, 50, NOW)
    assert state.last_success_time == NOW - timedelta(days=3) + timedelta(seconds=1)
    assert state.last_status is OperationStatus.ERROR
    assert state.last_failed is True
    assert state.next_backup == NOW + timedelta(hours=1)
    assert state.never_succeeded is False
    assert summary.success_30d == 4
    assert summary.failed_30d == 1


def test_last_status_change() -> None:
    """The change time is the end of the first run of the current streak."""
    ops = [
        _op(1, "STATUS_SUCCESS", NOW - timedelta(hours=5), NOW - timedelta(hours=5)),
        _op(
            2,
            "STATUS_ERROR",
            NOW - timedelta(hours=4),
            NOW - timedelta(hours=4) + timedelta(minutes=1),
        ),
        _op(3, "STATUS_ERROR", NOW - timedelta(hours=3), NOW - timedelta(hours=3)),
    ]
    state = build_plan_state(_plan(), ops, None, 50, NOW)
    assert state.last_status_change == NOW - timedelta(hours=4) + timedelta(minutes=1)
    assert state.last_error.id == 3

    # A streak covering the complete history started with the first backup.
    same = ops[1:]
    state = build_plan_state(_plan(), same, None, 50, NOW)
    assert state.last_status_change == NOW - timedelta(hours=4) + timedelta(minutes=1)

    # If the streak reaches beyond a truncated history, the change time is unknown.
    filler = [{"id": str(100 + i), "operationForget": {}} for i in range(48)]
    state = build_plan_state(_plan(), same + filler, None, 50, NOW)
    assert not state.history_complete
    assert state.last_status_change is None
