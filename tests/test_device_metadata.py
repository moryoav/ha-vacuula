"""Keep Home Assistant's device metadata current after later robot reads."""

import pytest
from homeassistant.helpers import device_registry as dr
from test_integration import loaded  # noqa: F401

from custom_components.vacuula.const import DOMAIN, GEN3, GEN4


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
async def test_device_firmware_follows_later_confirmed_info(hass, loaded):  # noqa: F811
    """Refresh firmware metadata without reloading or adding another entity."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, "TEST-robot"), entry.entry_id
    )
    assert device is not None
    assert device.sw_version == "1.4.0"
    registry.async_update_device(device.id, name_by_user="My robot")
    original_command = server.client.async_command.side_effect

    async def command(serial, payload):
        """Change confirmed firmware while retaining every existing telemetry key."""
        response = await original_command(serial, payload)
        if payload.get("cmd") == "getGeneralInfo":
            response["data"]["firmware"] = "1.4.1"
        elif payload.get("ability") == "info.robot":
            response["body"]["firmware"] = "1.4.1"
        return response

    server.client.async_command.side_effect = command
    existing_entities = set(hass.states.async_entity_ids())
    assert await coordinator.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()

    assert coordinator.robots["TEST-robot"].info["firmware"] == "1.4.1"
    assert set(hass.states.async_entity_ids()) == existing_entities
    refreshed = registry.async_get(device.id)
    assert refreshed is not None
    assert refreshed.sw_version == "1.4.1"
    assert refreshed.name_by_user == "My robot"
    assert refreshed.manufacturer == "Neato"
    assert refreshed.model == ("D7" if generation == GEN3 else "D10")
