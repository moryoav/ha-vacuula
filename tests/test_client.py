"""Protocol failure, framing and serialization regression tests."""

import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.vacuula.client import (
    CannotConnect,
    CommandError,
    InvalidAuth,
    UnsupportedCommand,
    VacuulaClient,
    VacuulaError,
    _SSEParser,
    unwrap,
)


def make_client(
    payload=None,
    *,
    raw=None,
    chunks=None,
    status=200,
    headers=None,
    content_length=None,
):
    """Build an async response without opening a network connection."""
    if raw is None:
        raw = json.dumps(payload if payload is not None else {}).encode()
    response = MagicMock(spec=aiohttp.ClientResponse)
    response.status = status
    response.headers = headers or {"Content-Type": "application/json"}
    response.content_length = content_length
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    response.content = MagicMock()

    async def iterate(_size):
        for chunk in chunks if chunks is not None else [raw]:
            yield chunk

    response.content.iter_chunked.side_effect = iterate
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = response
    return VacuulaClient(session, "http://vacuula.test:81"), session, response


@pytest.mark.parametrize(
    ("method", "args", "payload", "path"),
    [
        ("async_server_info", (), {"version": "1"}, "/api/server"),
        ("async_robots", (), {"robot": {"connected": True}}, "/api/robots"),
        ("async_maps", ("robot",), [{"id": "map"}], "/api/robot/robot/maps"),
        ("async_runs", ("robot",), [{"id": "run"}], "/api/robot/robot/runs"),
        ("async_map", ("robot", "map"), {"width": 2}, "/api/robot/robot/maps/map"),
        ("async_run", ("robot", "run"), {"width": 2}, "/api/robot/robot/run/run"),
        (
            "async_map_boundaries",
            ("robot", "map"),
            [],
            "/api/robot/robot/maps/map/boundaries",
        ),
    ],
)
async def test_get_resources(method, args, payload, path):
    client, session, response = make_client(payload)
    assert await getattr(client, method)(*args) == payload
    call = session.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1].raw_path == path
    assert call.kwargs["allow_redirects"] is False
    assert call.kwargs["timeout"].total > 30
    response.__aexit__.assert_awaited_once()


async def test_encode_identifiers_as_single_path_segments():
    client, session, _ = make_client({})
    await client.async_map("robot /?#%", "map/?#")
    assert session.request.call_args.args[1].raw_path == (
        "/api/robot/robot%20%2F%3F%23%25/maps/map%2F%3F%23"
    )


@pytest.mark.parametrize(
    "url",
    [
        "ftp://vacuula.test",
        "http://user:password@vacuula.test",
        "/relative",
        "http://vacuula.test?secret=value",
        "http://vacuula.test#fragment",
    ],
)
def test_reject_unsafe_base_urls(url):
    with pytest.raises(ValueError, match="server URL"):
        VacuulaClient(MagicMock(), url)


@pytest.mark.parametrize(
    ("payload", "command"),
    [
        ({"result": "ok", "data": {}}, {"cmd": "findMe"}),
        (
            {"type": "response", "code": 200, "body": {}},
            {"ability": "utilities.find_me"},
        ),
    ],
)
async def test_command_returns_full_envelope(payload, command):
    client, session, _ = make_client(payload)
    assert await client.async_command("robot", command) == payload
    assert session.request.call_args.kwargs["json"] == command
    session.request.assert_called_once()


@pytest.mark.parametrize(
    ("payload", "status", "error"),
    [
        ({"result": "bad_request", "error": "PRIVATE_RESPONSE"}, 200, CommandError),
        ({"result": "command_not_found"}, 400, UnsupportedCommand),
        ({"result": "not_implemented"}, 400, UnsupportedCommand),
        ({"code": 409, "body": {"error": "PRIVATE_RESPONSE"}}, 200, CommandError),
        ({"code": 501}, 200, UnsupportedCommand),
        ({"error": "PRIVATE_RESPONSE"}, 504, CommandError),
    ],
)
async def test_command_rejection(payload, status, error):
    client, session, _ = make_client(payload, status=status)
    with pytest.raises(error) as caught:
        await client.async_command("robot", {"cmd": "findMe"})
    assert "PRIVATE_RESPONSE" not in str(caught.value)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    ("payload", "command"),
    [
        ({}, {"cmd": "findMe"}),
        ({"result": None}, {"cmd": "findMe"}),
        ({}, {"ability": "state.show"}),
        ({"code": True}, {"ability": "state.show"}),
        ({"code": "200"}, {"ability": "state.show"}),
        ([], {"cmd": "findMe"}),
    ],
)
async def test_command_requires_explicit_success(payload, command):
    client, _, _ = make_client(payload)
    with pytest.raises(VacuulaError):
        await client.async_command("robot", command)


@pytest.mark.parametrize("command", [{}, {"cmd": "x", "ability": "y"}])
async def test_invalid_command_does_not_send(command):
    client, session, _ = make_client()
    with pytest.raises(ValueError):
        await client.async_command("robot", command)
    session.request.assert_not_called()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, InvalidAuth),
        (403, InvalidAuth),
        (301, CannotConnect),
        (302, CannotConnect),
        (500, CannotConnect),
        (404, CommandError),
    ],
)
async def test_safe_http_errors(status, error):
    client, session, _ = make_client(raw=b"PRIVATE_RESPONSE", status=status)
    with pytest.raises(error) as caught:
        await client.async_server_info()
    assert caught.value.status == status
    assert "PRIVATE_RESPONSE" not in str(caught.value)
    assert session.request.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "exception",
    [
        aiohttp.ClientConnectionError("PRIVATE_URL"),
        TimeoutError("PRIVATE_URL"),
        OSError("PRIVATE_URL"),
    ],
)
async def test_transport_failure_is_safe_and_not_retried(exception):
    client, session, response = make_client()
    response.__aenter__.side_effect = exception
    with pytest.raises(CannotConnect) as caught:
        await client.async_server_info()
    assert "PRIVATE_URL" not in str(caught.value)
    session.request.assert_called_once()


@pytest.mark.parametrize("raw", [b"no json", b"\xff", b'{"number": NaN}', b"["])
async def test_invalid_json(raw):
    client, _, _ = make_client(raw=raw)
    with pytest.raises(VacuulaError, match="Invalid JSON"):
        await client.async_server_info()


async def test_json_limit_applies_with_and_without_content_length(monkeypatch):
    monkeypatch.setattr("custom_components.vacuula.client.JSON_LIMIT", 8)
    for content_length in (20, None):
        client, _, _ = make_client(
            chunks=[b"12345", b"67890"], content_length=content_length
        )
        with pytest.raises(VacuulaError, match="size limit"):
            await client.async_server_info()


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("async_server_info", []),
        ("async_robots", {"robot": "wrong"}),
        ("async_maps", {}),
        ("async_runs", [5]),
    ],
)
async def test_resource_shapes(method, payload):
    client, _, _ = make_client(payload)
    with pytest.raises(VacuulaError):
        await getattr(client, method)(
            *(() if method in ("async_server_info", "async_robots") else ("robot",))
        )


async def test_mutations_accept_server_unquoted_ok():
    client, session, _ = make_client(raw=b"OK")
    await client.async_update_robot("robot", "Kitchen")
    assert session.request.call_args.kwargs["json"] == {"name": "Kitchen"}
    await client.async_update_map("robot", "map", "Downstairs", True)
    assert session.request.call_args.kwargs["json"] == {
        "name": "Downstairs",
        "enabled": True,
    }
    boundaries = [{"id": "zone"}]
    await client.async_map_action("robot", "map", "boundaries", boundaries)
    assert session.request.call_args.kwargs["json"] is boundaries
    await client.async_map_action("robot", "map", "demote")
    assert session.request.call_args.args[0] == "DELETE"
    assert session.request.call_args.args[1].raw_path == "/api/robot/robot/maps/map"
    with pytest.raises(ValueError):
        await client.async_map_action("robot", "map", "../command")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"result": "ok", "data": {}, "state": 1, "details": {"charge": 90}},
            {"state": 1, "details": {"charge": 90}},
        ),
        ({"result": "ok", "data": {"robotSounds": True}}, {"robotSounds": True}),
        ({"code": 200, "body": {"state": "idle"}}, {"state": "idle"}),
    ],
)
def test_unwrap_preserves_telemetry(payload, expected):
    assert unwrap(payload) == expected


async def test_sse_fragmented_crlf_multiline_named_and_keepalive():
    chunks = [
        b"\xef\xbb\xbf: keepalive\r",
        b'\n\r\nevent: robots\r\ndata: {\r\ndata: "robot": {"connected": true}}\r',
        b"\n\r\n",
    ]
    client, session, response = make_client(
        chunks=chunks, headers={"Content-Type": "text/event-stream; charset=utf-8"}
    )
    assert [event async for event in client.async_server_events()] == [
        {"robot": {"connected": True}}
    ]
    assert session.request.call_args.kwargs["timeout"].total is None
    response.__aexit__.assert_awaited_once()


async def test_robot_stream_opened_before_any_content_read():
    raw = b'data: {"serial":"robot","data":{}}\n\n'
    client, _, response = make_client(
        raw=raw, headers={"Content-Type": "text/event-stream"}
    )
    stream = client.async_robot_events("robot")
    assert await anext(stream) == {"_vacuula_stream_opened": True}
    response.content.iter_chunked.assert_not_called()
    assert await anext(stream) == {"serial": "robot", "data": {}}
    await stream.aclose()


async def test_sse_rejects_wrong_robot():
    client, _, _ = make_client(
        raw=b'data: {"serial":"other"}\n\n',
        headers={"Content-Type": "text/event-stream"},
    )
    stream = client.async_robot_events("robot")
    await anext(stream)
    with pytest.raises(VacuulaError, match="Unexpected robot"):
        await anext(stream)


@pytest.mark.parametrize("raw", [b"data: invalid\n\n", b"data: []\n\n"])
async def test_sse_malformed_robot_events(raw):
    client, _, _ = make_client(raw=raw, headers={"Content-Type": "text/event-stream"})
    stream = client.async_robot_events("robot")
    await anext(stream)
    with pytest.raises(VacuulaError):
        await anext(stream)


async def test_sse_wrong_content_type():
    client, _, _ = make_client()
    with pytest.raises(VacuulaError, match="event stream"):
        await anext(client.async_server_events())


async def test_sse_incomplete_frame_is_discarded():
    client, _, _ = make_client(
        raw=b"event: robots\ndata: {}\n", headers={"Content-Type": "text/event-stream"}
    )
    assert [event async for event in client.async_server_events()] == []


def test_sse_size_limit_accumulates_multiline_frames(monkeypatch):
    monkeypatch.setattr("custom_components.vacuula.client.SSE_LIMIT", 20)
    parser = _SSEParser()
    assert parser.feed(b"data: 12345\n") == []
    with pytest.raises(VacuulaError, match="size limit"):
        parser.feed(b"data: 67890\n")
    with pytest.raises(VacuulaError, match="size limit"):
        _SSEParser().feed(b"x" * 21)
