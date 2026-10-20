"""Verify map-handler completion evidence and the server's optional boot identity."""

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_client import make_client

from custom_components.vacuula.client import (
    REQUEST_TIMEOUT,
    CannotConnect,
    CommandError,
    MapRequestNotSent,
    MapRequestRejected,
    VacuulaClient,
    VacuulaError,
)


@pytest.mark.parametrize(
    ("action", "status", "error"),
    [
        ("promote", 400, "Robot not connected"),
        ("promote", 404, "Map not found"),
        ("promote", 502, "Failed to send promote to robot"),
        ("promote", 502, "Robot rejected promote (status 400)"),
        ("promote", 504, "Robot did not acknowledge promote in time"),
        ("boundaries", 400, "not all required keys"),
        ("boundaries", 400, "Invalid JSON body"),
        ("boundaries", 400, "Invalid Content-Length header"),
        ("demote", 400, "Robot not connected"),
        ("delete", 400, "Robot not connected"),
    ],
)
async def test_exact_map_handler_rejections_have_safe_completion_evidence(
    action, status, error
):
    """Identify known completed handlers without retaining raw server error text."""
    client, session, _ = make_client({"error": error}, status=status)
    with pytest.raises(MapRequestRejected) as caught:
        await client.async_map_action("robot", "map", action, [])
    assert caught.value.status == status
    assert str(caught.value) == "Vacuula completed and rejected the map request"
    assert error not in str(caught.value)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    ("action", "status", "payload"),
    [
        ("promote", 500, {"error": "Internal Server Error"}),
        ("boundaries", 500, {"error": "Internal Server Error"}),
        ("promote", 504, {"error": "Gateway timeout"}),
        ("promote", 400, {"error": "Robot rejected promote (status 400)"}),
        ("boundaries", 400, {"error": "Robot not connected"}),
        ("use", 400, {"error": "Robot not connected"}),
        ("unuse", 400, {"error": "Robot not connected"}),
        ("promote", 502, {"error": "Robot rejected promote (status PRIVATE)"}),
        ("promote", 400, {"error": None}),
        ("promote", 400, ["Robot not connected"]),
        (
            "promote",
            400,
            {"error": "Robot not connected", "extra": "PRIVATE_RESPONSE"},
        ),
    ],
)
async def test_unknown_map_failures_do_not_claim_handler_completion(
    action, status, payload
):
    """Leave proxy, unexpected and malformed responses ambiguous and private."""
    client, _, _ = make_client(payload, status=status)
    with pytest.raises(VacuulaError) as caught:
        await client.async_map_action("robot", "map", action, [])
    assert not isinstance(caught.value, MapRequestRejected)
    assert "PRIVATE" not in str(caught.value)


async def test_non_json_gateway_failure_does_not_claim_map_handler_completion():
    """A proxy's HTML timeout response cannot resolve a map-operation fence."""
    client, _, _ = make_client(raw=b"PRIVATE_GATEWAY_HTML", status=504)
    with pytest.raises(VacuulaError) as caught:
        await client.async_map_action("robot", "map", "promote")
    assert not isinstance(caught.value, MapRequestRejected)
    assert "PRIVATE" not in str(caught.value)


async def test_generic_api_rejection_keeps_its_existing_classification():
    """Only map-action endpoints can return the specialized completion signal."""
    client, _, _ = make_client({"error": "Robot not connected"}, status=400)
    with pytest.raises(CommandError) as caught:
        await client.async_server_info()
    assert not isinstance(caught.value, MapRequestRejected)


@pytest.mark.parametrize(
    "action", ["promote", "boundaries", "use", "unuse", "demote", "delete"]
)
async def test_map_connect_refusal_proves_no_request_was_sent(monkeypatch, action):
    """Classify real aiohttp connection setup failure before sending a map write."""
    attempts = []

    async def refuse_connection(connector, request, traces, timeout):
        """Fail TCP connection creation without constructing an HTTP transport."""
        attempts.append(request.method)
        raise aiohttp.ClientConnectorError(
            request.connection_key, ConnectionRefusedError(111, "PRIVATE_DESTINATION")
        )

    monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", refuse_connection)
    async with aiohttp.ClientSession() as session:
        client = VacuulaClient(session, "http://vacuula.test:81")
        with pytest.raises(MapRequestNotSent) as caught:
            await client.async_map_action("robot", "map", action, [])
    assert attempts == ["DELETE" if action in {"demote", "delete"} else "POST"]
    assert isinstance(caught.value.__cause__, aiohttp.ClientConnectorError)
    assert "PRIVATE" not in str(caught.value)


async def test_generic_connect_refusal_keeps_existing_error_class(monkeypatch):
    """Keep ordinary API connection failures as CannotConnect rather than map state."""

    async def refuse_connection(connector, request, traces, timeout):
        """Refuse the real session's connection before it can dispatch GET."""
        raise aiohttp.ClientConnectorError(
            request.connection_key, ConnectionRefusedError(111, "PRIVATE_DESTINATION")
        )

    monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", refuse_connection)
    async with aiohttp.ClientSession() as session:
        client = VacuulaClient(session, "http://vacuula.test:81")
        with pytest.raises(CannotConnect) as caught:
            await client.async_server_info()
    assert type(caught.value) is CannotConnect


async def test_disconnected_map_reply_does_not_claim_request_was_unsent():
    """A disconnected response may follow dispatch and must retain uncertainty."""
    client, session, response = make_client()
    response.__aenter__.side_effect = aiohttp.ServerDisconnectedError("Reply lost")
    with pytest.raises(CannotConnect) as caught:
        await client.async_map_action("robot", "map", "promote")
    assert not isinstance(caught.value, MapRequestNotSent)
    session.request.assert_called_once()


@pytest.mark.usefixtures("socket_enabled")
@pytest.mark.parametrize("action", ["promote", "demote"])
async def test_actual_dropped_map_response_is_not_replayed(action):
    """Send exactly one real POST or DELETE when the server drops its response."""
    received = []

    async def disconnect(request):
        """Record dispatch and disconnect only after reading the actual request."""
        received.append(request.method)
        await request.read()
        assert request.transport is not None
        request.transport.close()
        return web.Response(text="Reply intentionally unavailable")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", disconnect)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = VacuulaClient(session, str(server.make_url("/")))
        with pytest.raises(CannotConnect) as caught:
            await client.async_map_action("robot", "map", action)
        assert not isinstance(caught.value, MapRequestNotSent)
        assert not session.closed
    assert received == ["DELETE" if action == "demote" else "POST"]


@pytest.mark.parametrize(
    ("etag", "expected"),
    [
        ('"012345abcdef"', '"012345abcdef"'),
        (None, None),
        (123, None),
        ("012345abcdef", None),
        ('W/"012345abcdef"', None),
        ('"012345abcde"', None),
        ('"012345abcdef0"', None),
        ('"012345ABCDEF"', None),
        ('"012345abcdeg"', None),
        ('"012345abcdef"\n', None),
    ],
)
async def test_boot_identity_requires_the_exact_frontend_etag(etag, expected):
    """Accept only Vacuula's quoted lowercase boot token without reading HTML."""
    client, session, response = make_client(raw=b"PRIVATE_HTML", headers={"ETag": etag})
    assert await client.async_server_boot_id() == expected
    request = session.request.call_args
    assert request.args[0] == "GET"
    assert request.args[1].raw_path == "/web"
    assert request.kwargs["headers"] == {"Cache-Control": "no-cache"}
    assert request.kwargs["timeout"] is REQUEST_TIMEOUT
    assert request.kwargs["allow_redirects"] is False
    response.content.iter_chunked.assert_not_called()
    response.__aexit__.assert_awaited_once()
    session.request.assert_called_once()


@pytest.mark.parametrize("status", [301, 304, 401, 404, 500])
async def test_failed_boot_probe_does_not_use_an_etag(status):
    """Treat redirects, cached replies and missing frontends as unknown identities."""
    client, session, _ = make_client(status=status, headers={"ETag": '"012345abcdef"'})
    assert await client.async_server_boot_id() is None
    session.request.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError("PRIVATE_URL"),
        TimeoutError("PRIVATE_URL"),
        OSError("PRIVATE_URL"),
    ],
)
async def test_boot_probe_transport_failure_is_unknown_without_retry(failure):
    """A failed optional probe must not invent a restart or expose its URL."""
    client, session, response = make_client()
    response.__aenter__.side_effect = failure
    assert await client.async_server_boot_id() is None
    session.request.assert_called_once()
