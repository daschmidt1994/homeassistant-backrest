"""End-to-end tests against a real Backrest server.

Skipped unless BACKREST_LIVE_HOST is set. The server must have a plan whose
ID is given in BACKREST_LIVE_PLAN and that backs up quickly. The test creates
and deletes a temporary plan through Backrest's SetConfig API.

    BACKREST_LIVE_HOST=127.0.0.1 BACKREST_LIVE_PORT=9898 \
    BACKREST_LIVE_USER=admin BACKREST_LIVE_PASSWORD=... BACKREST_LIVE_PLAN=docs \
    pytest tests/test_live.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest

from custom_components.backrest.api import BackrestClient, build_base_url
from custom_components.backrest.const import DOMAIN

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("BACKREST_LIVE_HOST"), reason="no live Backrest"),
]

TEMP_PLAN = "ha-live-test-plan"


def _input() -> dict[str, Any]:
    return {
        CONF_HOST: os.environ["BACKREST_LIVE_HOST"],
        CONF_PORT: int(os.environ.get("BACKREST_LIVE_PORT", "9898")),
        CONF_SSL: False,
        CONF_VERIFY_SSL: True,
        CONF_USERNAME: os.environ.get("BACKREST_LIVE_USER", ""),
        CONF_PASSWORD: os.environ.get("BACKREST_LIVE_PASSWORD", ""),
    }


async def _set_plans(hass: HomeAssistant, change) -> None:
    data = _input()
    client = BackrestClient(
        async_get_clientsession(hass),
        build_base_url(data[CONF_HOST], data[CONF_PORT], False),
        data[CONF_USERNAME] or None,
        data[CONF_PASSWORD] or None,
    )
    config = await client.async_get_config()
    config["plans"] = change(config.get("plans", []))
    await client._call("SetConfig", config, idempotent=False)


async def test_live_end_to_end(socket_enabled: None, hass: HomeAssistant) -> None:
    """Set up, start a backup, add and remove a plan against a real server."""
    plan_id = os.environ.get("BACKREST_LIVE_PLAN", "docs")
    slug = plan_id.replace("-", "_")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    if _input()[CONF_PASSWORD]:
        bad = {**_input(), CONF_PASSWORD: "definitely-wrong"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
        assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _input())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    assert hass.states.get(f"button.backrest_{slug}_start_backup") is not None
    attempt_before = hass.states.get(f"sensor.backrest_{slug}_last_backup_attempt").state

    await hass.services.async_call(
        "button", "press", {"entity_id": f"button.backrest_{slug}_start_backup"}, blocking=True
    )
    for _ in range(60):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(f"sensor.backrest_{slug}_last_backup_status").state
        attempt = hass.states.get(f"sensor.backrest_{slug}_last_backup_attempt").state
        if attempt != attempt_before and state != "in_progress":
            break
        await asyncio.sleep(0.5)
    assert attempt != attempt_before
    assert state == "success"

    template = next(p for p in (await _get_plans(hass)) if p["id"] == plan_id)
    try:
        await _set_plans(hass, lambda plans: [*plans, {**template, "id": TEMP_PLAN}])
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        temp_entity = f"sensor.backrest_{TEMP_PLAN.replace('-', '_')}_last_backup_status"
        assert hass.states.get(temp_entity) is not None
    finally:
        await _set_plans(hass, lambda plans: [p for p in plans if p["id"] != TEMP_PLAN])
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(temp_entity) is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _get_plans(hass: HomeAssistant) -> list[dict[str, Any]]:
    data = _input()
    client = BackrestClient(
        async_get_clientsession(hass),
        build_base_url(data[CONF_HOST], data[CONF_PORT], False),
        data[CONF_USERNAME] or None,
        data[CONF_PASSWORD] or None,
    )
    return (await client.async_get_config()).get("plans", [])
