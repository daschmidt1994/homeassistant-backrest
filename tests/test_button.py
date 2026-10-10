"""Tests for starting and cancelling backups."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.api import BackrestApiError, BackrestConnectionError
from custom_components.backrest.const import DOMAIN

from .conftest import FakeBackrest


@pytest.fixture(autouse=True)
def short_confirm_timeout() -> Generator[None]:
    """Do not wait 10 seconds for start confirmations in tests."""
    with patch("custom_components.backrest.coordinator.START_CONFIRM_TIMEOUT", 0.05):
        yield


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_start_backup(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Pressing the button starts the backup without reporting success."""
    before = hass.states.get("sensor.backrest_docs_last_backup_status").state
    await _press(hass, "button.backrest_docs_start_backup")
    assert backrest.backup_calls == ["docs"]
    coordinator = setup_integration.runtime_data
    assert coordinator.pending_requests == ["backup:docs"]
    # Accepting the request is not a successful backup.
    await hass.async_block_till_done()
    assert hass.states.get("sensor.backrest_docs_last_backup_status").state == before
    assert hass.states.get("binary_sensor.backrest_docs_last_backup_failed").state == STATE_ON

    # A second press while the request is in flight is rejected.
    with pytest.raises(ServiceValidationError) as err:
        await _press(hass, "button.backrest_docs_start_backup")
    assert err.value.translation_key == "backup_already_running"
    assert backrest.backup_calls == ["docs"]

    # Backrest finishes the backup: the request returns and a refresh follows.
    now = dt_util.utcnow()
    backrest.add_backup("docs", "STATUS_SUCCESS", now - timedelta(seconds=30), now)
    backrest.backup_release.set()
    await hass.async_block_till_done()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.pending_requests == []
    assert hass.states.get("sensor.backrest_docs_last_backup_status").state == "success"


async def test_start_rejected_while_running(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A backup started elsewhere (e.g. by schedule) blocks a manual start."""
    backrest.add_backup("nas", "STATUS_INPROGRESS", dt_util.utcnow(), percent_done=0.1)
    await setup_integration.runtime_data.async_refresh()
    with pytest.raises(ServiceValidationError) as err:
        await _press(hass, "button.backrest_nas_start_backup")
    assert err.value.translation_key == "backup_already_running"
    assert backrest.backup_calls == []


async def test_start_rejected_while_queued(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A backup waiting in Backrest's queue also blocks a manual start."""
    backrest.add_backup("nas", "STATUS_PENDING", dt_util.utcnow() - timedelta(seconds=5))
    await setup_integration.runtime_data.async_refresh()
    with pytest.raises(ServiceValidationError):
        await _press(hass, "button.backrest_nas_start_backup")


async def test_start_fails_fast(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Errors returned right away are shown to the user."""
    backrest.backup_error = BackrestApiError("unknown", "restic: repository is locked")
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, "button.backrest_nas_start_backup")
    assert err.value.translation_key == "action_failed"
    assert err.value.translation_placeholders["error"] == "unknown: restic: repository is locked"

    backrest.backup_error = BackrestConnectionError("Error calling Backup: ClientConnectorError")
    with pytest.raises(HomeAssistantError) as err:
        await _press(hass, "button.backrest_nas_start_backup")
    assert err.value.translation_key == "cannot_connect"


async def test_cancel_backup(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Cancelling sends the running operation's ID to Backrest."""
    with pytest.raises(ServiceValidationError) as err:
        await _press(hass, "button.backrest_nas_cancel_backup")
    assert err.value.translation_key == "no_backup_running"

    op_id = backrest.add_backup("nas", "STATUS_INPROGRESS", dt_util.utcnow())
    await setup_integration.runtime_data.async_refresh()
    await _press(hass, "button.backrest_nas_cancel_backup")
    assert backrest.cancel_calls == [op_id]


async def test_start_backup_action(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """The backrest.start_backup action starts a plan by ID."""
    await hass.services.async_call(
        DOMAIN,
        "start_backup",
        {"config_entry_id": setup_integration.entry_id, "plan_id": "nas"},
        blocking=True,
    )
    assert backrest.backup_calls == ["nas"]

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "start_backup",
            {"config_entry_id": setup_integration.entry_id, "plan_id": "unknown"},
            blocking=True,
        )
    assert err.value.translation_key == "plan_not_found"

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "start_backup",
            {"config_entry_id": "does-not-exist", "plan_id": "nas"},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_found"
    backrest.backup_release.set()
    await hass.async_block_till_done()


async def test_cancel_backup_action(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """The backrest.cancel_backup action cancels the running backup."""
    op_id = backrest.add_backup("docs", "STATUS_INPROGRESS", dt_util.utcnow())
    await setup_integration.runtime_data.async_refresh()
    await hass.services.async_call(
        DOMAIN,
        "cancel_backup",
        {"config_entry_id": setup_integration.entry_id, "plan_id": "docs"},
        blocking=True,
    )
    assert backrest.cancel_calls == [op_id]


async def test_action_on_unloaded_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Actions report unloaded entries."""
    await hass.config_entries.async_unload(setup_integration.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "start_backup",
            {"config_entry_id": setup_integration.entry_id, "plan_id": "nas"},
            blocking=True,
        )
    assert err.value.translation_key == "entry_not_loaded"


async def test_unload_cancels_pending_backup_request(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Unloading cancels the waiting request (the backup keeps running in Backrest)."""
    await _press(hass, "button.backrest_nas_start_backup")
    coordinator = setup_integration.runtime_data
    assert coordinator.pending_requests == ["backup:nas"]
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert coordinator.pending_requests == []


async def test_repository_buttons(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Repository maintenance buttons are disabled by default and work when enabled."""
    entity_registry = er.async_get(hass)
    entity_id = "button.backrest_repository_local_repo_check_repository"
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    entity_registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    await _press(hass, entity_id)
    assert backrest.repo_task_calls == [("local-repo", "TASK_CHECK")]
