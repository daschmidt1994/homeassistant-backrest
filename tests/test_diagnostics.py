"""Tests for diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.backrest.api import BackrestConnectionError

from .conftest import HOST, PASSWORD, REPO_PASSWORD, USERNAME, FakeBackrest


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client,
    setup_integration: MockConfigEntry,
    backrest: FakeBackrest,
) -> None:
    """Diagnostics contain state but no credentials or addresses."""
    backrest.error = BackrestConnectionError(f"Cannot connect to host {HOST}:9898")
    await setup_integration.runtime_data.async_refresh()

    result = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    dumped = json.dumps(result)
    for secret in (
        PASSWORD,
        USERNAME,
        HOST,
        REPO_PASSWORD,
        "gotify-secret-token",
        "ed25519.fake-key-1",
    ):
        assert secret not in dumped

    assert result["integration_version"] == "0.1.0"
    assert result["entry"]["credentials_configured"] is True
    assert result["connection"]["reachable"] is False
    assert "**REDACTED**" in result["connection"]["last_error"]
    assert result["connection"]["last_successful_fetch"] is not None
    assert result["api_features"]["summary_dashboard"] is True
    assert result["backrest"]["plan_count"] == 2
    assert result["backrest"]["plans"]["docs"]["last_failed"] is True
