"""Buttons for the Backrest integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    REPO_TASK_CHECK,
    REPO_TASK_PRUNE,
    BackrestConfigEntry,
    BackrestCoordinator,
)
from .entity import BackrestPlanEntity, BackrestRepoEntity, async_setup_dynamic_entities

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class BackrestButtonDescription(ButtonEntityDescription):
    """Describes a Backrest button."""

    press_fn: Callable[[BackrestCoordinator, str], Awaitable[None]]


PLAN_BUTTONS: tuple[BackrestButtonDescription, ...] = (
    BackrestButtonDescription(
        key="start_backup",
        translation_key="start_backup",
        press_fn=lambda coordinator, plan_id: coordinator.async_start_backup(plan_id),
    ),
    BackrestButtonDescription(
        key="cancel_backup",
        translation_key="cancel_backup",
        press_fn=lambda coordinator, plan_id: coordinator.async_cancel_backup(plan_id),
    ),
)

REPO_BUTTONS: tuple[BackrestButtonDescription, ...] = (
    BackrestButtonDescription(
        key="check",
        translation_key="check_repository",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=lambda coordinator, repo_id: coordinator.async_run_repo_task(
            repo_id, REPO_TASK_CHECK
        ),
    ),
    BackrestButtonDescription(
        key="prune",
        translation_key="prune_repository",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        press_fn=lambda coordinator, repo_id: coordinator.async_run_repo_task(
            repo_id, REPO_TASK_PRUNE
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BackrestConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Backrest buttons."""
    coordinator = entry.runtime_data
    async_setup_dynamic_entities(
        entry,
        async_add_entities,
        lambda plan_id: (
            []
            # Backups of remote instances cannot be started or cancelled via the API.
            if coordinator.data.plans[plan_id].plan.is_remote
            else [
                BackrestPlanButton(coordinator, description, plan_id)
                for description in PLAN_BUTTONS
            ]
        ),
        lambda repo_id: [
            BackrestRepoButton(coordinator, description, repo_id) for description in REPO_BUTTONS
        ],
    )


class BackrestPlanButton(BackrestPlanEntity, ButtonEntity):
    """Button acting on a backup plan."""

    entity_description: BackrestButtonDescription

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.entity_description.press_fn(self.coordinator, self.plan_id)


class BackrestRepoButton(BackrestRepoEntity, ButtonEntity):
    """Button acting on a repository."""

    entity_description: BackrestButtonDescription

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.entity_description.press_fn(self.coordinator, self.repo_id)
