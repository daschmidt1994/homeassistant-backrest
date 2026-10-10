"""Tests for the Backrest config flow."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.api import (
    BackrestResponseError,
    BackrestSslError,
    BackrestTimeoutError,
)
from custom_components.backrest.const import CONF_EVENT_STREAM, DOMAIN

from .conftest import HOST, PASSWORD, USERNAME, FakeBackrest, make_entry

USER_INPUT = {
    CONF_HOST: HOST,
    CONF_PORT: 9898,
    CONF_SSL: False,
    CONF_VERIFY_SSL: True,
    CONF_USERNAME: USERNAME,
    CONF_PASSWORD: PASSWORD,
}


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_flow(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """A valid server and credentials create an entry."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Backrest test-instance"
    assert result["data"] == USER_INPUT
    assert result["result"].unique_id == "ed25519.fake-key-1"
    await hass.async_block_till_done()


async def test_user_flow_without_auth(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """Credentials are optional when Backrest authentication is disabled."""
    backrest.auth_enabled = False
    result = await _start(hass)
    user_input = {k: v for k, v in USER_INPUT.items() if k not in (CONF_USERNAME, CONF_PASSWORD)}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**user_input, CONF_USERNAME: "", CONF_PASSWORD: ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_USERNAME not in result["data"]
    assert CONF_PASSWORD not in result["data"]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({**USER_INPUT, CONF_PASSWORD: "wrong"}, "invalid_auth"),
        (
            {k: v for k, v in USER_INPUT.items() if k not in (CONF_USERNAME, CONF_PASSWORD)},
            "auth_required",
        ),
        ({**USER_INPUT, CONF_HOST: "unreachable.local"}, "cannot_connect"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, backrest: FakeBackrest, user_input: dict, error: str
) -> None:
    """Wrong credentials and unreachable servers show errors and can be fixed."""
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (BackrestTimeoutError("Timeout calling GetConfig"), "cannot_connect"),
        (BackrestSslError("TLS certificate rejected"), "ssl_error"),
        (BackrestResponseError("GetConfig did not return JSON"), "invalid_response"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_server_errors(
    hass: HomeAssistant, backrest: FakeBackrest, exception: Exception, error: str
) -> None:
    """Timeouts, TLS problems and non-Backrest servers are reported."""
    backrest.error = exception
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_user_flow_already_configured(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """The same Backrest instance cannot be added twice, even via another address."""
    entry = make_entry(host="192.0.2.10")
    entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # The stored address is updated to the new one.
    assert entry.data[CONF_HOST] == HOST


async def test_reauth_flow(
    hass: HomeAssistant, backrest: FakeBackrest, setup_integration: MockConfigEntry
) -> None:
    """Reauthentication stores the new credentials."""
    backrest.password = "new-password"
    result = await setup_integration.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: USERNAME, CONF_PASSWORD: "new-password"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_PASSWORD] == "new-password"
    await hass.async_block_till_done()


async def test_reconfigure_flow(
    hass: HomeAssistant,
    backrest_servers: dict[str, FakeBackrest],
    backrest: FakeBackrest,
    setup_integration: MockConfigEntry,
) -> None:
    """Reconfiguring changes the address but refuses other instances."""
    backrest_servers["new-address.local"] = backrest
    backrest_servers["other.local"] = FakeBackrest(key_id="ed25519.other")

    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "other.local", CONF_PORT: 9898, CONF_SSL: False, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_instance"

    result = await setup_integration.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "new-address.local", CONF_PORT: 9898, CONF_SSL: False, CONF_VERIFY_SSL: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_integration.data[CONF_HOST] == "new-address.local"
    assert setup_integration.data[CONF_PASSWORD] == PASSWORD
    await hass.async_block_till_done()


async def test_options_flow(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Options change the polling interval and reload the entry."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120, CONF_EVENT_STREAM: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_integration.options == {CONF_SCAN_INTERVAL: 120, CONF_EVENT_STREAM: False}
    assert setup_integration.runtime_data.update_interval.total_seconds() == 120
