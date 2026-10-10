"""Actions of the Backrest integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_PLAN_ID,
    DOMAIN,
    SERVICE_CANCEL_BACKUP,
    SERVICE_START_BACKUP,
)
from .coordinator import BackrestConfigEntry

PLAN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PLAN_ID): cv.string,
    }
)


def _get_entry(hass: HomeAssistant, call: ServiceCall) -> BackrestConfigEntry:
    entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    entry: BackrestConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"entry": entry.title},
        )
    return entry


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Backrest actions."""

    async def start_backup(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        await entry.runtime_data.async_start_backup(call.data[ATTR_PLAN_ID])

    async def cancel_backup(call: ServiceCall) -> None:
        entry = _get_entry(hass, call)
        await entry.runtime_data.async_cancel_backup(call.data[ATTR_PLAN_ID])

    hass.services.async_register(DOMAIN, SERVICE_START_BACKUP, start_backup, schema=PLAN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_BACKUP, cancel_backup, schema=PLAN_SCHEMA)
