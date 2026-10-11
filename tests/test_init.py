"""Tests for setup, entities and dynamic discovery."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.api import BackrestAuthError, BackrestTimeoutError
from custom_components.backrest.const import DOMAIN

from .conftest import FakeBackrest, make_entry


def _device(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> dr.DeviceEntry | None:
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):  # HA 2026.10+
        return registry.async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, identifier)})


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_setup_creates_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A successful setup creates server, plan and repository entities."""
    assert setup_integration.state is ConfigEntryState.LOADED

    assert hass.states.get("binary_sensor.backrest_test_instance_reachable").state == STATE_ON
    assert hass.states.get("sensor.backrest_test_instance_backup_plans").state == "2"
    assert hass.states.get("sensor.backrest_test_instance_repositories").state == "1"
    assert hass.states.get("sensor.backrest_test_instance_running_backups").state == "0"
    assert (
        hass.states.get("sensor.backrest_test_instance_plans_with_successful_last_backup").state
        == "1"
    )
    failed = hass.states.get("sensor.backrest_test_instance_plans_with_failed_last_backup")
    assert failed.state == "1"
    assert failed.attributes["plans"] == ["docs"]
    assert (
        hass.states.get("sensor.backrest_test_instance_plans_without_successful_backup").state
        == "1"
    )

    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "success"
    assert hass.states.get("sensor.backrest_nas_last_backup_duration").state == "2.5"
    assert hass.states.get("sensor.backrest_nas_successful_backups_30_days").state == "2"
    assert hass.states.get("sensor.backrest_nas_failed_backups_30_days").state == "0"
    assert hass.states.get("sensor.backrest_nas_next_backup").state != STATE_UNKNOWN
    assert hass.states.get("sensor.backrest_nas_backup_progress").state == "0"
    assert hass.states.get("binary_sensor.backrest_nas_backup_running").state == STATE_OFF
    assert hass.states.get("binary_sensor.backrest_nas_last_backup_failed").state == STATE_OFF
    assert hass.states.get("binary_sensor.backrest_nas_backup_overdue").state == STATE_OFF
    assert hass.states.get("button.backrest_nas_start_backup") is not None

    assert hass.states.get("sensor.backrest_docs_last_backup_status").state == "error"
    assert (
        hass.states.get("sensor.backrest_docs_last_error_message").state
        == "repository is locked by another process"
    )
    assert hass.states.get("binary_sensor.backrest_docs_last_backup_failed").state == STATE_ON
    assert hass.states.get("binary_sensor.backrest_test_instance_problem").state == STATE_ON

    assert hass.states.get("sensor.backrest_repository_local_repo_backup_plans").state == "2"


async def test_device_hierarchy_and_unique_ids(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Plans and repositories are devices below the server device."""
    server = _device(hass, setup_integration, "ed25519.fake-key-1")
    plan = _device(hass, setup_integration, "ed25519.fake-key-1_plan_nas")
    repo = _device(hass, setup_integration, "ed25519.fake-key-1_repo_local-repo")
    assert server is not None
    assert plan is not None
    assert repo is not None
    assert plan.via_device_id == server.id
    assert repo.via_device_id == server.id
    assert plan.name == "Backrest nas"

    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get("button.backrest_nas_start_backup")
    assert entry.unique_id == "ed25519.fake-key-1_plan_nas_start_backup"
    assert entry.device_id == plan.id


async def test_new_plan_is_discovered_without_restart(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A plan created in Backrest gets entities on the next refresh."""
    assert hass.states.get("button.backrest_offsite_start_backup") is None
    backrest.add_plan("offsite", schedule={"maxFrequencyDays": 1})
    await _refresh(hass, setup_integration)

    assert hass.states.get("button.backrest_offsite_start_backup") is not None
    assert hass.states.get("sensor.backrest_offsite_last_backup_status").state == STATE_UNKNOWN
    assert hass.states.get("binary_sensor.backrest_offsite_backup_overdue").state == STATE_OFF
    assert hass.states.get("sensor.backrest_test_instance_backup_plans").state == "3"
    assert _device(hass, setup_integration, "ed25519.fake-key-1_plan_offsite") is not None


async def test_new_backup_runs_update_existing_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """New executions update sensors and do not create entities."""
    entity_registry = er.async_get(hass)
    entity_count = len(
        er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)
    )

    now = dt_util.utcnow()
    op_id = backrest.add_backup("docs", "STATUS_INPROGRESS", now, percent_done=0.42)
    await _refresh(hass, setup_integration)
    assert hass.states.get("sensor.backrest_docs_last_backup_status").state == "in_progress"
    assert hass.states.get("binary_sensor.backrest_docs_backup_running").state == STATE_ON
    assert hass.states.get("sensor.backrest_docs_backup_progress").state == "42.0"
    assert hass.states.get("sensor.backrest_test_instance_running_backups").state == "1"
    assert hass.states.get("binary_sensor.backrest_test_instance_backup_running").state == STATE_ON
    # The previous run still failed; a running backup is no success.
    assert hass.states.get("binary_sensor.backrest_docs_last_backup_failed").state == STATE_ON
    assert hass.states.get("sensor.backrest_docs_last_successful_backup").state == STATE_UNKNOWN

    backrest.set_status(op_id, "STATUS_SUCCESS", end=now + timedelta(minutes=1))
    await _refresh(hass, setup_integration)
    assert hass.states.get("sensor.backrest_docs_last_backup_status").state == "success"
    assert hass.states.get("binary_sensor.backrest_docs_backup_running").state == STATE_OFF
    assert hass.states.get("binary_sensor.backrest_docs_last_backup_failed").state == STATE_OFF
    assert hass.states.get("sensor.backrest_docs_last_successful_backup").state != STATE_UNKNOWN
    changed = dt_util.parse_datetime(
        hass.states.get("sensor.backrest_docs_last_status_change").state
    )
    assert abs(changed - (now + timedelta(minutes=1))) < timedelta(seconds=1)
    assert hass.states.get("binary_sensor.backrest_test_instance_problem").state == STATE_OFF
    assert (
        len(er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id))
        == entity_count
    )


async def test_failed_backup_is_reported(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """A failed run flips the problem sensors and records the message."""
    now = dt_util.utcnow()
    backrest.add_backup("nas", "STATUS_ERROR", now - timedelta(minutes=5), now, message="disk full")
    await _refresh(hass, setup_integration)
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "error"
    assert hass.states.get("binary_sensor.backrest_nas_last_backup_failed").state == STATE_ON
    error = hass.states.get("sensor.backrest_nas_last_error_message")
    assert error.state == "disk full"
    assert "occurred_at" in error.attributes
    problem = hass.states.get("binary_sensor.backrest_test_instance_problem")
    assert problem.state == STATE_ON
    assert problem.attributes["failed_plans"] == ["docs", "nas"]
    # Last success is still the earlier successful run.
    assert hass.states.get("sensor.backrest_nas_last_successful_backup").state != STATE_UNKNOWN


async def test_deleted_plan_is_removed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Deleting a plan in Backrest removes its device and entities."""
    identifier = "ed25519.fake-key-1_plan_docs"
    assert _device(hass, setup_integration, identifier) is not None

    backrest.remove_plan("docs")
    await _refresh(hass, setup_integration)

    assert _device(hass, setup_integration, identifier) is None
    assert er.async_get(hass).async_get("button.backrest_docs_start_backup") is None
    assert hass.states.get("button.backrest_docs_start_backup") is None
    assert hass.states.get("sensor.backrest_test_instance_backup_plans").state == "1"

    # Re-creating the plan with the same ID restores the same entity IDs.
    backrest.add_plan("docs")
    await _refresh(hass, setup_integration)
    assert hass.states.get("button.backrest_docs_start_backup") is not None


async def test_unreachable_server(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Connection loss makes entities unavailable and recovers afterwards."""
    backrest.error = BackrestTimeoutError("Timeout calling GetConfig")
    await _refresh(hass, setup_integration)

    assert hass.states.get("binary_sensor.backrest_test_instance_reachable").state == STATE_OFF
    assert hass.states.get("binary_sensor.backrest_test_instance_problem").state == STATE_ON
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == STATE_UNAVAILABLE
    assert setup_integration.state is ConfigEntryState.LOADED

    backrest.error = None
    await _refresh(hass, setup_integration)
    assert hass.states.get("binary_sensor.backrest_test_instance_reachable").state == STATE_ON
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "success"


async def test_setup_retries_when_unreachable(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """An unreachable server at startup leads to a setup retry."""
    backrest.error = BackrestTimeoutError("Timeout calling GetConfig")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, backrest: FakeBackrest
) -> None:
    """Rejected credentials trigger the reauthentication flow."""
    backrest.error = BackrestAuthError("invalid password")
    await _refresh(hass, setup_integration)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == SOURCE_REAUTH for flow in flows)


async def test_summary_unsupported(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Older Backrest versions without summary API still work."""
    backrest.summary_supported = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "success"
    assert hass.states.get("sensor.backrest_nas_successful_backups_30_days") is None
    assert hass.states.get("sensor.backrest_nas_next_backup").state != STATE_UNKNOWN


async def test_multiple_instances(
    hass: HomeAssistant, backrest_servers: dict[str, FakeBackrest]
) -> None:
    """Two Backrest instances are set up independently."""
    now = dt_util.utcnow()
    first = FakeBackrest(instance="nas-box", key_id="ed25519.key-a")
    first.add_plan("photos")
    first.add_backup("photos", "STATUS_SUCCESS", now - timedelta(hours=1), now)
    second = FakeBackrest(instance="laptop", key_id="ed25519.key-b")
    second.add_plan("photos")
    second.add_backup("photos", "STATUS_ERROR", now - timedelta(hours=1), now, message="boom")
    backrest_servers["nas.local"] = first
    backrest_servers["laptop.local"] = second

    entry_a = make_entry("nas.local", "ed25519.key-a", "Backrest nas-box")
    entry_b = make_entry("laptop.local", "ed25519.key-b", "Backrest laptop")
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    # Setting up the domain loads both entries.
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    assert entry_a.state is ConfigEntryState.LOADED
    assert entry_b.state is ConfigEntryState.LOADED
    entity_registry = er.async_get(hass)
    a_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "ed25519.key-a_plan_photos_last_backup_status"
    )
    b_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, "ed25519.key-b_plan_photos_last_backup_status"
    )
    assert a_id != b_id
    assert hass.states.get(a_id).state == "success"
    assert hass.states.get(b_id).state == "error"

    # Unloading one instance leaves the other untouched.
    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(a_id).state == "success"
    assert hass.states.get(b_id).state == STATE_UNAVAILABLE


async def test_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The entry unloads cleanly."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == STATE_UNAVAILABLE


async def test_secrets_are_not_kept(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Repository passwords from GetConfig never end up in coordinator data."""
    data = setup_integration.runtime_data.data
    assert "super-secret-repo-password" not in repr(data)
    assert "gotify-secret-token" not in repr(data)


@pytest.mark.parametrize(
    ("options", "expected"),
    [({}, 60), ({"scan_interval": 300, "event_stream": False}, 300)],
)
async def test_scan_interval_option(
    hass: HomeAssistant, backrest: FakeBackrest, options: dict, expected: int
) -> None:
    """The polling interval follows the options."""
    entry = make_entry(options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval == timedelta(seconds=expected)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
