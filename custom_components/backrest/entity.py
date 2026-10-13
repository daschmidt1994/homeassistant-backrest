"""Base entities and device handling for the Backrest integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BackrestConfigEntry, BackrestCoordinator
from .models import PlanState, RepoState

MANUFACTURER = "Backrest"

# Newer Home Assistant versions link devices by device registry ID and reject
# the deprecated identifier based `via_device` when a device is registered
# again at runtime, for example after an entity was renamed.
_SUPPORTS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


def _instance_uid(entry: BackrestConfigEntry) -> str:
    return entry.unique_id or entry.entry_id


def plan_device_identifier(entry: BackrestConfigEntry, plan_id: str) -> tuple[str, str]:
    """Return the device identifier of a plan."""
    return (DOMAIN, f"{_instance_uid(entry)}_plan_{plan_id}")


def repo_device_identifier(entry: BackrestConfigEntry, repo_id: str) -> tuple[str, str]:
    """Return the device identifier of a repository."""
    return (DOMAIN, f"{_instance_uid(entry)}_repo_{repo_id}")


def _via_server(coordinator: BackrestCoordinator) -> DeviceInfo:
    """Return the device info fields linking a device to the server device."""
    if not _SUPPORTS_VIA_DEVICE_ID:
        return DeviceInfo(via_device=(DOMAIN, _instance_uid(coordinator.config_entry)))
    if coordinator.server_device_id is None:
        return DeviceInfo()
    return DeviceInfo(via_device_id=coordinator.server_device_id)


def server_device_info(entry: BackrestConfigEntry, coordinator: BackrestCoordinator) -> DeviceInfo:
    """Return the device info of the Backrest server."""
    instance = coordinator.data.config.instance if coordinator.data else ""
    return DeviceInfo(
        identifiers={(DOMAIN, _instance_uid(entry))},
        name=f"Backrest {instance}".strip(),
        manufacturer=MANUFACTURER,
        model="Backrest server",
        configuration_url=str(coordinator.client.base_url),
    )


class BackrestEntity(CoordinatorEntity[BackrestCoordinator]):
    """Base class for Backrest entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BackrestCoordinator, description: EntityDescription) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description


class BackrestServerEntity(BackrestEntity):
    """Entity attached to the Backrest server device."""

    def __init__(self, coordinator: BackrestCoordinator, description: EntityDescription) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, description)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{_instance_uid(entry)}_{description.key}"
        self._attr_device_info = server_device_info(entry, coordinator)


class BackrestPlanEntity(BackrestEntity):
    """Entity attached to a backup plan device."""

    def __init__(
        self,
        coordinator: BackrestCoordinator,
        description: EntityDescription,
        plan_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, description)
        entry = coordinator.config_entry
        self.plan_id = plan_id
        self._attr_unique_id = f"{_instance_uid(entry)}_plan_{plan_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={plan_device_identifier(entry, plan_id)},
            name=f"Backrest {plan_id}",
            manufacturer=MANUFACTURER,
            model="Backup plan",
            configuration_url=str(coordinator.client.base_url),
            **_via_server(coordinator),
        )

    @property
    def plan_state(self) -> PlanState | None:
        """Return the current state of the plan."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.plans.get(self.plan_id)

    @property
    def available(self) -> bool:
        """Return whether the plan still exists and data is current."""
        return super().available and self.plan_state is not None


class BackrestRepoEntity(BackrestEntity):
    """Entity attached to a repository device."""

    def __init__(
        self,
        coordinator: BackrestCoordinator,
        description: EntityDescription,
        repo_id: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, description)
        entry = coordinator.config_entry
        self.repo_id = repo_id
        self._attr_unique_id = f"{_instance_uid(entry)}_repo_{repo_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={repo_device_identifier(entry, repo_id)},
            name=f"Backrest repository {repo_id}",
            manufacturer=MANUFACTURER,
            model="Restic repository",
            configuration_url=str(coordinator.client.base_url),
            **_via_server(coordinator),
        )

    @property
    def repo_state(self) -> RepoState | None:
        """Return the current state of the repository."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.repos.get(self.repo_id)

    @property
    def available(self) -> bool:
        """Return whether the repository still exists and data is current."""
        return super().available and self.repo_state is not None


@callback
def async_setup_dynamic_entities(
    entry: BackrestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    plan_entities: Callable[[str], Iterable[Entity]],
    repo_entities: Callable[[str], Iterable[Entity]] | None = None,
) -> None:
    """Add entities for plans and repositories, including ones created later.

    Entities of a plan are created once when the plan first appears. New
    backup runs only update existing entities. Removed plans are cleaned up
    together with their device (see __init__.py).
    """
    coordinator = entry.runtime_data
    known_plans: set[str] = set()
    known_repos: set[str] = set()

    @callback
    def _async_check() -> None:
        data = coordinator.data
        if data is None or not coordinator.last_update_success:
            return
        new_entities: list[Entity] = []
        known_plans.intersection_update(data.plans)
        for plan_id in sorted(data.plans.keys() - known_plans):
            known_plans.add(plan_id)
            new_entities.extend(plan_entities(plan_id))
        if repo_entities is not None:
            known_repos.intersection_update(data.repos)
            for repo_id in sorted(data.repos.keys() - known_repos):
                known_repos.add(repo_id)
                new_entities.extend(repo_entities(repo_id))
        if new_entities:
            async_add_entities(new_entities)

    _async_check()
    entry.async_on_unload(coordinator.async_add_listener(_async_check))
