"""Diagnostics support for the Backrest integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN, OPERATIONS_PER_PLAN
from .coordinator import BackrestConfigEntry
from .models import PlanState

TO_REDACT = {CONF_HOST, CONF_PASSWORD, CONF_USERNAME, "unique_id", "identity_key_id"}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _plan_diagnostics(state: PlanState) -> dict[str, Any]:
    return {
        "schedule": state.plan.schedule.kind,
        "last_status": state.last_status,
        "last_attempt": _iso(state.last_attempt),
        "last_success": _iso(state.last_success_time),
        "next_backup": _iso(state.next_backup),
        "running": len(state.running),
        "queued": state.queued,
        "last_failed": state.last_failed,
        "overdue": state.overdue,
        "history_complete": state.history_complete,
        "has_summary": state.summary is not None,
        "last_error_present": state.last_error is not None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BackrestConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    integration = await async_get_integration(hass, DOMAIN)
    data = coordinator.data
    host = entry.data.get(CONF_HOST, "")

    last_error = coordinator.last_error
    if last_error and host:
        last_error = last_error.replace(host, REDACTED)

    diagnostics: dict[str, Any] = {
        "integration_version": str(integration.version),
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "credentials_configured": CONF_USERNAME in entry.data,
        },
        "connection": {
            "reachable": coordinator.last_update_success,
            "authenticated_session": coordinator.client.authenticated,
            "last_successful_fetch": _iso(coordinator.last_successful_fetch),
            "last_error": last_error,
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "event_stream": coordinator.event_stream_state,
            "pending_requests": coordinator.pending_requests,
        },
        "api_features": {
            "get_config": data is not None,
            "get_operations": data is not None,
            "summary_dashboard": coordinator.summary_supported,
            "event_stream": coordinator.event_stream_state,
            "operations_per_plan": OPERATIONS_PER_PLAN,
        },
    }
    if data is not None:
        # Plan and repository IDs are user-chosen names; paths, repository
        # URIs, passwords and hooks are never stored by the integration.
        diagnostics["backrest"] = {
            "auth_enabled": data.config.auth_enabled,
            "has_identity": data.config.identity_key_id is not None,
            "plan_count": len(data.plans),
            "repository_count": len(data.repos),
            "running_backups": data.running_count,
            "summary_available": data.summary_available,
            "plans": {
                plan_id: _plan_diagnostics(state) for plan_id, state in sorted(data.plans.items())
            },
            "repositories": {
                repo_id: {"plans": list(state.plan_ids), "has_summary": state.summary is not None}
                for repo_id, state in sorted(data.repos.items())
            },
        }
    return async_redact_data(diagnostics, TO_REDACT)
