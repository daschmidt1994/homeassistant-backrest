"""Tests for remote Backrest instances (multihost clients) of a server."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.backrest.api import BackrestApiError
from custom_components.backrest.const import DOMAIN
from custom_components.backrest.models import PeerConnection, RemoteInstance

from .conftest import REPO_PASSWORD, FakeBackrest

FIXTURES = Path(__file__).parent / "fixtures"
REMOTE = "sensor.backrest_netbird_vps_system"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _device(hass: HomeAssistant, entry: MockConfigEntry, identifier: str) -> dr.DeviceEntry | None:
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):  # HA 2026.10+
        return registry.async_get_device_by_identifier((DOMAIN, identifier), entry.entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, identifier)})


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


@pytest.fixture
def peer(backrest: FakeBackrest) -> FakeBackrest:
    """Add a connected remote instance with one successful backup."""
    now = dt_util.utcnow()
    backrest.add_peer("netbird-vps", heartbeat=now - timedelta(seconds=20))
    start = now - timedelta(hours=2)
    backrest.add_peer_backup(
        "netbird-vps", "system", "STATUS_SUCCESS", start, start + timedelta(seconds=65)
    )
    backrest.add_peer_backup("netbird-vps", "system", "STATUS_PENDING", now + timedelta(hours=10))
    return backrest


async def test_remote_plan_entities(
    hass: HomeAssistant, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Plans of remote instances get the plan sensors, the instance its own device."""
    await _setup(hass, config_entry)

    assert _state(hass, f"{REMOTE}_last_backup_status") == "success"
    assert float(_state(hass, f"{REMOTE}_last_backup_duration")) == pytest.approx(65 / 60)
    assert _state(hass, f"{REMOTE}_next_backup") != STATE_UNKNOWN
    assert _state(hass, "binary_sensor.backrest_netbird_vps_system_backup_running") == STATE_OFF
    assert _state(hass, "binary_sensor.backrest_netbird_vps_system_last_backup_failed") == STATE_OFF
    assert _state(hass, "binary_sensor.backrest_netbird_vps_connected") == STATE_ON
    assert _state(hass, "sensor.backrest_netbird_vps_last_contact") != STATE_UNKNOWN
    assert _state(hass, "sensor.backrest_netbird_vps_connection") == "connected"
    # The server's summary dashboard does not cover remote plans.
    assert hass.states.get(f"{REMOTE}_successful_backups_30_days") is None
    # Backups of remote instances cannot be started through the API.
    assert hass.states.get("button.backrest_netbird_vps_system_start_backup") is None
    assert hass.states.get("button.backrest_nas_start_backup") is not None
    # Local plans are unaffected; the server counts the remote plan too.
    assert _state(hass, "sensor.backrest_nas_last_backup_status") == "success"
    assert _state(hass, "sensor.backrest_test_instance_backup_plans") == "3"

    plan_device = _device(hass, config_entry, "ed25519.fake-key-1_plan_netbird-vps/system")
    assert plan_device is not None
    assert plan_device.name == "Backrest netbird-vps system"
    assert plan_device.model == "Backup plan (remote)"
    remote_device = _device(hass, config_entry, "ed25519.fake-key-1_remote_netbird-vps")
    assert remote_device is not None
    assert remote_device.model == "Remote Backrest instance"


async def test_remote_running_and_failed(
    hass: HomeAssistant, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Running and failed remote backups show up, failures count as a server problem."""
    entry = await _setup(hass, config_entry)
    now = dt_util.utcnow()
    peer.add_peer_backup(
        "netbird-vps", "system", "STATUS_INPROGRESS", now - timedelta(minutes=1), percent_done=0.42
    )
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor.backrest_netbird_vps_system_backup_running") == STATE_ON
    assert _state(hass, f"{REMOTE}_backup_progress") == "42.0"

    peer.peers["netbird-vps"]["operations"]["system"].pop()
    peer.add_peer_backup("netbird-vps", "system", "STATUS_ERROR", now - timedelta(minutes=1), now)
    await _refresh(hass, entry)
    assert _state(hass, "binary_sensor.backrest_netbird_vps_system_last_backup_failed") == STATE_ON
    problem = hass.states.get("binary_sensor.backrest_test_instance_problem")
    assert problem.state == STATE_ON
    assert "netbird-vps/system" in problem.attributes["failed_plans"]


async def test_remote_plan_actions_rejected(
    hass: HomeAssistant, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Starting or cancelling a remote backup is refused without calling Backrest."""
    entry = await _setup(hass, config_entry)
    coordinator = entry.runtime_data
    for action in (coordinator.async_start_backup, coordinator.async_cancel_backup):
        with pytest.raises(ServiceValidationError) as err:
            await action("netbird-vps/system")
        assert err.value.translation_key == "remote_plan_action"
    assert peer.backup_calls == []
    assert peer.cancel_calls == []


async def test_remote_disconnected(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """A disconnected instance stays visible with its last known plans."""
    backrest.add_peer("pangolin-vps", state="CONNECTION_STATE_RETRY_WAIT")
    await _setup(hass, config_entry)
    assert _state(hass, "binary_sensor.backrest_pangolin_vps_connected") == STATE_OFF
    assert _state(hass, "sensor.backrest_pangolin_vps_connection") == "retry_wait"
    assert _state(hass, "sensor.backrest_pangolin_vps_last_contact") == STATE_UNKNOWN
    assert _state(hass, "sensor.backrest_pangolin_vps_system_last_backup_status") == STATE_UNKNOWN


async def test_multihost_unsupported(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Servers without multihost sync work as before and are not asked again."""
    backrest.multihost_unsupported = True
    entry = await _setup(hass, config_entry)
    assert entry.runtime_data.multihost_supported is False
    assert entry.runtime_data.data.remotes == {}
    assert _state(hass, "sensor.backrest_test_instance_backup_plans") == "2"

    backrest.multihost_unsupported = False
    backrest.add_peer("netbird-vps")
    await _refresh(hass, entry)
    assert entry.runtime_data.data.remotes == {}


async def test_peer_error_keeps_last_state(
    hass: HomeAssistant, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """A failing peer request keeps the last known remote instances."""
    entry = await _setup(hass, config_entry)
    peer.peer_states_error = BackrestApiError("internal", "sync manager busy")
    await _refresh(hass, entry)
    assert entry.runtime_data.last_update_success
    assert _state(hass, f"{REMOTE}_last_backup_status") == "success"
    assert _state(hass, "binary_sensor.backrest_netbird_vps_connected") == STATE_ON


async def test_removed_remote_instance(
    hass: HomeAssistant, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Removing an instance from the server removes its devices."""
    entry = await _setup(hass, config_entry)
    remote_id = "ed25519.fake-key-1_remote_netbird-vps"
    plan_id = "ed25519.fake-key-1_plan_netbird-vps/system"
    assert _device(hass, entry, remote_id) is not None

    del peer.peers["netbird-vps"]
    await _refresh(hass, entry)
    assert _device(hass, entry, remote_id) is None
    assert _device(hass, entry, plan_id) is None
    state = hass.states.get(f"{REMOTE}_last_backup_status")
    assert state is None or state.state == STATE_UNAVAILABLE


async def test_remote_diagnostics_without_secrets(
    hass: HomeAssistant, hass_client, peer: FakeBackrest, config_entry: MockConfigEntry
) -> None:
    """Remote configs (with repository passwords) and key IDs never reach diagnostics."""
    entry = await _setup(hass, config_entry)
    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    dumped = json.dumps(result)
    assert REPO_PASSWORD not in dumped
    assert "ed25519.netbird-vps" not in dumped
    assert "backup:8100" not in dumped
    assert result["backrest"]["remote_instances"]["netbird-vps"]["plans"] == ["system"]
    assert result["api_features"]["multihost"] is True


def test_remote_instance_from_backrest_1_14() -> None:
    """A PeerState captured from Backrest 1.14.1 is parsed without keeping secrets."""
    raw = json.loads((FIXTURES / "peer_states_1_14.json").read_text())
    remote = RemoteInstance.from_api(raw[0])
    assert remote is not None
    assert remote.instance_id == "netbird-vps"
    assert remote.connection is PeerConnection.CONNECTED
    assert remote.connected
    assert remote.last_heartbeat is not None
    assert [plan.id for plan in remote.plans] == ["netbird-system"]
    plan = remote.plans[0]
    assert plan.instance == "netbird-vps"
    assert plan.is_remote
    assert plan.schedule.kind == "cron"
    assert "fixture-repo-password" not in repr(remote)
    assert "fixture-rest-password" not in repr(remote)


def test_remote_instance_without_config() -> None:
    """Without config permission only plan IDs are known."""
    remote = RemoteInstance.from_api(
        {
            "peerInstanceId": "laptop",
            "state": "CONNECTION_STATE_SOMETHING_NEW",
            "knownPlans": [{"id": "home"}, {"id": ""}],
        }
    )
    assert remote is not None
    assert remote.connection is PeerConnection.UNKNOWN
    assert [(plan.id, plan.schedule.kind) for plan in remote.plans] == [("home", "unknown")]
    assert RemoteInstance.from_api({"peerInstanceId": ""}) is None
    assert RemoteInstance.from_api({"peerInstanceId": "a/b"}) is None
