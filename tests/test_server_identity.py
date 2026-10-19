"""Deduplicate empty servers using their actual network endpoint."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.config_flow import discovery_aliases, entry_aliases
from custom_components.vacuula.const import DOMAIN


def advertisement(address="192.0.2.1", port=81, protocol="http"):
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        hostname="vacuula.local.",
        name="Vacuula Server._vacuula._tcp.local.",
        port=port,
        properties={"protocol": protocol},
        type="_vacuula._tcp.local.",
    )


@pytest.fixture
def api():
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as client:
        client.return_value.async_server_info = AsyncMock(
            return_value={"version": "1.0.0", "timezone": "Asia/Jerusalem"}
        )
        client.return_value.async_robots = AsyncMock(return_value={})
        yield client


@pytest.fixture
def resolver():
    with patch(
        "custom_components.vacuula.config_flow."
        "HomeAssistantTCPConnector.async_resolve_host",
        new_callable=AsyncMock,
        return_value=[{"host": "192.0.2.1"}],
    ) as resolve:
        yield resolve


async def test_legacy_empty_hostname_entry_remains_single_when_robots_arrive(
    hass, api, resolver
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://vacuula.local:81",
        data={"url": "http://vacuula.local:81", "robot_ids": []},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )
    assert result["reason"] == "already_configured"
    assert entry.data["url_aliases"] == [
        "http://192.0.2.1:81",
        "http://vacuula.local:81",
    ]
    assert entry.data["robot_ids"] == []

    api.return_value.async_robots.return_value = {"TEST-robot": {}}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )
    assert result["reason"] == "already_configured"
    assert entry.data["robot_ids"] == ["TEST-robot"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    resolver.assert_not_awaited()


async def test_manual_search_recognizes_configured_empty_hostname_without_form(
    hass, api, resolver
):
    MockConfigEntry(
        domain=DOMAIN, data={"url": "http://vacuula.local:81", "robot_ids": []}
    ).add_to_hass(hass)
    with patch(
        "custom_components.vacuula.config_flow.async_discover_servers",
        new=AsyncMock(return_value=[advertisement()]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    api.assert_not_called()
    resolver.assert_not_awaited()


async def test_conflicting_advertised_hostname_does_not_hide_a_second_server(
    hass, api, resolver
):
    MockConfigEntry(
        domain=DOMAIN, data={"url": "http://vacuula.local:81", "robot_ids": []}
    ).add_to_hass(hass)
    with patch(
        "custom_components.vacuula.config_flow.async_discover_servers",
        new=AsyncMock(return_value=[advertisement(), advertisement("192.0.2.2")]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
    assert result["step_id"] == "confirm"
    assert api.call_args.args[-1] == "http://192.0.2.2:81"
    resolver.assert_awaited_once_with("vacuula.local")


async def test_discovered_empty_entry_remembers_hostname_for_manual_setup(
    hass, api, resolver
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["url_aliases"] == [
        "http://192.0.2.1:81",
        "http://vacuula.local:81",
    ]
    duplicate = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={"url": "http://vacuula.local.:81"},
    )
    assert duplicate["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    resolver.assert_not_awaited()


@pytest.mark.parametrize(
    ("address", "port", "protocol"),
    [
        ("192.0.2.1", 82, "http"),
        ("192.0.2.1", 81, "https"),
        ("192.0.2.2", 81, "http"),
    ],
)
async def test_distinct_empty_endpoints_are_not_merged(
    hass, api, resolver, address, port, protocol
):
    MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": "http://192.0.2.1:81",
            "robot_ids": [],
            "url_aliases": ["http://192.0.2.1:81", "http://vacuula.local:81"],
        },
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "zeroconf"},
        data=advertisement(address, port, protocol),
    )
    assert result["step_id"] == "confirm"
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    resolver.assert_not_awaited()


async def test_every_advertised_address_is_an_alias(hass, api, resolver):
    MockConfigEntry(
        domain=DOMAIN, data={"url": "http://[2001:db8::1]:81", "robot_ids": []}
    ).add_to_hass(hass)
    service = advertisement()
    service.ip_addresses.append("2001:db8::1")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=service
    )
    assert result["reason"] == "already_configured"
    assert discovery_aliases(service) == {
        "http://192.0.2.1:81",
        "http://[2001:db8::1]:81",
        "http://vacuula.local:81",
    }
    resolver.assert_not_awaited()


@pytest.mark.parametrize("manual_hostname", [True, False])
async def test_alternate_hostname_resolves_to_same_empty_endpoint(
    hass, api, resolver, manual_hostname
):
    existing = "http://cleaner.example:81" if manual_hostname else "http://192.0.2.1:81"
    entry = MockConfigEntry(domain=DOMAIN, data={"url": existing, "robot_ids": []})
    entry.add_to_hass(hass)
    if manual_hostname:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=advertisement()
        )
    else:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={"url": "http://cleaner.example:81"},
        )
    assert result["reason"] == "already_configured"
    assert {"http://cleaner.example:81", "http://192.0.2.1:81"} <= entry_aliases(entry)
    resolver.assert_awaited_once_with("cleaner.example")


@pytest.mark.parametrize("failure", ["different_address", "dns_error", "timeout"])
async def test_unverified_hostname_does_not_merge_empty_servers(
    hass, api, resolver, failure
):
    MockConfigEntry(
        domain=DOMAIN, data={"url": "http://other.example:81", "robot_ids": []}
    ).add_to_hass(hass)
    if failure == "different_address":
        resolver.return_value = [{"host": "192.0.2.99"}]
    elif failure == "dns_error":
        resolver.side_effect = OSError("Cannot resolve")
    else:

        async def never_resolves(host):
            await asyncio.Event().wait()

        resolver.side_effect = never_resolves
    with patch("custom_components.vacuula.config_flow.IDENTITY_RESOLVE_TIMEOUT", 0.001):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=advertisement()
        )
    assert result["step_id"] == "confirm"
    resolver.assert_awaited_once_with("other.example")


async def test_nonstandard_shared_connector_cannot_establish_dns_identity(
    hass, api, resolver
):
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://other.example:81", "robot_ids": []}
    )
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    with patch(
        "custom_components.vacuula.config_flow.async_get_clientsession",
        return_value=SimpleNamespace(connector=object()),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=advertisement()
        )
    assert result["step_id"] == "confirm"
    assert entry.data == original_data
    resolver.assert_not_awaited()


@pytest.mark.parametrize("invalid_url", [None, 123])
async def test_malformed_legacy_url_does_not_hide_a_different_valid_endpoint(
    hass, api, resolver, invalid_url
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": invalid_url,
            "url_aliases": ["http://192.0.2.99:81"],
            "robot_ids": [],
        },
    )
    entry.add_to_hass(hass)
    original_data = dict(entry.data)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )
    assert result["step_id"] == "confirm"
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == "http://192.0.2.1:81"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert entry.data == original_data
    resolver.assert_not_awaited()


async def test_empty_reconfigure_does_not_keep_aliases_of_different_endpoint(
    hass, api, resolver
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": "http://192.0.2.1:81",
            "robot_ids": [],
            "url_aliases": ["http://192.0.2.1:81", "http://vacuula.local:81"],
        },
    )
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
            data={"url": "http://192.0.2.2:82"},
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url_aliases"] == ["http://192.0.2.2:82"]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=advertisement()
    )
    assert result["step_id"] == "confirm"
    resolver.assert_not_awaited()


def test_optional_legacy_alias_data_is_validated():
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": "http://vacuula.local:81",
            "url_aliases": [None, "https://user:password@host", "http://192.0.2.1:81"],
        },
    )
    assert entry_aliases(entry) == {
        "http://vacuula.local:81",
        "http://192.0.2.1:81",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://vacuula.local:81", "url_aliases": "invalid"},
    )
    assert entry_aliases(entry) == {"http://vacuula.local:81"}
