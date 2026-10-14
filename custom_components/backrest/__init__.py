"""The Backrest integration."""

from __future__ import annotations

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import BackrestClient, build_base_url
from .const import CONF_EVENT_STREAM, DEFAULT_EVENT_STREAM, DOMAIN
from .coordinator import BackrestConfigEntry, BackrestCoordinator
from .entity import (
    plan_device_identifier,
    remote_device_identifier,
    repo_device_identifier,
    server_device_info,
)
from .events import BackrestEventListener
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Backrest actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BackrestConfigEntry) -> bool:
    """Set up a Backrest instance from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True))
    client = BackrestClient(
        session,
        build_base_url(entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_SSL]),
        entry.data.get(CONF_USERNAME),
        entry.data.get(CONF_PASSWORD),
    )
    coordinator = BackrestCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Register the server device first so plan and repository devices can
    # link to it.
    server_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **server_device_info(entry, coordinator)
    )
    coordinator.server_device_id = server_device.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(
        coordinator.async_add_listener(lambda: _async_remove_stale_devices(hass, entry))
    )
    _async_remove_stale_devices(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    if entry.options.get(CONF_EVENT_STREAM, DEFAULT_EVENT_STREAM):
        listener = BackrestEventListener(coordinator, client)
        entry.async_create_background_task(
            hass, listener.async_run(), name=f"backrest event stream {entry.title}"
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BackrestConfigEntry) -> bool:
    """Unload a config entry. Background tasks are cancelled by Home Assistant."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: BackrestConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _expected_identifiers(entry: BackrestConfigEntry) -> set[tuple[str, str]]:
    coordinator = entry.runtime_data
    data = coordinator.data
    identifiers = {(DOMAIN, entry.unique_id or entry.entry_id)}
    identifiers.update(plan_device_identifier(entry, plan_id) for plan_id in data.plans)
    identifiers.update(repo_device_identifier(entry, repo_id) for repo_id in data.repos)
    identifiers.update(remote_device_identifier(entry, instance) for instance in data.remotes)
    return identifiers


@callback
def _async_remove_stale_devices(hass: HomeAssistant, entry: BackrestConfigEntry) -> None:
    """Remove devices of plans and repositories deleted in Backrest."""
    coordinator = entry.runtime_data
    if not coordinator.last_update_success or coordinator.data is None:
        return
    expected = _expected_identifiers(entry)
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if not device.identifiers & expected:
            device_registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: BackrestConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow manual removal of devices that no longer exist in Backrest."""
    if entry.runtime_data.data is None:
        return True
    return not device.identifiers & _expected_identifiers(entry)
