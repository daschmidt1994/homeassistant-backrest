"""Async client for the Backrest Connect RPC API.

Backrest exposes its API as Connect RPC services (``v1.Backrest`` and
``v1.Authentication``). Unary calls are plain ``POST`` requests with a JSON
body to ``/<service>/<method>``; server streaming calls use the enveloped
``application/connect+json`` content type. See ``proto/v1/service.proto`` in
the Backrest repository for the service definitions.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
import logging
import struct
from typing import Any

import aiohttp
from yarl import URL

from .const import EVENT_STREAM_READ_TIMEOUT, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)

BACKREST_SERVICE = "v1.Backrest"
AUTH_SERVICE = "v1.Authentication"

_CONNECT_HEADERS = {
    "Content-Type": "application/json",
    "Connect-Protocol-Version": "1",
    "Accept": "application/json",
}
_STREAM_HEADERS = {
    "Content-Type": "application/connect+json",
    "Connect-Protocol-Version": "1",
}
# Envelope flag marking the final message of a Connect stream.
_END_STREAM_FLAG = 0x02
# Guard against absurd frame sizes from misbehaving servers.
_MAX_STREAM_FRAME = 32 * 1024 * 1024

# Read requests are retried once on transient network errors.
_READ_ATTEMPTS = 2
_RETRY_DELAY = 1.0


class BackrestError(Exception):
    """Base class for Backrest API errors."""


class BackrestConnectionError(BackrestError):
    """Backrest could not be reached."""


class BackrestTimeoutError(BackrestConnectionError):
    """A request to Backrest timed out."""


class BackrestSslError(BackrestConnectionError):
    """The TLS certificate of the Backrest server was rejected."""


class BackrestAuthError(BackrestError):
    """Authentication failed or is required."""


class BackrestResponseError(BackrestError):
    """Backrest returned data that could not be understood."""


class BackrestApiError(BackrestError):
    """Backrest returned a Connect error."""

    def __init__(self, code: str, message: str) -> None:
        """Initialize the error."""
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class BackrestNotFoundError(BackrestApiError):
    """The requested plan, repository or operation does not exist."""


class BackrestUnsupportedError(BackrestApiError):
    """The Backrest server does not implement the called method."""


def build_base_url(host: str, port: int, use_ssl: bool) -> URL:
    """Build the base URL of a Backrest server."""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return URL.build(scheme="https" if use_ssl else "http", host=host, port=port)


def _error_from_payload(status: int, text: str) -> BackrestError:
    """Translate a non-200 Connect response into an exception."""
    code = ""
    message = ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or "")
    else:
        # Backrest's auth middleware answers with a plain text body.
        message = text.strip()[:200]

    if status == 401 or code == "unauthenticated":
        return BackrestAuthError(message or "unauthenticated")
    if status == 403 or code == "permission_denied":
        return BackrestAuthError(message or "permission denied")
    if code == "unimplemented":
        return BackrestUnsupportedError(code, message)
    if code == "not_found":
        return BackrestNotFoundError(code, message)
    if not code:
        if status == 404:
            return BackrestUnsupportedError("unimplemented", message)
        return BackrestResponseError(f"Unexpected HTTP status {status}")
    return BackrestApiError(code, message)


class BackrestClient:
    """Client for a single Backrest instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: URL,
        username: str | None = None,
        password: str | None = None,
        request_timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base_url = base_url
        self._username = username or None
        self._password = password or None
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    @property
    def base_url(self) -> URL:
        """Return the base URL of the server."""
        return self._base_url

    @property
    def has_credentials(self) -> bool:
        """Return whether credentials are configured."""
        return self._username is not None

    @property
    def authenticated(self) -> bool:
        """Return whether a session token is currently held."""
        return self._token is not None

    def _auth_headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def async_login(self) -> None:
        """Exchange the configured credentials for a session token (JWT)."""
        if not self.has_credentials:
            raise BackrestAuthError("Backrest requires authentication")
        failed_token = self._token
        async with self._login_lock:
            if self._token is not None and self._token != failed_token:
                # Another request already logged in again.
                return
            self._token = None
            result = await self._post(
                AUTH_SERVICE,
                "Login",
                {"username": self._username, "password": self._password},
                authenticated=False,
            )
            token = result.get("token")
            if not isinstance(token, str) or not token:
                raise BackrestResponseError("Login response did not contain a token")
            self._token = token
            _LOGGER.debug("Logged in to Backrest at %s", self._base_url.host)

    async def _post(
        self,
        service: str,
        method: str,
        payload: dict[str, Any],
        *,
        authenticated: bool = True,
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Perform one unary Connect call without auth retry."""
        url = self._base_url / service / method
        headers = dict(_CONNECT_HEADERS)
        if authenticated:
            headers.update(self._auth_headers())
        try:
            async with self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=client_timeout or self._timeout,
                allow_redirects=False,
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise _error_from_payload(response.status, text)
        except TimeoutError as err:
            raise BackrestTimeoutError(f"Timeout calling {method}") from err
        except aiohttp.ClientConnectorCertificateError as err:
            raise BackrestSslError(f"TLS certificate rejected: {err}") from err
        except aiohttp.ClientSSLError as err:
            raise BackrestSslError(f"TLS error: {err}") from err
        except aiohttp.ClientError as err:
            raise BackrestConnectionError(f"Error calling {method}: {type(err).__name__}") from err

        try:
            data = json.loads(text) if text else {}
        except ValueError as err:
            raise BackrestResponseError(
                f"{method} did not return JSON; is this a Backrest server?"
            ) from err
        if not isinstance(data, dict):
            raise BackrestResponseError(f"{method} returned unexpected data")
        return data

    async def _call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotent: bool = True,
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated call with re-login and retry handling."""
        attempts = _READ_ATTEMPTS if idempotent else 1
        relogged = False
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._post(
                    BACKREST_SERVICE, method, payload or {}, client_timeout=client_timeout
                )
            except BackrestAuthError:
                if relogged or not self.has_credentials:
                    raise
                relogged = True
                attempt -= 1
                await self.async_login()
            except BackrestSslError:
                raise
            except BackrestConnectionError:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(_RETRY_DELAY)

    async def async_get_config(self) -> dict[str, Any]:
        """Return the raw Backrest configuration.

        Callers must not persist or log the result: Backrest includes repository
        passwords and hook credentials in this response.
        """
        return await self._call("GetConfig")

    async def async_get_summary_dashboard(self) -> dict[str, Any]:
        """Return the summary dashboard (Backrest 1.6.0 and newer)."""
        return await self._call("GetSummaryDashboard")

    async def async_get_operations(
        self, plan_id: str, instance_id: str | None, last_n: int
    ) -> list[dict[str, Any]]:
        """Return the most recent operations of a plan, oldest first."""
        selector: dict[str, Any] = {"planId": plan_id}
        if instance_id:
            selector["instanceId"] = instance_id
        result = await self._call("GetOperations", {"selector": selector, "lastN": str(last_n)})
        operations = result.get("operations", [])
        if not isinstance(operations, list):
            raise BackrestResponseError("GetOperations returned unexpected data")
        return [op for op in operations if isinstance(op, dict)]

    async def async_backup(self, plan_id: str) -> None:
        """Run a backup of a plan.

        Backrest only answers this call once the backup has completed, which
        may take hours. Callers should run it as a background task.
        """
        await self._call(
            "Backup",
            {"value": plan_id},
            idempotent=False,
            client_timeout=aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT),
        )

    async def async_do_repo_task(self, repo_id: str, task: str) -> None:
        """Run a repository task such as TASK_CHECK or TASK_PRUNE.

        Backrest versions before 1.14 only answer once the task has finished.
        """
        await self._call(
            "DoRepoTask",
            {"repoId": repo_id, "task": task},
            idempotent=False,
            client_timeout=aiohttp.ClientTimeout(total=None, sock_connect=REQUEST_TIMEOUT),
        )

    async def async_cancel(self, operation_id: int) -> None:
        """Request cancellation of an operation. Not guaranteed to succeed."""
        await self._call("Cancel", {"operationId": str(operation_id)}, idempotent=False)

    async def async_operation_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield operation events from the GetOperationEvents stream.

        The iterator ends when the server closes the stream and raises a
        BackrestError subclass on failure.
        """
        for attempt in range(2):
            try:
                async for event in self._stream("GetOperationEvents", {}):
                    yield event
            except BackrestAuthError:
                if attempt or not self.has_credentials:
                    raise
                await self.async_login()
                continue
            return

    async def _stream(self, method: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        url = self._base_url / BACKREST_SERVICE / method
        body = json.dumps(payload).encode()
        envelope = struct.pack(">BI", 0, len(body)) + body
        headers = dict(_STREAM_HEADERS)
        headers.update(self._auth_headers())
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=REQUEST_TIMEOUT, sock_read=EVENT_STREAM_READ_TIMEOUT
        )
        try:
            async with self._session.post(
                url,
                data=envelope,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise _error_from_payload(response.status, await response.text())
                content = response.content
                while True:
                    try:
                        header = await content.readexactly(5)
                    except asyncio.IncompleteReadError as err:
                        if not err.partial:
                            return
                        raise BackrestResponseError("Truncated stream frame") from err
                    flags, length = struct.unpack(">BI", header)
                    if length > _MAX_STREAM_FRAME:
                        raise BackrestResponseError("Stream frame too large")
                    try:
                        frame = await content.readexactly(length)
                    except asyncio.IncompleteReadError as err:
                        raise BackrestResponseError("Truncated stream frame") from err
                    message = parse_stream_frame(frame)
                    if flags & _END_STREAM_FLAG:
                        error = message.get("error")
                        if isinstance(error, dict):
                            raise _error_from_payload(0, json.dumps(error))
                        return
                    yield message
        except TimeoutError as err:
            raise BackrestTimeoutError("Event stream timed out") from err
        except aiohttp.ClientError as err:
            raise BackrestConnectionError(f"Event stream failed: {type(err).__name__}") from err


def parse_stream_frame(frame: bytes) -> dict[str, Any]:
    """Decode the JSON payload of a single stream frame."""
    try:
        message = json.loads(frame) if frame else {}
    except ValueError as err:
        raise BackrestResponseError("Invalid stream frame") from err
    if not isinstance(message, dict):
        raise BackrestResponseError("Invalid stream frame")
    return message
