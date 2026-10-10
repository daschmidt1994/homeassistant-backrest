"""Binary sensors for the Backrest integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import BackrestConfigEntry
from .entity import BackrestPlanEntity, BackrestServerEntity, async_setup_dynamic_entities
from .models import BackrestData, PlanState

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BackrestServerBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a binary sensor of the Backrest server."""

    value_fn: Callable[[BackrestData], bool | None]
    attributes_fn: Callable[[BackrestData], dict[str, Any] | None] = lambda _: None


@dataclass(frozen=True, kw_only=True)
class BackrestPlanBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a binary sensor of a backup plan."""

    value_fn: Callable[[PlanState], bool | None]
    attributes_fn: Callable[[PlanState], dict[str, Any] | None] = lambda _: None


def _failure_attributes(state: PlanState) -> dict[str, Any] | None:
    # Exposed on the binary sensor itself so automations triggered by it see
    # the message in the same state change.
    finished = state.last_finished
    if finished is None or not state.last_failed:
        return None
    return {"error_message": finished.message or None}


def _overdue_attributes(state: PlanState) -> dict[str, Any]:
    last = state.last_success_time
    return {"last_success": last.isoformat() if last else None}


SERVER_BINARY_SENSORS: tuple[BackrestServerBinarySensorDescription, ...] = (
    BackrestServerBinarySensorDescription(
        key="backup_running",
        translation_key="backup_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.running_count > 0,
    ),
)

PLAN_BINARY_SENSORS: tuple[BackrestPlanBinarySensorDescription, ...] = (
    BackrestPlanBinarySensorDescription(
        key="backup_running",
        translation_key="backup_running",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda state: state.is_running,
    ),
    BackrestPlanBinarySensorDescription(
        key="last_backup_failed",
        translation_key="last_backup_failed",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda state: state.last_failed,
        attributes_fn=_failure_attributes,
    ),
    BackrestPlanBinarySensorDescription(
        key="overdue",
        translation_key="overdue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda state: state.overdue,
        attributes_fn=_overdue_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BackrestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Backrest binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            BackrestReachableBinarySensor(
                coordinator,
                BinarySensorEntityDescription(
                    key="reachable",
                    translation_key="reachable",
                    device_class=BinarySensorDeviceClass.CONNECTIVITY,
                ),
            ),
            BackrestProblemBinarySensor(
                coordinator,
                BinarySensorEntityDescription(
                    key="problem",
                    translation_key="problem",
                    device_class=BinarySensorDeviceClass.PROBLEM,
                ),
            ),
            *(
                BackrestServerBinarySensor(coordinator, description)
                for description in SERVER_BINARY_SENSORS
            ),
        ]
    )
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda plan_id: [
            BackrestPlanBinarySensor(coordinator, description, plan_id)
            for description in PLAN_BINARY_SENSORS
        ],
    )


class BackrestServerBinarySensor(BackrestServerEntity, BinarySensorEntity):
    """Binary sensor of the Backrest server."""

    entity_description: BackrestServerBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator.data)


class BackrestReachableBinarySensor(BackrestServerEntity, BinarySensorEntity):
    """Whether the last request to Backrest succeeded. Never unavailable."""

    @property
    def available(self) -> bool:
        """Stay available to report connection problems."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether Backrest is reachable."""
        return self.coordinator.last_update_success


class BackrestProblemBinarySensor(BackrestServerEntity, BinarySensorEntity):
    """On when Backrest is unreachable, a backup failed or a plan is overdue."""

    @property
    def available(self) -> bool:
        """Stay available so an unreachable server counts as a problem."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether there is a problem."""
        if not self.coordinator.last_update_success or self.coordinator.data is None:
            return True
        return bool(self.coordinator.data.problem_plans)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the reasons."""
        data = self.coordinator.data
        if not self.coordinator.last_update_success or data is None:
            return {"unreachable": True, "failed_plans": [], "overdue_plans": []}
        return {
            "unreachable": False,
            "failed_plans": sorted(p for p, s in data.plans.items() if s.last_failed),
            "overdue_plans": sorted(p for p, s in data.plans.items() if s.overdue),
        }


class BackrestPlanBinarySensor(BackrestPlanEntity, BinarySensorEntity):
    """Binary sensor of a backup plan."""

    entity_description: BackrestPlanBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
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
