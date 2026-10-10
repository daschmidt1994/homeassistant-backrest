"""Config flow for the Backrest integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    BackrestAuthError,
    BackrestClient,
    BackrestConnectionError,
    BackrestError,
    BackrestResponseError,
    BackrestSslError,
    build_base_url,
)
from .const import (
    CONF_EVENT_STREAM,
    DEFAULT_EVENT_STREAM,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import BackrestConfigEntry
from .models import BackrestConfig

_LOGGER = logging.getLogger(__name__)

_PORT_SELECTOR = NumberSelector(
    NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
)
_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_USERNAME_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
)


def _connection_schema(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, vol.UNDEFINED)): str,
        vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): _PORT_SELECTOR,
        vol.Required(CONF_SSL, default=defaults.get(CONF_SSL, False)): bool,
        vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
    }


def _credentials_schema(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Optional(
            CONF_USERNAME,
            description={"suggested_value": defaults.get(CONF_USERNAME)},
        ): _USERNAME_SELECTOR,
        vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }


def _normalize(user_input: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    if CONF_HOST in data:
        data[CONF_HOST] = str(data[CONF_HOST]).strip()
    if CONF_PORT in data:
        data[CONF_PORT] = int(data[CONF_PORT])
    for key in (CONF_USERNAME, CONF_PASSWORD):
        if not data.get(key):
            data.pop(key, None)
    return data


async def async_validate_connection(hass: HomeAssistant, data: Mapping[str, Any]) -> BackrestConfig:
    """Connect to Backrest and return its configuration."""
    client = BackrestClient(
        async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True)),
        build_base_url(data[CONF_HOST], data[CONF_PORT], data[CONF_SSL]),
        data.get(CONF_USERNAME),
        data.get(CONF_PASSWORD),
    )
    return BackrestConfig.from_api(await client.async_get_config())


def _error_key(err: Exception, data: Mapping[str, Any]) -> str:
    if isinstance(err, BackrestAuthError):
        return "invalid_auth" if data.get(CONF_USERNAME) else "auth_required"
    if isinstance(err, BackrestSslError):
        return "ssl_error"
    if isinstance(err, BackrestConnectionError):
        return "cannot_connect"
    if isinstance(err, BackrestResponseError):
        return "invalid_response"
    return "unknown"


class BackrestConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Backrest."""

    VERSION = 1
    MINOR_VERSION = 1

    async def _async_try_connect(
        self, data: Mapping[str, Any], errors: dict[str, str]
    ) -> BackrestConfig | None:
        try:
            return await async_validate_connection(self.hass, data)
        except BackrestError as err:
            errors["base"] = _error_key(err, data)
            _LOGGER.debug("Connection test failed: %s", type(err).__name__)
        except Exception:
            _LOGGER.exception("Unexpected error while connecting to Backrest")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalize(user_input)
            self._async_abort_entries_match(
                {CONF_HOST: data[CONF_HOST], CONF_PORT: data[CONF_PORT]}
            )
            config = await self._async_try_connect(data, errors)
            if config is not None:
                await self.async_set_unique_id(
                    config.unique_id or f"{data[CONF_HOST]}:{data[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: data[CONF_HOST],
                        CONF_PORT: data[CONF_PORT],
                        CONF_SSL: data[CONF_SSL],
                        CONF_VERIFY_SSL: data[CONF_VERIFY_SSL],
                    }
                )
                title = f"Backrest {config.instance}" if config.instance else "Backrest"
                return self.async_create_entry(title=title, data=data)
            user_input = {**user_input, CONF_PASSWORD: ""}

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    **_connection_schema(user_input or {}),
                    **_credentials_schema(user_input or {}),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle a rejected token or changed credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for new credentials."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data}
            data.pop(CONF_USERNAME, None)
            data.pop(CONF_PASSWORD, None)
            data.update(_normalize(user_input))
            config = await self._async_try_connect(data, errors)
            if config is not None:
                if config.unique_id and entry.unique_id and config.unique_id != entry.unique_id:
                    return self.async_abort(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(_credentials_schema(user_input or entry.data)),
            description_placeholders={"name": entry.title},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the connection settings."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **_normalize(user_input)}
            config = await self._async_try_connect(data, errors)
            if config is not None:
                if config.unique_id and entry.unique_id and config.unique_id != entry.unique_id:
                    return self.async_abort(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(_connection_schema(user_input or entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: BackrestConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BackrestOptionsFlow()


class BackrestOptionsFlow(OptionsFlow):
    """Handle Backrest options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            user_input[CONF_SCAN_INTERVAL] = int(user_input[CONF_SCAN_INTERVAL])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        CONF_EVENT_STREAM,
                        default=options.get(CONF_EVENT_STREAM, DEFAULT_EVENT_STREAM),
                    ): bool,
                }
            ),
        )
