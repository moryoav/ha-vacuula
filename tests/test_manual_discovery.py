"""Find advertised servers when setup is started from Add integration."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN


def advertised_server(address="192.0.2.1", port=8099, name="Vacuula Server"):
    """Describe a service without depending on a physical LAN advertiser."""
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        hostname="vacuula.local.",
        name=f"{name}._vacuula._tcp.local.",
        port=port,
        properties={"protocol": "http"},
        type="_vacuula._tcp.local.",
    )


@pytest.fixture
def search():
    with patch(
        "custom_components.vacuula.config_flow.async_discover_servers",
        new_callable=AsyncMock,
        return_value=[],
    ) as discover:
        yield discover


@pytest.fixture
def api():
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as client:
        client.return_value.async_server_info = AsyncMock(
            return_value={"version": "1.0.0"}
        )
        client.return_value.async_robots = AsyncMock(return_value={"NEW-robot": {}})
        yield client


def assert_confirmation(result):
    """Discovery confirmation must never ask for an address."""
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result.get("data_schema") is None or not result["data_schema"].schema


async def finish_setup(hass, result):
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    return result


async def test_single_server_uses_advertised_port(hass, search, api):
    search.return_value = [advertised_server()]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(result)
    search.assert_awaited_once()
    assert api.call_args.args[-1] == "http://192.0.2.1:8099"

    result = await finish_setup(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "url": "http://192.0.2.1:8099",
        "robot_ids": ["NEW-robot"],
        "url_aliases": ["http://192.0.2.1:8099", "http://vacuula.local:8099"],
    }
    assert result["result"].unique_id == "http://192.0.2.1:8099"


async def test_multiple_servers_offer_selection(hass, search, api):
    search.return_value = [
        advertised_server(),
        advertised_server("192.0.2.2", 12345, "Other Vacuula"),
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "select_server"
    api.assert_not_called()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://192.0.2.2:12345"}
    )
    assert_confirmation(result)
    result = await finish_setup(hass, result)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == "http://192.0.2.2:12345"


async def test_no_advertisement_falls_back_to_address_form(hass, search, api):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert "url" in result["data_schema"].schema
    search.assert_awaited_once()
    api.assert_not_called()


async def test_duplicate_advertisements_offer_one_confirmation(hass, search, api):
    search.return_value = [
        advertised_server(port=80),
        advertised_server(port=80, name="Vacuula Alias"),
    ]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(result)
    assert api.call_args.args[-1] == "http://192.0.2.1"
    api.return_value.async_server_info.assert_awaited_once()


async def test_only_configured_server_allows_adding_another_server(hass, search, api):
    MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://192.0.2.1:8099", "robot_ids": ["EXISTING-robot"]},
    ).add_to_hass(hass)
    search.return_value = [advertised_server()]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    api.assert_not_called()

    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://192.0.2.2:81"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "url": "http://192.0.2.2:81",
        "robot_ids": ["NEW-robot"],
    }
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    search.assert_awaited_once()


@pytest.mark.parametrize("multiple", [False, True])
async def test_unreachable_advertisement_allows_a_working_address(
    hass, search, api, multiple
):
    search.return_value = [advertised_server()]
    if multiple:
        search.return_value.append(advertised_server("192.0.2.2"))
    api.return_value.async_server_info.side_effect = CannotConnect("unreachable")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    if multiple:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://192.0.2.1:8099"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}
    url_key = next(iter(result["data_schema"].schema))
    assert url_key.default() == "http://192.0.2.1:8099"

    api.return_value.async_server_info.side_effect = None
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://192.0.2.3:81"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "url": "http://192.0.2.3:81",
        "robot_ids": ["NEW-robot"],
    }
    assert result["result"].unique_id == "http://192.0.2.3:81"
    search.assert_awaited_once()


async def test_unreachable_advertisement_fallback_preserves_duplicate_prevention(
    hass, search, api
):
    MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://192.0.2.3:81", "robot_ids": ["NEW-robot"]},
    ).add_to_hass(hass)
    search.return_value = [advertised_server()]
    api.return_value.async_server_info.side_effect = CannotConnect("unreachable")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["step_id"] == "user"
    api.return_value.async_server_info.side_effect = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": "http://192.0.2.3:81"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_discovered_robot_identity_duplicate_offers_another_address(
    hass, search, api
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://192.0.2.3:81", "robot_ids": ["NEW-robot"]},
    )
    entry.add_to_hass(hass)
    search.return_value = [advertised_server()]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert entry.data["url"] == "http://192.0.2.1:8099"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1

    api.return_value.async_robots.return_value = {"OTHER-robot": {}}
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": "http://192.0.2.4:81"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_configured_servers_are_excluded_from_selection(hass, search, api):
    MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://192.0.2.1:8099", "robot_ids": ["EXISTING-robot"]},
    ).add_to_hass(hass)
    search.return_value = [advertised_server(), advertised_server("192.0.2.2")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(result)
    assert api.call_args.args[-1] == "http://192.0.2.2:8099"


async def test_malformed_advertisement_falls_back_to_address_form(hass, search, api):
    search.return_value = [advertised_server(port=0)]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    api.assert_not_called()


@pytest.mark.parametrize("same_url", [True, False])
async def test_confirmation_connection_failure_allows_address_retry(
    hass, search, api, same_url
):
    search.return_value = [advertised_server()]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(result)
    api.return_value.async_server_info.side_effect = CannotConnect("server offline")

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert "confirm_only" not in flow["context"]

    api.return_value.async_server_info.side_effect = None
    base_url = "http://192.0.2.1:8099" if same_url else "http://192.0.2.3:81"
    with patch("custom_components.vacuula.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": base_url}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["url"] == base_url
    assert ("url_aliases" in result["data"]) is same_url
    search.assert_awaited_once()


@pytest.mark.parametrize("same_url", [True, False])
async def test_confirmation_rechecks_entries_created_during_setup(
    hass, search, api, same_url
):
    search.return_value = [advertised_server()]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(result)
    MockConfigEntry(
        domain=DOMAIN,
        data={
            "url": "http://192.0.2.1:8099" if same_url else "http://other.local:81",
            "robot_ids": [] if same_url else ["NEW-robot"],
        },
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_setup_can_complete_with_existing_discovery_flow(
    hass, search, api
):
    service = advertised_server()
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=service
    )
    assert_confirmation(discovered)
    search.return_value = [service]

    manual = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert_confirmation(manual)
    manual = await finish_setup(hass, manual)
    assert manual["type"] == FlowResultType.CREATE_ENTRY

    assert discovered["flow_id"] not in {
        flow["flow_id"] for flow in hass.config_entries.flow.async_progress()
    }
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "zeroconf"}, data=service
    )
    assert discovered["type"] == FlowResultType.ABORT
    assert discovered["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
