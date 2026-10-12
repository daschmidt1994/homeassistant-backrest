"""Tests for the activity, current task and repository maintenance sensors."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.models import (
    Operation,
    OperationKind,
    OperationStatus,
    current_task,
)

from .conftest import FakeBackrest

ACTIVITY = "sensor.backrest_test_instance_activity"
CURRENT_TASK = "sensor.backrest_repository_local_repo_current_task"
LAST_CHECK = "sensor.backrest_repository_local_repo_last_check"
LAST_PRUNE = "sensor.backrest_repository_local_repo_last_prune"
PROBLEM = "binary_sensor.backrest_test_instance_problem"


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_idle(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Without running operations everything is idle and no check is known."""
    assert hass.states.get(ACTIVITY).state == "idle"
    assert hass.states.get(ACTIVITY).attributes["operations"] == []
    assert hass.states.get(CURRENT_TASK).state == "idle"
    assert hass.states.get(LAST_CHECK).state == STATE_UNKNOWN
    assert hass.states.get(LAST_PRUNE).state == STATE_UNKNOWN


async def test_running_check_and_result(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A running check is shown live and its result afterwards."""
    now = dt_util.utcnow()
    op_id = backrest.add_system_operation(
        "operationCheck", "local-repo", "STATUS_INPROGRESS", now - timedelta(minutes=2)
    )
    await _refresh(hass, setup_integration)

    assert hass.states.get(CURRENT_TASK).state == "check"
    activity = hass.states.get(ACTIVITY)
    assert activity.state == "check"
    (operation,) = activity.attributes["operations"]
    assert operation["task"] == "check"
    assert operation["repository"] == "local-repo"
    assert operation["plan"] is None
    started = dt_util.parse_datetime(operation["started"])
    assert abs(started - (now - timedelta(minutes=2))) < timedelta(seconds=1)
    assert hass.states.get(LAST_CHECK).state == STATE_UNKNOWN

    backrest.set_status(op_id, "STATUS_ERROR", end=now)
    backrest.operations["_system_"][-1]["displayMessage"] = "Fatal: repository contains errors"
    await _refresh(hass, setup_integration)

    assert hass.states.get(CURRENT_TASK).state == "idle"
    last_check = hass.states.get(LAST_CHECK)
    assert abs(dt_util.parse_datetime(last_check.state) - now) < timedelta(seconds=1)
    assert last_check.attributes["status"] == "error"
    assert last_check.attributes["error_message"] == "Fatal: repository contains errors"
    assert hass.states.get(PROBLEM).attributes["failed_checks"] == ["local-repo"]

    backrest.add_system_operation(
        "operationCheck", "local-repo", "STATUS_SUCCESS", now, now + timedelta(minutes=3)
    )
    await _refresh(hass, setup_integration)
    last_check = hass.states.get(LAST_CHECK)
    assert last_check.attributes["status"] == "success"
    assert last_check.attributes["error_message"] is None
    assert hass.states.get(PROBLEM).attributes["failed_checks"] == []


async def test_prune_and_backup_activity(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Prune results are tracked and backups show up as repository activity."""
    now = dt_util.utcnow()
    backrest.add_system_operation(
        "operationPrune", "local-repo", "STATUS_SUCCESS", now - timedelta(hours=1), now
    )
    backrest.add_backup("docs", "STATUS_INPROGRESS", now)
    await _refresh(hass, setup_integration)

    last_prune = hass.states.get(LAST_PRUNE)
    assert abs(dt_util.parse_datetime(last_prune.state) - now) < timedelta(seconds=1)
    assert last_prune.attributes["status"] == "success"
    assert hass.states.get(CURRENT_TASK).state == "backup"
    activity = hass.states.get(ACTIVITY)
    assert activity.state == "backup"
    assert activity.attributes["operations"][0]["plan"] == "docs"


async def test_system_operations_failure_is_not_fatal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Missing maintenance data keeps the integration working."""
    original = setup_integration.runtime_data.client.async_get_operations

    async def failing(plan_id: str, instance_id: str | None, last_n: int):
        if plan_id == "_system_":
            raise RuntimeError("boom")
        return await original(plan_id, instance_id, last_n)

    setup_integration.runtime_data.client.async_get_operations = failing
    await _refresh(hass, setup_integration)
    assert setup_integration.runtime_data.last_update_success
    assert hass.states.get(CURRENT_TASK).state == "idle"


def _op(op_id: int, kind: OperationKind) -> Operation:
    return Operation(
        id=op_id,
        kind=kind,
        status=OperationStatus.IN_PROGRESS,
        plan_id="nas",
        repo_id="local-repo",
        start=None,
        end=None,
        message="",
    )


def test_current_task_prefers_real_work_over_hooks() -> None:
    """Hooks only win when nothing else runs; otherwise the newest task wins."""
    assert current_task([]) == "idle"
    assert current_task([_op(1, OperationKind.HOOK)]) == "hook"
    assert current_task([_op(1, OperationKind.BACKUP), _op(2, OperationKind.HOOK)]) == "backup"
    assert current_task([_op(1, OperationKind.BACKUP), _op(3, OperationKind.CHECK)]) == "check"


def test_operation_parsing() -> None:
    """Unknown operation types are ignored."""
    assert Operation.from_api({"id": "5", "operationPrune": {}}).kind is OperationKind.PRUNE
    assert Operation.from_api({"id": "5", "operationSomethingNew": {}}) is None
    assert Operation.from_api({"operationPrune": {}}) is None
