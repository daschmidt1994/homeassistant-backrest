"""Sensors for the Backrest integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BackrestConfigEntry
from .entity import (
    BackrestPlanEntity,
    BackrestRemoteEntity,
    BackrestRepoEntity,
    BackrestServerEntity,
    async_setup_dynamic_entities,
)
from .models import (
    GOOD_STATUSES,
    LAST_STATUS_OPTIONS,
    TASK_OPTIONS,
    BackrestData,
    Operation,
    OperationStatus,
    PeerConnection,
    PlanState,
    RemoteInstance,
    RepoState,
)

PARALLEL_UPDATES = 0

type StateValue = str | int | float | datetime | None


@dataclass(frozen=True, kw_only=True)
class BackrestServerSensorDescription(SensorEntityDescription):
    """Describes a sensor of the Backrest server."""

    value_fn: Callable[[BackrestData], StateValue]
    attributes_fn: Callable[[BackrestData], dict[str, Any] | None] = lambda _: None


@dataclass(frozen=True, kw_only=True)
class BackrestPlanSensorDescription(SensorEntityDescription):
    """Describes a sensor of a backup plan."""

    value_fn: Callable[[PlanState], StateValue]
    attributes_fn: Callable[[PlanState], dict[str, Any] | None] = lambda _: None
    requires_summary: bool = False


@dataclass(frozen=True, kw_only=True)
class BackrestRepoSensorDescription(SensorEntityDescription):
    """Describes a sensor of a repository."""

    value_fn: Callable[[RepoState], StateValue]
    attributes_fn: Callable[[RepoState], dict[str, Any] | None] = lambda _: None
    requires_summary: bool = False


def _plan_names(plan_ids: list[str]) -> dict[str, Any]:
    return {"plans": plan_ids}


def _last_success_plan(data: BackrestData) -> dict[str, Any] | None:
    latest = data.last_success_time
    if latest is None:
        return None
    plans = sorted(
        plan_id for plan_id, state in data.plans.items() if state.last_success_time == latest
    )
    return {"plan": plans[0]} if plans else None


def _operations(operations: list[Operation]) -> dict[str, Any]:
    return {"operations": [op.as_dict() for op in operations]}


def _result(operation: Operation | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    failed = operation.status is OperationStatus.ERROR
    return {
        "status": operation.status.value,
        "error_message": (operation.message or None) if failed else None,
    }


SERVER_SENSORS: tuple[BackrestServerSensorDescription, ...] = (
    BackrestServerSensorDescription(
        key="activity",
        translation_key="activity",
        device_class=SensorDeviceClass.ENUM,
        options=TASK_OPTIONS,
        value_fn=lambda data: data.current_task,
        attributes_fn=lambda data: _operations(data.running_operations),
    ),
    BackrestServerSensorDescription(
        key="plans",
        translation_key="plans",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.plans),
        attributes_fn=lambda data: _plan_names(sorted(data.plans)),
    ),
    BackrestServerSensorDescription(
        key="successful_plans",
        translation_key="successful_plans",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.plans_with_last_status(GOOD_STATUSES)),
        attributes_fn=lambda data: _plan_names(data.plans_with_last_status(GOOD_STATUSES)),
    ),
    BackrestServerSensorDescription(
        key="failed_plans",
        translation_key="failed_plans",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.plans_with_last_status(frozenset({OperationStatus.ERROR}))),
        attributes_fn=lambda data: _plan_names(
            data.plans_with_last_status(frozenset({OperationStatus.ERROR}))
        ),
    ),
    BackrestServerSensorDescription(
        key="running_backups",
        translation_key="running_backups",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.running_count,
        attributes_fn=lambda data: _plan_names(
            sorted(p for p, s in data.plans.items() if s.is_running)
        ),
    ),
    BackrestServerSensorDescription(
        key="plans_without_success",
        translation_key="plans_without_success",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: sum(
            1 for state in data.plans.values() if state.never_succeeded is True
        ),
        attributes_fn=lambda data: _plan_names(
            sorted(p for p, s in data.plans.items() if s.never_succeeded is True)
        ),
    ),
    BackrestServerSensorDescription(
        key="last_success",
        translation_key="last_success_overall",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.last_success_time,
        attributes_fn=_last_success_plan,
    ),
    BackrestServerSensorDescription(
        key="repositories",
        translation_key="repositories",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.repos),
        attributes_fn=lambda data: {"repositories": sorted(data.repos)},
    ),
)


def _duration(state: PlanState) -> float | None:
    if state.last_finished is None or (duration := state.last_finished.duration) is None:
        return None
    return round(duration.total_seconds(), 1)


def _last_error_message(state: PlanState) -> str | None:
    if state.last_error is None:
        return None
    return state.last_error.message or OperationStatus.ERROR.value


def _last_error_attributes(state: PlanState) -> dict[str, Any] | None:
    if state.last_error is None or state.last_error.completed_at is None:
        return None
    return {"occurred_at": state.last_error.completed_at.isoformat()}


def _last_size(state: PlanState) -> int | None:
    if state.last_success and state.last_success.bytes_processed is not None:
        return state.last_success.bytes_processed
    if state.summary is not None:
        return state.summary.protected_bytes
    return None


PLAN_SENSORS: tuple[BackrestPlanSensorDescription, ...] = (
    BackrestPlanSensorDescription(
        key="last_backup_status",
        translation_key="last_backup_status",
        device_class=SensorDeviceClass.ENUM,
        options=LAST_STATUS_OPTIONS,
        value_fn=lambda state: state.last_status.value if state.last_status else None,
    ),
    BackrestPlanSensorDescription(
        key="last_success",
        translation_key="last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: state.last_success_time,
        attributes_fn=lambda state: (
            {"snapshot_id": state.last_success.snapshot_id}
            if state.last_success and state.last_success.snapshot_id
            else None
        ),
    ),
    BackrestPlanSensorDescription(
        key="last_attempt",
        translation_key="last_attempt",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.last_attempt,
    ),
    BackrestPlanSensorDescription(
        key="next_backup",
        translation_key="next_backup",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: state.next_backup,
    ),
    BackrestPlanSensorDescription(
        key="last_duration",
        translation_key="last_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_duration,
    ),
    BackrestPlanSensorDescription(
        key="successful_backups_30d",
        translation_key="successful_backups_30d",
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: (
            state.summary.success_30d + state.summary.warning_30d if state.summary else None
        ),
        attributes_fn=lambda state: (
            {"with_warnings": state.summary.warning_30d} if state.summary else None
        ),
    ),
    BackrestPlanSensorDescription(
        key="failed_backups_30d",
        translation_key="failed_backups_30d",
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: state.summary.failed_30d if state.summary else None,
    ),
    BackrestPlanSensorDescription(
        key="running_backups",
        translation_key="running_backups",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: len(state.running),
        attributes_fn=lambda state: {"queued": state.queued},
    ),
    BackrestPlanSensorDescription(
        key="backup_progress",
        translation_key="backup_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda state: state.progress if state.is_running else 0,
    ),
    BackrestPlanSensorDescription(
        key="last_status_change",
        translation_key="last_status_change",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.last_status_change,
    ),
    BackrestPlanSensorDescription(
        key="last_error",
        translation_key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_error_message,
        attributes_fn=_last_error_attributes,
    ),
    BackrestPlanSensorDescription(
        key="last_backup_size",
        translation_key="last_backup_size",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_last_size,
    ),
    BackrestPlanSensorDescription(
        key="last_backup_data_added",
        translation_key="last_backup_data_added",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: state.last_success.bytes_added if state.last_success else None,
    ),
    BackrestPlanSensorDescription(
        key="last_backup_files",
        translation_key="last_backup_files",
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda state: state.last_success.files_processed if state.last_success else None,
    ),
)

REPO_SENSORS: tuple[BackrestRepoSensorDescription, ...] = (
    BackrestRepoSensorDescription(
        key="current_task",
        translation_key="current_task",
        device_class=SensorDeviceClass.ENUM,
        options=TASK_OPTIONS,
        value_fn=lambda state: state.current_task,
        attributes_fn=lambda state: _operations(list(state.running)),
    ),
    BackrestRepoSensorDescription(
        key="last_check",
        translation_key="last_check",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: state.last_check.completed_at if state.last_check else None,
        attributes_fn=lambda state: _result(state.last_check),
    ),
    BackrestRepoSensorDescription(
        key="last_prune",
        translation_key="last_prune",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: state.last_prune.completed_at if state.last_prune else None,
        attributes_fn=lambda state: _result(state.last_prune),
    ),
    BackrestRepoSensorDescription(
        key="plans",
        translation_key="repo_plans",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda state: len(state.plan_ids),
    ),
    BackrestRepoSensorDescription(
        key="successful_backups_30d",
        translation_key="successful_backups_30d",
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: (
            state.summary.success_30d + state.summary.warning_30d if state.summary else None
        ),
    ),
    BackrestRepoSensorDescription(
        key="failed_backups_30d",
        translation_key="failed_backups_30d",
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: state.summary.failed_30d if state.summary else None,
    ),
    BackrestRepoSensorDescription(
        key="data_added_30d",
        translation_key="data_added_30d",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: state.summary.bytes_added_30d if state.summary else None,
    ),
    BackrestRepoSensorDescription(
        key="protected_data",
        translation_key="protected_data",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.MEASUREMENT,
        requires_summary=True,
        value_fn=lambda state: state.summary.protected_bytes if state.summary else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class BackrestRemoteSensorDescription(SensorEntityDescription):
    """Describes a sensor of a remote instance."""

    value_fn: Callable[[RemoteInstance], StateValue]


REMOTE_SENSORS: tuple[BackrestRemoteSensorDescription, ...] = (
    BackrestRemoteSensorDescription(
        key="last_contact",
        translation_key="last_contact",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda remote: remote.last_heartbeat,
    ),
    BackrestRemoteSensorDescription(
        key="connection",
        translation_key="connection",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in PeerConnection],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda remote: remote.connection.value,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BackrestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Backrest sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        BackrestServerSensor(coordinator, description) for description in SERVER_SENSORS
    )

    def plan_entities(plan_id: str) -> list[BackrestPlanSensor]:
        # The summary dashboard only covers local plans.
        remote = coordinator.data.plans[plan_id].plan.is_remote
        return [
            BackrestPlanSensor(coordinator, description, plan_id)
            for description in PLAN_SENSORS
            if not description.requires_summary or (coordinator.summary_supported and not remote)
        ]

    def repo_entities(repo_id: str) -> list[BackrestRepoSensor]:
        return [
            BackrestRepoSensor(coordinator, description, repo_id)
            for description in REPO_SENSORS
            if not description.requires_summary or coordinator.summary_supported
        ]

    def remote_entities(instance_id: str) -> list[BackrestRemoteSensor]:
        return [
            BackrestRemoteSensor(coordinator, description, instance_id)
            for description in REMOTE_SENSORS
        ]

    async_setup_dynamic_entities(
        entry, async_add_entities, plan_entities, repo_entities, remote_entities
    )


class BackrestServerSensor(BackrestServerEntity, SensorEntity):
    """Sensor of the Backrest server."""

    entity_description: BackrestServerSensorDescription

    @property
    def native_value(self) -> StateValue:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        return self.entity_description.attributes_fn(self.coordinator.data)


class BackrestPlanSensor(BackrestPlanEntity, SensorEntity):
    """Sensor of a backup plan."""

    entity_description: BackrestPlanSensorDescription

    @property
    def native_value(self) -> StateValue:
        """Return the state."""
        if (state := self.plan_state) is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        if (state := self.plan_state) is None:
            return None
        return self.entity_description.attributes_fn(state)


class BackrestRepoSensor(BackrestRepoEntity, SensorEntity):
    """Sensor of a repository."""

    entity_description: BackrestRepoSensorDescription

    @property
    def native_value(self) -> StateValue:
        """Return the state."""
        if (state := self.repo_state) is None:
            return None
        return self.entity_description.value_fn(state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return additional attributes."""
        if (state := self.repo_state) is None:
            return None
        return self.entity_description.attributes_fn(state)


class BackrestRemoteSensor(BackrestRemoteEntity, SensorEntity):
    """Sensor of a remote (multihost client) instance."""

    entity_description: BackrestRemoteSensorDescription

    @property
    def native_value(self) -> StateValue:
        """Return the state."""
        if (remote := self.remote) is None:
            return None
        return self.entity_description.value_fn(remote)
