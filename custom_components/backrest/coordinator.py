"""Data update coordinator for the Backrest integration."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BackrestApiError,
    BackrestAuthError,
    BackrestClient,
    BackrestConnectionError,
    BackrestError,
    BackrestNotFoundError,
    BackrestUnsupportedError,
)
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_PARALLEL_REQUESTS,
    OPERATIONS_PER_PLAN,
    START_CONFIRM_TIMEOUT,
    SYSTEM_OPERATIONS,
)
from .models import (
    SYSTEM_PLAN_ID,
    BackrestConfig,
    BackrestData,
    RemoteInstance,
    SummaryDashboard,
    build_plan_state,
    build_repo_states,
    remote_plan_key,
)

_LOGGER = logging.getLogger(__name__)

type BackrestConfigEntry = ConfigEntry[BackrestCoordinator]

REPO_TASK_CHECK = "TASK_CHECK"
REPO_TASK_PRUNE = "TASK_PRUNE"


class BackrestCoordinator(DataUpdateCoordinator[BackrestData]):
    """Polls a Backrest instance and runs actions against it."""

    config_entry: BackrestConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BackrestConfigEntry,
        client: BackrestClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Backrest {entry.title}",
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.summary_supported: bool | None = None
        self.multihost_supported: bool | None = None
        self.last_successful_fetch: datetime | None = None
        self.last_error: str | None = None
        self.event_stream_state: str = "disabled"
        self.server_device_id: str | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def _async_update_data(self) -> BackrestData:
        """Fetch configuration, summary and recent operations."""
        try:
            data = await self._async_fetch()
        except BackrestAuthError as err:
            self.last_error = type(err).__name__
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except BackrestError as err:
            self.last_error = f"{type(err).__name__}: {err}"
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.last_error = None
        self.last_successful_fetch = data.fetched_at
        return data

    async def _async_fetch(self) -> BackrestData:
        # The raw config contains secrets; it is reduced to the fields needed
        # and the raw payload is dropped right away.
        config = BackrestConfig.from_api(await self.client.async_get_config())
        summary = await self._async_fetch_summary()
        remotes = await self._async_fetch_remotes()
        remote_plans = [plan for remote in remotes.values() for plan in remote.plans]
        semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

        async def fetch_operations(
            plan_id: str, instance: str, last_n: int
        ) -> list[dict[str, Any]]:
            async with semaphore:
                return await self.client.async_get_operations(plan_id, instance, last_n)

        plan_ids = list(config.plans)
        *results, system_result = await asyncio.gather(
            *(
                fetch_operations(plan_id, config.instance, OPERATIONS_PER_PLAN)
                for plan_id in plan_ids
            ),
            *(
                fetch_operations(plan.id, plan.instance, OPERATIONS_PER_PLAN)
                for plan in remote_plans
            ),
            fetch_operations(SYSTEM_PLAN_ID, config.instance, SYSTEM_OPERATIONS),
            return_exceptions=True,
        )
        local_results = results[: len(plan_ids)]
        remote_results = results[len(plan_ids) :]
        all_operations: list[dict[str, Any]] = []
        if isinstance(system_result, BackrestAuthError):
            raise system_result
        if isinstance(system_result, BaseException):
            # Repository maintenance only enriches the data.
            _LOGGER.debug("Cannot fetch repository maintenance operations: %s", system_result)
        else:
            all_operations.extend(
                op for op in system_result if op.get("instanceId") in (None, "", config.instance)
            )
        now = dt_util.utcnow()
        plans = {}
        for plan_id, result in zip(plan_ids, local_results, strict=True):
            if isinstance(result, BackrestNotFoundError):
                # Plan deleted between GetConfig and GetOperations.
                continue
            if isinstance(result, BaseException):
                raise result
            operations = [
                op for op in result if op.get("instanceId") in (None, "", config.instance)
            ]
            all_operations.extend(operations)
            plans[plan_id] = build_plan_state(
                config.plans[plan_id],
                operations,
                summary.plans.get(plan_id) if summary else None,
                OPERATIONS_PER_PLAN,
                now,
            )
        for plan, result in zip(remote_plans, remote_results, strict=True):
            if isinstance(result, BackrestAuthError):
                raise result
            if isinstance(result, BaseException):
                # A remote plan only enriches the data; keep the rest working.
                _LOGGER.debug(
                    "Cannot fetch operations of %s/%s: %s", plan.instance, plan.id, result
                )
                continue
            # Remote repos are not tracked here: their operations stay out of
            # all_operations so local repository states are unaffected.
            operations = [op for op in result if op.get("instanceId") == plan.instance]
            plans[remote_plan_key(plan.instance, plan.id)] = build_plan_state(
                plan, operations, None, OPERATIONS_PER_PLAN, now
            )

        return BackrestData(
            config=config,
            plans=plans,
            repos=build_repo_states(config, summary, all_operations),
            summary_available=summary is not None,
            fetched_at=now,
            remotes=remotes,
        )

    async def _async_fetch_summary(self) -> SummaryDashboard | None:
        if self.summary_supported is False:
            return None
        try:
            raw = await self.client.async_get_summary_dashboard()
        except BackrestUnsupportedError:
            _LOGGER.info(
                "Backrest at %s has no summary dashboard API (requires 1.6.0+)",
                self.client.base_url.host,
            )
            self.summary_supported = False
            return None
        except BackrestApiError as err:
            # The summary only enriches the data; don't fail the whole refresh.
            _LOGGER.debug("Summary dashboard unavailable: %s", err)
            return None
        self.summary_supported = True
        return SummaryDashboard.from_api(raw)

    async def _async_fetch_remotes(self) -> dict[str, RemoteInstance]:
        """Return the remote instances synced to this server (multihost)."""
        if self.multihost_supported is False:
            return {}
        try:
            raw = await self.client.async_get_peer_states()
        except BackrestUnsupportedError:
            _LOGGER.info(
                "Backrest at %s has no multihost sync API (requires 1.10.0+)",
                self.client.base_url.host,
            )
            self.multihost_supported = False
            return {}
        except BackrestAuthError:
            raise
        except BackrestError as err:
            # Remote instances only enrich the data; keep the last known ones.
            _LOGGER.debug("Remote instances unavailable: %s", err)
            return dict(self.data.remotes) if self.data else {}
        self.multihost_supported = True
        remotes: dict[str, RemoteInstance] = {}
        for item in raw:
            if remote := RemoteInstance.from_api(item):
                remotes[remote.instance_id] = remote
        return remotes

    # Actions

    def is_backup_active(self, plan_id: str) -> bool:
        """Return whether a backup of the plan is running, queued or requested."""
        if (task := self._tasks.get(f"backup:{plan_id}")) and not task.done():
            return True
        if self.data and (state := self.data.plans.get(plan_id)):
            return state.is_running or state.queued > 0
        return False

    async def async_start_backup(self, plan_id: str) -> None:
        """Start a backup of a plan."""
        self._require_plan(plan_id)
        if self.is_backup_active(plan_id):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="backup_already_running",
                translation_placeholders={"target": plan_id},
            )
        await self._async_start_task(
            f"backup:{plan_id}",
            self.client.async_backup(plan_id),
            placeholders={"target": plan_id},
        )

    async def async_cancel_backup(self, plan_id: str) -> None:
        """Cancel the running backup of a plan."""
        state = self._require_plan(plan_id)
        if not state.running:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_backup_running",
                translation_placeholders={"target": plan_id},
            )
        for operation in state.running:
            try:
                await self.client.async_cancel(operation.id)
            except BackrestError as err:
                raise _action_error(err, {"target": plan_id}) from err
        await self.async_request_refresh()

    async def async_run_repo_task(self, repo_id: str, task: str) -> None:
        """Start a repository maintenance task."""
        if not self.data or repo_id not in self.data.repos:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="repo_not_found",
                translation_placeholders={"target": repo_id},
            )
        key = f"repo:{repo_id}:{task}"
        if (running := self._tasks.get(key)) and not running.done():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="repo_task_already_running",
                translation_placeholders={"target": repo_id},
            )
        await self._async_start_task(
            key,
            self.client.async_do_repo_task(repo_id, task),
            placeholders={"target": repo_id},
        )

    def _require_plan(self, plan_id: str) -> Any:
        if not self.data or (state := self.data.plans.get(plan_id)) is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="plan_not_found",
                translation_placeholders={"target": plan_id},
            )
        if state.plan.is_remote:
            # The API offers no way to start or cancel backups on a peer.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="remote_plan_action",
                translation_placeholders={"target": plan_id},
            )
        return state

    async def _async_start_task(
        self,
        key: str,
        request: Coroutine[Any, Any, None],
        placeholders: dict[str, str],
    ) -> None:
        """Send a long running request and wait until it is accepted.

        Backrest only answers the request once the task has finished, so the
        request keeps running in the background. Errors returned within
        START_CONFIRM_TIMEOUT (unknown plan, authentication, connection, or a
        task failing right away) are raised to the caller. A request that is
        still pending afterwards means the task was accepted and is running;
        this does not imply that it will succeed.
        """
        task = self.config_entry.async_create_background_task(
            self.hass, request, name=f"backrest {key}"
        )
        self._tasks[key] = task
        task.add_done_callback(partial(self._async_task_done, key))
        await asyncio.wait({task}, timeout=START_CONFIRM_TIMEOUT)
        if task.done() and not task.cancelled() and (err := task.exception()):
            raise _action_error(err, placeholders) from err
        await self.async_request_refresh()

    def _async_task_done(self, key: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if task.cancelled():
            return
        if err := task.exception():
            _LOGGER.debug("Request %s ended with: %s", key, err)
        if self.config_entry.state is ConfigEntryState.LOADED:
            self.config_entry.async_create_task(
                self.hass, self.async_request_refresh(), name="backrest refresh"
            )

    @property
    def pending_requests(self) -> list[str]:
        """Return the keys of requests that are still running."""
        return sorted(key for key, task in self._tasks.items() if not task.done())


def _action_error(err: BaseException, placeholders: dict[str, str]) -> HomeAssistantError:
    """Map an API error raised by an action to a translated exception."""
    if isinstance(err, BackrestNotFoundError):
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="target_not_found",
            translation_placeholders=placeholders,
        )
    if isinstance(err, BackrestAuthError):
        return HomeAssistantError(translation_domain=DOMAIN, translation_key="auth_failed")
    if isinstance(err, BackrestConnectionError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        )
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={**placeholders, "error": str(err)},
    )
