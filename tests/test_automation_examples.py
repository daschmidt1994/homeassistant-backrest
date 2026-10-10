"""Run the documented example automations against the integration."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.backrest.api import BackrestConnectionError

from .conftest import HOST, FakeBackrest, make_entry

EXAMPLES = Path(__file__).parent.parent / "examples" / "automations.yaml"

# The nightly time trigger keeps a timer scheduled after the test.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


@pytest.fixture
async def example_setup(
    hass: HomeAssistant, backrest_servers: dict[str, FakeBackrest]
) -> tuple[FakeBackrest, MockConfigEntry, list]:
    """Set up 'homeserver' with plan 'nas' and load the example automations."""
    now = dt_util.utcnow()
    server = FakeBackrest(instance="homeserver", key_id="ed25519.home")
    server.add_plan("nas", schedule={"maxFrequencyDays": 1})
    server.add_backup("nas", "STATUS_SUCCESS", now - timedelta(hours=3), now - timedelta(hours=3))
    backrest_servers[HOST] = server
    entry = make_entry(unique_id="ed25519.home", title="Backrest homeserver")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    notifications = async_mock_service(hass, "notify", "notify")
    assert await async_setup_component(
        hass, AUTOMATION_DOMAIN, {AUTOMATION_DOMAIN: load_yaml(EXAMPLES)}
    )
    await hass.async_block_till_done()
    return server, entry, notifications


async def test_example_entities_exist(
    hass: HomeAssistant, example_setup: tuple[FakeBackrest, MockConfigEntry, list]
) -> None:
    """Every entity referenced by the examples exists."""
    for entity_id in (
        "binary_sensor.backrest_nas_last_backup_failed",
        "binary_sensor.backrest_nas_backup_overdue",
        "binary_sensor.backrest_nas_backup_running",
        "binary_sensor.backrest_homeserver_reachable",
        "sensor.backrest_nas_last_error_message",
        "sensor.backrest_nas_last_successful_backup",
        "button.backrest_nas_start_backup",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    automations = hass.states.async_all(AUTOMATION_DOMAIN)
    assert len(automations) == len(load_yaml(EXAMPLES))
    assert all(state.state == "on" for state in automations)


async def test_example_backup_failed(
    hass: HomeAssistant, example_setup: tuple[FakeBackrest, MockConfigEntry, list]
) -> None:
    """A failed backup sends a notification with the error message."""
    server, entry, notifications = example_setup
    now = dt_util.utcnow()
    server.add_backup("nas", "STATUS_ERROR", now - timedelta(minutes=1), now, message="disk full")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert len(notifications) == 1
    assert notifications[0].data["title"] == "Backup failed"
    assert "disk full" in notifications[0].data["message"]


async def test_example_overdue_and_48h(
    hass: HomeAssistant, example_setup: tuple[FakeBackrest, MockConfigEntry, list]
) -> None:
    """Overdue (schedule based) and the custom 48 hour threshold both notify."""
    server, entry, notifications = example_setup
    now = dt_util.utcnow()
    # The daily plan's last success is now 31 hours old: overdue (> 1.25 days).
    server.operations["nas"].clear()
    server.add_backup("nas", "STATUS_SUCCESS", now - timedelta(hours=31), now - timedelta(hours=31))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert [call.data["title"] for call in notifications] == ["Backup overdue"]
    assert "never" not in notifications[0].data["message"]

    server.operations["nas"].clear()
    server.add_backup("nas", "STATUS_SUCCESS", now - timedelta(hours=49), now - timedelta(hours=49))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert [call.data["title"] for call in notifications] == [
        "Backup overdue",
        "Backup older than 48 hours",
    ]


async def test_example_nightly_backup(
    hass: HomeAssistant, example_setup: tuple[FakeBackrest, MockConfigEntry, list]
) -> None:
    """The time trigger presses the start button."""
    server, _, _ = example_setup
    with patch("custom_components.backrest.coordinator.START_CONFIRM_TIMEOUT", 0.01):
        local_now = dt_util.now()
        target = local_now.replace(hour=22, minute=0, second=0, microsecond=0)
        if target <= local_now:
            target += timedelta(days=1)
        target += timedelta(seconds=1)
        async_fire_time_changed(hass, target)
        await hass.async_block_till_done()
    assert server.backup_calls == ["nas"]
    server.backup_release.set()
    await hass.async_block_till_done()


async def test_example_offline(
    hass: HomeAssistant, example_setup: tuple[FakeBackrest, MockConfigEntry, list]
) -> None:
    """Five minutes of unreachability send a notification."""
    server, entry, notifications = example_setup
    server.error = BackrestConnectionError("Error calling GetConfig: ClientConnectorError")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.backrest_homeserver_reachable").state == "off"
    assert notifications == []

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    assert [call.data["title"] for call in notifications] == ["Backrest offline"]
