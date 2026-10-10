"""Tests for the operation event stream listener."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.backrest.const import CONF_EVENT_STREAM

from .conftest import FakeBackrest, make_entry


async def test_events_trigger_refresh(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """Created operations and status changes refresh; progress updates do not."""
    entry = make_entry(options={CONF_EVENT_STREAM: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data

    with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
        op = {"id": "900", "planId": "nas", "status": "STATUS_INPROGRESS"}
        await backrest.events.put({"createdOperations": {"operations": [op]}})
        await hass.async_block_till_done()
        assert coordinator.event_stream_state == "connected"
        # One catch-up refresh after connecting plus one for the new operation.
        assert refresh.await_count == 2

        refresh.reset_mock()
        await backrest.events.put({"updatedOperations": {"operations": [op]}})
        await backrest.events.put({"keepAlive": {}})
        await hass.async_block_till_done()
        assert refresh.await_count == 0

        await backrest.events.put(
            {"updatedOperations": {"operations": [{**op, "status": "STATUS_SUCCESS"}]}}
        )
        await backrest.events.put({"deletedOperations": {"values": ["1"]}})
        await hass.async_block_till_done()
        assert refresh.await_count == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_event_stream_unsupported(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """Servers without the stream fall back to polling only."""
    backrest.events_unsupported = True
    entry = make_entry(options={CONF_EVENT_STREAM: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.event_stream_state == "unsupported"
    assert hass.states.get("sensor.backrest_nas_last_backup_status").state == "success"


async def test_event_stream_reconnects(hass: HomeAssistant, backrest: FakeBackrest) -> None:
    """A failed stream is retried after a backoff while polling keeps working."""
    backrest.events_failures = 2
    entry = make_entry(options={CONF_EVENT_STREAM: True})
    with patch("custom_components.backrest.events.EVENT_STREAM_MIN_BACKOFF", 0):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        assert coordinator.last_update_success
        await backrest.events.put({"keepAlive": {}})
        await hass.async_block_till_done()
    assert backrest.events_connects == 3
    assert coordinator.event_stream_state == "connected"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
