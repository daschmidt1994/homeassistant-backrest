"""Fixtures for the Backrest tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

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
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.backrest.api import (
    BackrestAuthError,
    BackrestConnectionError,
    BackrestNotFoundError,
    BackrestUnsupportedError,
)
from custom_components.backrest.const import CONF_EVENT_STREAM, DOMAIN

HOST = "backrest.local"
USERNAME = "admin"
PASSWORD = "correct-horse"
REPO_PASSWORD = "super-secret-repo-password"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration."""


def _ms(value: datetime | None) -> str | None:
    return str(int(value.timestamp() * 1000)) if value else None


class FakeBackrest:
    """In-memory model of a Backrest server, shaped like the real API."""

    def __init__(self, instance: str = "test-instance", key_id: str = "ed25519.fake-key-1") -> None:
        """Initialize the fake server."""
        self.instance = instance
        self.key_id = key_id
        self.auth_enabled = True
        self.username = USERNAME
        self.password = PASSWORD
        self.plans: dict[str, dict[str, Any]] = {}
        self.repos: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, list[dict[str, Any]]] = {"_system_": []}
        self.summary_supported = True
        self.error: Exception | None = None
        self.backup_error: Exception | None = None
        self.backup_release = asyncio.Event()
        self.backup_calls: list[str] = []
        self.cancel_calls: list[int] = []
        self.repo_task_calls: list[tuple[str, str]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.events_unsupported = False
        self.events_failures = 0
        self.events_connects = 0
        self.get_config_calls = 0
        self.peers: dict[str, dict[str, Any]] = {}
        self.multihost_unsupported = False
        self.peer_states_error: Exception | None = None
        self.peer_state_requests: list[int] = []
        self._next_id = 100
        self.add_repo("local-repo")

    def add_repo(self, repo_id: str) -> None:
        """Add a repository (including its secret password)."""
        self.repos[repo_id] = {
            "id": repo_id,
            "uri": f"/srv/restic/{repo_id}",
            "guid": f"guid-{repo_id}",
            "password": REPO_PASSWORD,
            "env": ["AWS_SECRET_ACCESS_KEY=also-secret"],
        }

    def add_plan(
        self,
        plan_id: str,
        repo: str = "local-repo",
        schedule: dict[str, Any] | None = None,
    ) -> None:
        """Add a backup plan."""
        self.plans[plan_id] = {
            "id": plan_id,
            "repo": repo,
            "paths": ["/data"],
            "schedule": schedule or {"cron": "0 3 * * *", "clock": "CLOCK_LOCAL"},
            "hooks": [{"actionGotify": {"token": "gotify-secret-token"}}],
        }
        self.operations.setdefault(plan_id, [])

    def remove_plan(self, plan_id: str) -> None:
        """Remove a plan."""
        del self.plans[plan_id]
        self.operations.pop(plan_id, None)

    def add_backup(
        self,
        plan_id: str,
        status: str,
        start: datetime,
        end: datetime | None = None,
        message: str = "",
        bytes_processed: int = 3_145_734,
        bytes_added: int = 1_048_576,
        files: int = 42,
        percent_done: float | None = None,
        dry_run: bool = False,
    ) -> int:
        """Add a backup operation and return its ID."""
        self._next_id += 1
        op_id = self._next_id
        backup: dict[str, Any] = {}
        if status in ("STATUS_SUCCESS", "STATUS_WARNING"):
            backup["lastStatus"] = {
                "summary": {
                    "totalBytesProcessed": str(bytes_processed),
                    "dataAdded": str(bytes_added),
                    "totalFilesProcessed": str(files),
                    "snapshotId": f"snap{op_id}",
                }
            }
        elif percent_done is not None:
            backup["lastStatus"] = {"status": {"percentDone": percent_done}}
        if dry_run:
            backup["dryRun"] = True
        operation = {
            "id": str(op_id),
            "modno": str(op_id),
            "flowId": str(op_id),
            "repoId": self.plans[plan_id]["repo"],
            "planId": plan_id,
            "instanceId": self.instance,
            "status": status,
            "unixTimeStartMs": _ms(start),
            "operationBackup": backup,
        }
        if end is not None:
            operation["unixTimeEndMs"] = _ms(end)
        if message:
            operation["displayMessage"] = message
        if status in ("STATUS_SUCCESS", "STATUS_WARNING"):
            operation["snapshotId"] = f"snap{op_id}"
        self.operations[plan_id].append(operation)
        # Every backup flow also produces a forget operation.
        self._next_id += 1
        self.operations[plan_id].append(
            {
                "id": str(self._next_id),
                "planId": plan_id,
                "instanceId": self.instance,
                "status": "STATUS_SUCCESS",
                "unixTimeStartMs": _ms(end or start),
                "operationForget": {},
            }
        )
        return op_id

    def add_peer(
        self,
        instance: str,
        plans: tuple[str, ...] = ("system",),
        state: str = "CONNECTION_STATE_CONNECTED",
        heartbeat: datetime | None = None,
        with_config: bool = True,
    ) -> None:
        """Add a remote (multihost client) instance with its plans."""
        self.peers[instance] = {
            "plans": list(plans),
            "state": state,
            "heartbeat": heartbeat,
            "with_config": with_config,
            "operations": {plan_id: [] for plan_id in plans},
        }

    def add_peer_backup(
        self,
        instance: str,
        plan_id: str,
        status: str,
        start: datetime,
        end: datetime | None = None,
        percent_done: float | None = None,
    ) -> int:
        """Add a backup operation synced from a remote instance."""
        self._next_id += 1
        backup: dict[str, Any] = {}
        if percent_done is not None:
            backup["lastStatus"] = {"status": {"percentDone": percent_done}}
        operation = {
            "id": str(self._next_id),
            "repoId": f"{instance}-repo",
            "planId": plan_id,
            "instanceId": instance,
            "originalInstanceKeyid": f"ed25519.{instance}",
            "status": status,
            "unixTimeStartMs": _ms(start),
            "operationBackup": backup,
        }
        if end is not None:
            operation["unixTimeEndMs"] = _ms(end)
        self.peers[instance]["operations"][plan_id].append(operation)
        return self._next_id

    def peer_states(self) -> list[dict[str, Any]]:
        """Return GetPeerSyncStatesStream messages, shaped like Backrest 1.14."""
        states = []
        for instance, peer in self.peers.items():
            state: dict[str, Any] = {
                "peerInstanceId": instance,
                "peerKeyid": f"ed25519.{instance}",
                "state": peer["state"],
                "statusMessage": peer["state"].removeprefix("CONNECTION_STATE_").lower(),
                "knownPlans": [{"id": plan_id} for plan_id in peer["plans"]],
                "knownRepos": [{"id": f"{instance}-repo", "guid": f"guid-{instance}"}],
            }
            if peer["heartbeat"] is not None:
                state["lastHeartbeatMillis"] = _ms(peer["heartbeat"])
            if peer["with_config"]:
                state["remoteConfig"] = {
                    "modno": 3,
                    "version": 6,
                    "repos": [
                        {
                            "id": f"{instance}-repo",
                            "uri": f"rest:http://user:{REPO_PASSWORD}@backup:8100/{instance}/",
                            "password": REPO_PASSWORD,
                        }
                    ],
                    "plans": [
                        {
                            "id": plan_id,
                            "repo": f"{instance}-repo",
                            "paths": ["/opt", "/etc"],
                            "schedule": {"cron": "45 3 * * *", "clock": "CLOCK_LOCAL"},
                        }
                        for plan_id in peer["plans"]
                    ],
                }
            states.append(state)
        return states

    def add_system_operation(
        self,
        kind: str,
        repo_id: str,
        status: str,
        start: datetime,
        end: datetime | None = None,
        message: str = "",
    ) -> int:
        """Add a repository maintenance operation such as operationCheck."""
        self._next_id += 1
        operation: dict[str, Any] = {
            "id": str(self._next_id),
            "flowId": str(self._next_id),
            "repoId": repo_id,
            "repoGuid": f"guid-{repo_id}",
            "planId": "_system_",
            "instanceId": self.instance,
            "status": status,
            "unixTimeStartMs": _ms(start),
            kind: {},
        }
        if end is not None:
            operation["unixTimeEndMs"] = _ms(end)
        if message:
            operation["displayMessage"] = message
        self.operations["_system_"].append(operation)
        return self._next_id

    def set_status(self, op_id: int, status: str, end: datetime | None = None) -> None:
        """Change the status of an operation."""
        for operations in self.operations.values():
            for operation in operations:
                if operation["id"] == str(op_id):
                    operation["status"] = status
                    if end:
                        operation["unixTimeEndMs"] = _ms(end)
                    return
        raise KeyError(op_id)

    # API surface

    def config(self) -> dict[str, Any]:
        """Return the config like GetConfig (secrets included, as in Backrest)."""
        return {
            "modno": 3,
            "version": 6,
            "instance": self.instance,
            "repos": list(self.repos.values()),
            "plans": list(self.plans.values()),
            "auth": {
                "disabled": not self.auth_enabled,
                "users": [{"name": USERNAME, "passwordBcrypt": "JDJhJDEwJHNlY3JldA=="}],
            },
            "sync": {
                "identity": {"keyId": self.key_id},
                "authorizedClients": [
                    {"instanceId": instance, "keyId": f"ed25519.{instance}"}
                    for instance in self.peers
                ],
            },
        }

    def summary(self) -> dict[str, Any]:
        """Compute a summary dashboard similar to Backrest's."""
        now = dt_util.utcnow()

        def summarize(summary_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {"id": summary_id}
            chart: dict[str, list[Any]] = {"timestampMs": [], "durationMs": [], "status": []}
            counts = {"STATUS_SUCCESS": 0, "STATUS_WARNING": 0, "STATUS_ERROR": 0}
            for operation in reversed(operations):
                if "operationBackup" not in operation:
                    continue
                start = int(operation["unixTimeStartMs"])
                if operation["status"] == "STATUS_PENDING":
                    result["nextBackupTimeMs"] = str(start)
                    continue
                if start < (now - timedelta(days=30)).timestamp() * 1000:
                    continue
                if operation["status"] in counts:
                    counts[operation["status"]] += 1
                end = int(operation.get("unixTimeEndMs") or start)
                chart["timestampMs"].append(str(start))
                chart["durationMs"].append(str(max(end - start, 1000)))
                chart["status"].append(operation["status"])
            if counts["STATUS_SUCCESS"]:
                result["backupsSuccessLast30days"] = str(counts["STATUS_SUCCESS"])
            if counts["STATUS_WARNING"]:
                result["backupsWarningLast30days"] = str(counts["STATUS_WARNING"])
            if counts["STATUS_ERROR"]:
                result["backupsFailed30days"] = str(counts["STATUS_ERROR"])
            result["recentBackups"] = chart if chart["timestampMs"] else {}
            return result

        return {
            "planSummaries": [
                summarize(plan_id, self.operations.get(plan_id, [])) for plan_id in self.plans
            ],
            "repoSummaries": [
                summarize(
                    repo_id,
                    [
                        op
                        for plan_id, plan in self.plans.items()
                        if plan["repo"] == repo_id
                        for op in self.operations.get(plan_id, [])
                    ],
                )
                for repo_id in self.repos
            ],
            "configPath": "/etc/backrest/config.json",
            "dataPath": "/var/lib/backrest",
        }


class FakeClient:
    """Stand-in for BackrestClient that talks to a FakeBackrest."""

    servers: dict[str, FakeBackrest]

    def __init__(
        self,
        session: Any,
        base_url: Any,
        username: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize the client."""
        self.base_url = base_url
        self._username = username
        self._password = password
        self.authenticated = False

    @property
    def server(self) -> FakeBackrest:
        """Return the server addressed by the base URL."""
        if (server := self.servers.get(self.base_url.host)) is None:
            raise BackrestConnectionError("Error calling GetConfig: ClientConnectorError")
        return server

    def _check(self) -> FakeBackrest:
        server = self.server
        if server.error is not None:
            raise server.error
        if server.auth_enabled:
            if self._username is None:
                raise BackrestAuthError("Unauthorized (No Authorization Header)")
            if (self._username, self._password) != (server.username, server.password):
                raise BackrestAuthError("invalid password")
            self.authenticated = True
        return server

    async def async_get_config(self) -> dict[str, Any]:
        server = self._check()
        server.get_config_calls += 1
        return server.config()

    async def async_get_summary_dashboard(self) -> dict[str, Any]:
        server = self._check()
        if not server.summary_supported:
            raise BackrestUnsupportedError("unimplemented", "")
        return server.summary()

    async def async_get_operations(
        self, plan_id: str, instance_id: str | None, last_n: int
    ) -> list[dict[str, Any]]:
        server = self._check()
        if instance_id in server.peers:
            operations = server.peers[instance_id]["operations"].get(plan_id, [])
            return [dict(op) for op in operations[-last_n:]]
        if plan_id not in server.plans and plan_id != "_system_":
            raise BackrestNotFoundError("not_found", "plan not found")
        operations = server.operations.get(plan_id, [])
        return [dict(op) for op in operations[-last_n:]]

    async def async_get_peer_states(self, expected: int) -> list[dict[str, Any]]:
        server = self._check()
        server.peer_state_requests.append(expected)
        if server.multihost_unsupported:
            raise BackrestUnsupportedError("unimplemented", "")
        if server.peer_states_error is not None:
            raise server.peer_states_error
        return server.peer_states()

    async def async_backup(self, plan_id: str) -> None:
        server = self._check()
        server.backup_calls.append(plan_id)
        if plan_id not in server.plans:
            raise BackrestNotFoundError("not_found", f'get plan "{plan_id}": plan not found')
        if server.backup_error is not None:
            raise server.backup_error
        # Like Backrest: the call only returns once the backup is done.
        await server.backup_release.wait()

    async def async_do_repo_task(self, repo_id: str, task: str) -> None:
        server = self._check()
        server.repo_task_calls.append((repo_id, task))

    async def async_cancel(self, operation_id: int) -> None:
        server = self._check()
        server.cancel_calls.append(operation_id)

    async def async_operation_events(self) -> AsyncIterator[dict[str, Any]]:
        server = self._check()
        if server.events_unsupported:
            raise BackrestUnsupportedError("unimplemented", "")
        server.events_connects += 1
        if server.events_failures:
            server.events_failures -= 1
            raise BackrestConnectionError("Event stream failed: ClientPayloadError")
        while True:
            yield await server.events.get()


@pytest.fixture
def backrest_servers() -> Generator[dict[str, FakeBackrest]]:
    """Patch the API client and return the reachable fake servers by host."""
    servers: dict[str, FakeBackrest] = {}
    client_cls = type("BoundFakeClient", (FakeClient,), {"servers": servers})
    with (
        patch("custom_components.backrest.BackrestClient", client_cls),
        patch("custom_components.backrest.config_flow.BackrestClient", client_cls),
    ):
        yield servers


@pytest.fixture
def backrest(backrest_servers: dict[str, FakeBackrest]) -> FakeBackrest:
    """Return a fake Backrest server with two plans and some history."""
    server = FakeBackrest()
    now = dt_util.utcnow()
    server.add_plan("nas")
    server.add_plan("docs", schedule={"maxFrequencyHours": 6})
    server.add_backup(
        "nas", "STATUS_SUCCESS", now - timedelta(hours=5), now - timedelta(hours=4, minutes=58)
    )
    server.add_backup(
        "nas",
        "STATUS_SUCCESS",
        now - timedelta(hours=2),
        now - timedelta(hours=1, minutes=57, seconds=30),
    )
    server.add_backup("nas", "STATUS_PENDING", now + timedelta(hours=10))
    server.add_backup(
        "docs",
        "STATUS_ERROR",
        now - timedelta(hours=1),
        now - timedelta(minutes=59),
        message="repository is locked by another process",
    )
    backrest_servers[HOST] = server
    return server


def make_entry(
    host: str = HOST,
    unique_id: str = "ed25519.fake-key-1",
    title: str = "Backrest test-instance",
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Create a config entry for a fake server."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=unique_id,
        data={
            CONF_HOST: host,
            CONF_PORT: 9898,
            CONF_SSL: False,
            CONF_VERIFY_SSL: True,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
        },
        options=options
        if options is not None
        else {CONF_SCAN_INTERVAL: 60, CONF_EVENT_STREAM: False},
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return the default config entry."""
    return make_entry()


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, backrest: FakeBackrest, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration with the default fake server."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
