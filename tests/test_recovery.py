"""Discovery after outages and restarts."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.api import BackrestConnectionError

from .conftest import FakeBackrest


async def test_changes_during_outage_are_picked_up(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Repos and plans created while Backrest was unreachable appear on recovery."""
    coordinator = setup_integration.runtime_data
    backrest.error = BackrestConnectionError("Error calling GetConfig: ClientConnectorError")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    # Nothing is deleted while unreachable.
    assert hass.states.get("button.backrest_nas_start_backup") is not None

    backrest.add_repo("offsite")
    backrest.add_plan("cloud", repo="offsite")
    await coordinator.async_refresh()  # still offline
    await hass.async_block_till_done()
    assert hass.states.get("button.backrest_cloud_start_backup") is None

    backrest.error = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("button.backrest_cloud_start_backup") is not None
    assert hass.states.get("sensor.backrest_repository_offsite_backup_plans").state == "1"
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "success"


async def test_offline_at_startup_then_new_repo(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Setup retries while offline and discovers everything once reachable."""
    backrest.error = BackrestConnectionError("Error calling GetConfig: ClientConnectorError")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    backrest.add_repo("offsite")
    backrest.add_plan("cloud", repo="offsite")
    backrest.error = None
    await hass.config_entries.async_reload(config_entry.entry_id)  # what the retry does
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get("button.backrest_cloud_start_backup") is not None
    assert hass.states.get("sensor.backrest_repository_offsite_backup_plans") is not None


async def test_restart_keeps_ids_and_adds_new_repo(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A restart (reload) keeps entity IDs and picks up repos added meanwhile."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    backrest.add_repo("offsite")
    assert await hass.config_entries.async_setup(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("button.backrest_nas_start_backup") is not None
    assert hass.states.get("button.backrest_nas_start_backup_2") is None
    assert hass.states.get("sensor.backrest_repository_offsite_backup_plans").state == "0"
