"""Non-finite numbers must not escape HTTP or event response validation."""

import pytest
from test_client import make_client

from custom_components.vacuula.client import VacuulaError


@pytest.mark.parametrize("number", [b"1e999", b"-1e999"])
async def test_json_exponent_overflow_is_rejected_in_http(number):
    client, _, _ = make_client(raw=b'{"charge":' + number + b"}")
    with pytest.raises(VacuulaError, match="Invalid JSON"):
        await client.async_server_info()


@pytest.mark.parametrize("number", [b"1e999", b"-1e999"])
async def test_json_exponent_overflow_is_rejected_in_events(number):
    client, _, _ = make_client(
        raw=b'event: robots\ndata: {"robot":{"charge":' + number + b"}}\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    with pytest.raises(VacuulaError, match="Invalid JSON"):
        _ = [event async for event in client.async_server_events()]


async def test_finite_exponents_remain_valid():
    client, _, _ = make_client(raw=b'{"temperature":1.25e2}')
    assert await client.async_server_info() == {"temperature": 125.0}
