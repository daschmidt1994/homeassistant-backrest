"""Listener for the Backrest operation event stream.

The stream is only used as a trigger: when an operation is created, deleted
or changes its status, a (debounced) coordinator refresh is requested. All
entity state is still derived from the regular API calls, so a broken or
unsupported stream only delays updates until the next poll.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from typing import Any

from .api import (
    BackrestAuthError,
    BackrestClient,
    BackrestError,
    BackrestUnsupportedError,
)
from .const import EVENT_STREAM_MAX_BACKOFF, EVENT_STREAM_MIN_BACKOFF
from .coordinator import BackrestCoordinator

_LOGGER = logging.getLogger(__name__)

_MAX_TRACKED_OPERATIONS = 1000


class BackrestEventListener:
    """Consume GetOperationEvents and trigger refreshes."""

    def __init__(self, coordinator: BackrestCoordinator, client: BackrestClient) -> None:
        """Initialize the listener."""
        self._coordinator = coordinator
        self._client = client
        self._statuses: OrderedDict[str, Any] = OrderedDict()

    async def async_run(self) -> None:
        """Listen until cancelled, reconnecting with exponential backoff."""
        backoff = EVENT_STREAM_MIN_BACKOFF
        coordinator = self._coordinator
        while True:
            coordinator.event_stream_state = "connecting"
            try:
                async for event in self._client.async_operation_events():
                    if coordinator.event_stream_state != "connected":
                        coordinator.event_stream_state = "connected"
                        backoff = EVENT_STREAM_MIN_BACKOFF
                        _LOGGER.debug("Connected to the Backrest event stream")
                        # Catch up on anything missed while disconnected.
                        await coordinator.async_request_refresh()
                    if self._is_relevant(event):
                        await coordinator.async_request_refresh()
                _LOGGER.debug("Backrest closed the event stream")
            except BackrestUnsupportedError:
                _LOGGER.info("Backrest does not support the event stream; polling only")
                coordinator.event_stream_state = "unsupported"
                return
            except BackrestAuthError:
                _LOGGER.debug("Event stream authentication failed")
                backoff = EVENT_STREAM_MAX_BACKOFF
            except BackrestError as err:
                _LOGGER.debug("Event stream disconnected: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected error in the Backrest event stream")
                backoff = EVENT_STREAM_MAX_BACKOFF
            coordinator.event_stream_state = "disconnected"
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, EVENT_STREAM_MAX_BACKOFF)

    def _is_relevant(self, event: dict[str, Any]) -> bool:
        """Return whether an event changes something the entities show."""
        if "deletedOperations" in event:
            return True
        relevant = False
        for key in ("createdOperations", "updatedOperations"):
            container = event.get(key)
            if not isinstance(container, dict):
                continue
            operations = container.get("operations")
            for operation in operations if isinstance(operations, list) else []:
                if not isinstance(operation, dict):
                    continue
                op_id = str(operation.get("id"))
                status = operation.get("status")
                # Progress updates of a running backup arrive every second;
                # only status transitions warrant an immediate refresh.
                if key == "createdOperations" or self._statuses.get(op_id) != status:
                    relevant = True
                self._statuses[op_id] = status
                self._statuses.move_to_end(op_id)
        while len(self._statuses) > _MAX_TRACKED_OPERATIONS:
            self._statuses.popitem(last=False)
        return relevant
