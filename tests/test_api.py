"""Tests for the Backrest Connect RPC client."""

from __future__ import annotations

import json
import struct
from unittest.mock import patch

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.backrest.api import (
    BackrestApiError,
    BackrestAuthError,
    BackrestClient,
    BackrestConnectionError,
    BackrestNotFoundError,
    BackrestResponseError,
    BackrestTimeoutError,
    BackrestUnsupportedError,
    build_base_url,
)

BASE = "http://backrest.local:9898"


@pytest.fixture(autouse=True)
def no_retry_delay():
    """Do not sleep between retries."""
    with patch("custom_components.backrest.api._RETRY_DELAY", 0):
        yield


def _client(hass: HomeAssistant, username: str | None = "admin", password: str | None = "pw"):
    return BackrestClient(
        async_get_clientsession(hass),
        build_base_url("backrest.local", 9898, False),
        username,
        password,
    )


def _response(**kwargs) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse("post", URL(BASE), **kwargs)


def _frame(message: dict, flags: int = 0) -> bytes:
    body = json.dumps(message).encode()
    return struct.pack(">BI", flags, len(body)) + body


def test_build_base_url() -> None:
    """Hosts, IPv6 addresses and HTTPS are handled."""
    assert str(build_base_url("nas", 9898, False)) == "http://nas:9898"
    assert str(build_base_url(" nas.example.com ", 443, True)) == "https://nas.example.com"
    assert str(build_base_url("[fd00::1]", 9898, False)) == "http://[fd00::1]:9898"


async def test_get_config_without_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Unary calls are JSON POSTs to /<service>/<method>."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", json={"instance": "x", "modno": 1})
    client = _client(hass, None, None)
    assert await client.async_get_config() == {"instance": "x", "modno": 1}
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert method.lower() == "post"
    assert str(url) == f"{BASE}/v1.Backrest/GetConfig"
    assert data == {}
    assert headers["Connect-Protocol-Version"] == "1"
    assert "Authorization" not in headers


async def test_login_on_unauthorized(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 triggers a login and the call is retried with the bearer token."""
    calls = 0

    async def get_config(method, url, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response(status=401, text="Unauthorized (No Authorization Header)")
        return _response(json={"instance": "x"})

    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", side_effect=get_config)
    aioclient_mock.post(f"{BASE}/v1.Authentication/Login", json={"token": "jwt-token"})
    client = _client(hass)
    assert await client.async_get_config() == {"instance": "x"}
    assert client.authenticated
    login = aioclient_mock.mock_calls[1]
    assert login[2] == {"username": "admin", "password": "pw"}
    retried = aioclient_mock.mock_calls[2]
    assert retried[3]["Authorization"] == "Bearer jwt-token"


async def test_invalid_credentials(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Rejected logins raise BackrestAuthError."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", status=401, text="Unauthorized")
    aioclient_mock.post(
        f"{BASE}/v1.Authentication/Login",
        status=401,
        json={"code": "unauthenticated", "message": "invalid password"},
    )
    with pytest.raises(BackrestAuthError, match="invalid password"):
        await _client(hass).async_get_config()


async def test_auth_required_without_credentials(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without credentials a 401 is raised immediately."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", status=401, text="Unauthorized")
    with pytest.raises(BackrestAuthError):
        await _client(hass, None, None).async_get_config()
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("status", "payload", "exception"),
    [
        (404, {"code": "not_found", "message": "plan not found"}, BackrestNotFoundError),
        (404, {"code": "unimplemented", "message": ""}, BackrestUnsupportedError),
        (404, None, BackrestUnsupportedError),
        (500, {"code": "internal", "message": "boom"}, BackrestApiError),
        (502, None, BackrestResponseError),
        (403, {"code": "permission_denied", "message": "no"}, BackrestAuthError),
    ],
)
async def test_connect_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    payload: dict | None,
    exception: type[Exception],
) -> None:
    """Connect error codes map to exceptions."""
    if payload is None:
        aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", status=status, text="<html>")
    else:
        aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", status=status, json=payload)
    with pytest.raises(exception):
        await _client(hass, None, None).async_get_config()


async def test_not_backrest(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """An HTML page instead of JSON means the server is not Backrest."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", text="<!doctype html><html></html>")
    with pytest.raises(BackrestResponseError):
        await _client(hass, None, None).async_get_config()


async def test_timeout_is_retried(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Read calls are retried once; persistent timeouts raise."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", exc=TimeoutError())
    with pytest.raises(BackrestTimeoutError):
        await _client(hass, None, None).async_get_config()
    assert aioclient_mock.call_count == 2


async def test_connection_error_recovers_on_retry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A transient connection error is retried."""
    calls = 0

    async def flaky(method, url, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise aiohttp.ClientConnectionError
        return _response(json={"instance": "x"})

    aioclient_mock.post(f"{BASE}/v1.Backrest/GetConfig", side_effect=flaky)
    assert await _client(hass, None, None).async_get_config() == {"instance": "x"}


async def test_backup_is_not_retried(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Starting a backup is never retried to avoid duplicate runs."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/Backup", exc=aiohttp.ClientConnectionError())
    with pytest.raises(BackrestConnectionError):
        await _client(hass, None, None).async_backup("nas")
    assert aioclient_mock.call_count == 1


async def test_backup_payload(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The Backup RPC takes the plan ID as 'value'."""
    aioclient_mock.post(f"{BASE}/v1.Backrest/Backup", json={})
    await _client(hass, None, None).async_backup("nas")
    assert aioclient_mock.mock_calls[0][2] == {"value": "nas"}


async def test_get_operations(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Operations are requested per plan and instance with lastN as int64 string."""
    aioclient_mock.post(
        f"{BASE}/v1.Backrest/GetOperations",
        json={"operations": [{"id": "1"}, "garbage", {"id": "2"}]},
    )
    result = await _client(hass, None, None).async_get_operations("nas", "inst", 50)
    assert result == [{"id": "1"}, {"id": "2"}]
    assert aioclient_mock.mock_calls[0][2] == {
        "selector": {"planId": "nas", "instanceId": "inst"},
        "lastN": "50",
    }


async def test_operation_events_stream(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Enveloped stream frames are decoded until the end-of-stream frame."""
    body = (
        _frame({"keepAlive": {}})
        + _frame({"createdOperations": {"operations": [{"id": "5"}]}})
        + _frame({}, flags=0x02)
    )
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetOperationEvents", content=body)
    events = [event async for event in _client(hass, None, None).async_operation_events()]
    assert events == [{"keepAlive": {}}, {"createdOperations": {"operations": [{"id": "5"}]}}]
    sent = aioclient_mock.mock_calls[0][2]
    assert sent == struct.pack(">BI", 0, 2) + b"{}"


async def test_operation_events_stream_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An error in the end-of-stream frame is raised."""
    body = _frame({"error": {"code": "unimplemented", "message": "nope"}}, flags=0x02)
    aioclient_mock.post(f"{BASE}/v1.Backrest/GetOperationEvents", content=body)
    with pytest.raises(BackrestUnsupportedError):
        async for _ in _client(hass, None, None).async_operation_events():
            pass


async def test_operation_events_truncated(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A frame cut off mid-way is a response error."""
    aioclient_mock.post(
        f"{BASE}/v1.Backrest/GetOperationEvents", content=_frame({"keepAlive": {}})[:8]
    )
    with pytest.raises(BackrestResponseError):
        async for _ in _client(hass, None, None).async_operation_events():
            pass


async def test_peer_states_stream(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The stream is subscribed and read until every expected peer arrived."""
    body = (
        _frame({"peerKeyid": "k1", "peerInstanceId": "netbird-vps", "state": "A"})
        + _frame({"peerKeyid": "k1", "peerInstanceId": "netbird-vps", "state": "B"})
        + _frame({"peerKeyid": "k2", "peerInstanceId": "pangolin-vps"})
        + _frame({"peerKeyid": "k3", "peerInstanceId": "not-read"})
    )
    aioclient_mock.post(
        f"{BASE}/v1sync.BackrestSyncStateService/GetPeerSyncStatesStream", content=body
    )
    peers = await _client(hass, None, None).async_get_peer_states(2)
    assert [(p["peerInstanceId"], p.get("state")) for p in peers] == [
        ("netbird-vps", "B"),
        ("pangolin-vps", None),
    ]
    sent = aioclient_mock.mock_calls[0][2]
    assert sent == _frame({"subscribe": True})


async def test_peer_states_without_peers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without configured peers the stream is not opened at all."""
    assert await _client(hass, None, None).async_get_peer_states(0) == []
    assert aioclient_mock.call_count == 0


async def test_peer_states_stream_ends_early(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A stream that ends before all peers arrived returns what was received."""
    body = _frame({"peerKeyid": "k1", "peerInstanceId": "netbird-vps"}) + _frame(
        {"error": {"code": "canceled", "message": "context canceled"}}, flags=0x02
    )
    aioclient_mock.post(
        f"{BASE}/v1sync.BackrestSyncStateService/GetPeerSyncStatesStream", content=body
    )
    peers = await _client(hass, None, None).async_get_peer_states(2)
    assert [p["peerInstanceId"] for p in peers] == ["netbird-vps"]


async def test_peer_states_unsupported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Servers without multihost sync answer 404."""
    aioclient_mock.post(
        f"{BASE}/v1sync.BackrestSyncStateService/GetPeerSyncStatesStream", status=404, text=""
    )
    with pytest.raises(BackrestUnsupportedError):
        await _client(hass, None, None).async_get_peer_states(1)
