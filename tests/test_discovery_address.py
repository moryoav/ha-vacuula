"""Keep a working configured address when the server is rediscovered."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_server_identity import advertisement

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN

DISCOVERED = "http://192.0.2.1:81"


def offline(client_cls, *urls):
    """Make the given origins unreachable while other origins use the mock."""
    working = client_cls.return_value
    unreachable = MagicMock()
    unreachable.async_server_info = AsyncMock(side_effect=CannotConnect("offline"))
    unreachable.async_robots = AsyncMock(side_effect=CannotConnect("offline"))
    client_cls.side_effect = lambda _session, url: (
        unreachable if url in urls else working
    )


@pytest.fixture
def api():
    """Answer every origin as the same server with one known robot."""
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as client:
        client.return_value.async_server_info = AsyncMock(return_value={})
        client.return_value.async_robots = AsyncMock(return_value={"R1": {}})
        yield client


@pytest.fixture
def reload(hass):
    """Record entry reloads without loading the integration."""
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as mock:
        yield mock


def loaded_entry(hass, url):
    """Add a loaded entry for robot R1 configured with the given origin."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=url, data={"url": url, "robot_ids": ["R1"]}
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def rediscover(hass):
    """Deliver the server's announcement and return the flow result."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )


async def test_advertised_hostname_is_kept(hass, api, reload):
    """Keep the server's own advertised hostname instead of its current address."""
    entry = loaded_entry(hass, "http://vacuula.local:81")
    result = await rediscover(hass)
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://vacuula.local:81"
    assert entry.unique_id == "http://vacuula.local:81"
    assert DISCOVERED in entry.data["url_aliases"]
    reload.assert_not_awaited()


async def test_working_reverse_proxy_is_kept(hass, api, reload):
    """Keep a configured HTTPS origin that still reaches the same robots."""
    entry = loaded_entry(hass, "https://vacuula.example.org")
    await rediscover(hass)
    assert entry.data["url"] == "https://vacuula.example.org"
    reload.assert_not_awaited()


async def test_unreachable_https_origin_is_not_downgraded(hass, api, reload):
    """Never replace an HTTPS origin with a plain HTTP announcement."""
    offline(api, "https://vacuula.example.org")
    entry = loaded_entry(hass, "https://vacuula.example.org")
    await rediscover(hass)
    assert entry.data["url"] == "https://vacuula.example.org"
    reload.assert_not_awaited()


async def test_working_other_hostname_is_kept(hass, api, reload):
    """Keep a different HTTP name that still answers for the same robots."""
    entry = loaded_entry(hass, "http://robots.lan:81")
    await rediscover(hass)
    assert entry.data["url"] == "http://robots.lan:81"
    reload.assert_not_awaited()


async def test_unreachable_address_follows_discovery(hass, api, reload):
    """Replace an address that no longer answers with the announced origin."""
    offline(api, "http://old.local:81")
    entry = loaded_entry(hass, "http://old.local:81")
    await rediscover(hass)
    assert entry.data["url"] == DISCOVERED
    assert entry.unique_id == DISCOVERED
    reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("stored", ["vacuula", None])
async def test_malformed_stored_address_follows_discovery(hass, api, reload, stored):
    """Repair a missing or malformed stored address from the announcement."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="broken", data={"url": stored, "robot_ids": ["R1"]}
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    await rediscover(hass)
    assert entry.data["url"] == DISCOVERED
    reload.assert_awaited_once_with(entry.entry_id)


async def test_address_now_serving_other_robots_follows_discovery(hass, api, reload):
    """Replace an address that now reaches a different server."""
    other = MagicMock()
    other.async_server_info = AsyncMock(return_value={})
    other.async_robots = AsyncMock(return_value={"OTHER": {}})
    working = api.return_value
    api.side_effect = lambda _session, url: (
        other if url == "http://old.local:81" else working
    )
    entry = loaded_entry(hass, "http://old.local:81")
    await rediscover(hass)
    assert entry.data["url"] == DISCOVERED
    reload.assert_awaited_once_with(entry.entry_id)


async def test_slow_configured_address_follows_discovery(hass, api, reload):
    """Treat a configured address that does not answer in time as moved."""

    async def hang():
        """Never answer within the address check timeout."""
        await asyncio.sleep(10)

    slow = MagicMock()
    slow.async_server_info = AsyncMock(side_effect=hang)
    working = api.return_value
    api.side_effect = lambda _session, url: (
        slow if url == "http://old.local:81" else working
    )
    entry = loaded_entry(hass, "http://old.local:81")
    with patch("custom_components.vacuula.config_flow.CURRENT_URL_CHECK_TIMEOUT", 0.01):
        await rediscover(hass)
    assert entry.data["url"] == DISCOVERED
