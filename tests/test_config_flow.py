"""Validate setup, duplicate prevention, discovery and reconfiguration."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.config_flow import normalize_url
from custom_components.vacuula.const import DOMAIN


@pytest.mark.parametrize(
    "value",
    [
        "ftp://host",
        "http://user:password@host",
        "http://host/web",
        "http://host?token=secret",
        "http://host#fragment",
        "host:81",
        "http://",
    ],
)
def test_invalid_urls(value):
    """Reject addresses with unsupported schemes or unsafe URL components."""
    with pytest.raises(ValueError):
        normalize_url(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" http://EXAMPLE.local:81/ ", "http://example.local:81"),
        ("https://server:443", "https://server"),
        ("http://[::1]:81", "http://[::1]:81"),
    ],
)
def test_normalize(value, expected):
    """Canonicalize host casing, default ports and trailing slashes."""
    assert normalize_url(value) == expected


@pytest.fixture
def no_discovery():
    """Force manual setup by returning no mDNS server candidates."""
    with patch(
        "custom_components.vacuula.config_flow.async_discover_servers",
        new=AsyncMock(return_value=[]),
    ):
        yield


@pytest.fixture
def client():
    """Mock a reachable server with one known robot for configuration flows."""
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as cls:
        instance = cls.return_value
        instance.async_server_info = AsyncMock(return_value={"version": "1.0.0"})
        instance.async_robots = AsyncMock(return_value={"TEST-robot": {}})
        yield instance


async def test_user(hass, client, no_discovery):
    """Create a normalized manual entry and save its discovered robot IDs."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://test.local:81/"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "url": "http://test.local:81",
        "robot_ids": ["TEST-robot"],
    }


async def test_user_errors(hass, client, no_discovery):
    """Keep the manual form open for invalid URLs and unreachable servers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "bad"}
    )
    assert result["errors"] == {"url": "invalid_url"}
    client.async_robots.side_effect = CannotConnect("offline")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://test.local:81"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize("same_url", [True, False])
async def test_duplicate(hass, client, same_url):
    """Reject duplicate servers identified by URL or overlapping robot IDs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": "http://test.local:81" if same_url else "http://old.local:81",
            "robot_ids": [] if same_url else ["TEST-robot"],
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}, data={"url": "http://test.local:81"}
    )
    assert result["reason"] == "already_configured"


def discovery():
    """Build a realistic IPv4 Vacuula zeroconf advertisement on port 81."""
    return ZeroconfServiceInfo(
        ip_address="192.0.2.1",
        ip_addresses=["192.0.2.1"],
        hostname="vacuula.local.",
        name="Vacuula Server._vacuula._tcp.local.",
        port=81,
        properties={"protocol": "http"},
        type="_vacuula._tcp.local.",
    )


async def test_zeroconf(hass, client):
    """Confirm a reachable advertised server before creating its entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=discovery()
    )
    assert result["step_id"] == "confirm"
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY


async def test_discovery_errors(hass, client):
    """Abort zeroconf setup for unreachable or already configured servers."""
    client.async_server_info.side_effect = CannotConnect("offline")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=discovery()
    )
    assert result["reason"] == "cannot_connect"
    client.async_server_info.side_effect = None
    MockConfigEntry(
        domain=DOMAIN, data={"url": "http://192.0.2.1:81", "robot_ids": []}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=discovery()
    )
    assert result["reason"] == "already_configured"


async def test_reconfigure(hass, client):
    """Validate the replacement server before updating and reloading its URL."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://old.local:81", "robot_ids": ["TEST-robot"]}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "bad"}
    )
    assert result["errors"] == {"url": "invalid_url"}
    client.async_server_info.side_effect = CannotConnect("offline")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://new.local:81"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    client.async_server_info.side_effect = None
    client.async_robots.return_value = {"OTHER": {}}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://new.local:81"}
    )
    assert result["errors"] == {"base": "wrong_server"}
    client.async_robots.return_value = {"TEST-robot": {}}
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://new.local:81"}
        )
        await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == "http://new.local:81"


async def test_reconfigure_duplicate(hass, client):
    """Refuse a replacement URL already assigned to another config entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://old.local:81"})
    entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, data={"url": "http://new.local:81"}).add_to_hass(
        hass
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data={"url": "http://new.local:81"},
    )
    assert result["reason"] == "already_configured"
