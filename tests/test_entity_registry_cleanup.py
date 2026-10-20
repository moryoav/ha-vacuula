"""Remove deleted server records even while their entities are disabled."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_integration import entity_id, loaded  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.image import async_setup_entry as setup_images


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_disabled_map_records_are_removed(hass, loaded):  # noqa: F811
    entry, server, _generation = loaded
    registry = er.async_get(hass)
    image = entity_id(hass, "image", "map_floor")
    switch = entity_id(hass, "switch", "map_floor_enabled")
    for record in (image, switch):
        registry.async_update_entity(record, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    for record in (image, switch):
        assert registry.async_get(record) is not None
        assert hass.states.get(record) is None

    server.client.async_maps.return_value = []
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    for record in (image, switch):
        assert registry.async_get(record) is None

    # Deleting a record also forgets its prior discovery, so it can return.
    server.client.async_maps.return_value = [
        {"id": "floor", "name": "Returned floor", "enabled": True}
    ]
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    for platform, key in (("image", "map_floor"), ("switch", "map_floor_enabled")):
        returned = entity_id(hass, platform, key)
        assert registry.async_get(returned) is not None
        # HA restores the user's prior disabled preference when an ID returns.
        assert registry.async_get(returned).disabled_by is er.RegistryEntryDisabler.USER
        assert hass.states.get(returned) is None


@pytest.mark.parametrize("loaded", [GEN4], indirect=True)
async def test_disabled_schedule_event_is_removed_and_can_return(hass, loaded):  # noqa: F811
    entry, server, _generation = loaded
    registry = er.async_get(hass)
    event = {
        "id": "weekly",
        "enabled": False,
        "time": "09:00",
        "days": ["mon"],
        "command": {"ability": "cleaning.start", "runs": []},
    }
    events = [event]
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        if payload.get("ability") == "scheduling.show":
            return {"code": 200, "body": {"events": deepcopy(events)}}
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    record = entity_id(hass, "switch", "schedule_event_weekly_enabled")
    registry.async_update_entity(record, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(record) is not None
    assert hass.states.get(record) is None

    events.clear()
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    assert registry.async_get(record) is None
    events.append(event)
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    returned = entity_id(hass, "switch", "schedule_event_weekly_enabled")
    assert registry.async_get(returned).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(returned) is None


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_failed_read_does_not_remove_disabled_map_records(hass, loaded):  # noqa: F811
    entry, server, _generation = loaded
    registry = er.async_get(hass)
    records = [
        entity_id(hass, "image", "map_floor"),
        entity_id(hass, "switch", "map_floor_enabled"),
    ]
    for record in records:
        registry.async_update_entity(record, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    server.client.async_maps.side_effect = CannotConnect("Map read failed")
    await entry.runtime_data.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    for record in records:
        assert registry.async_get(record) is not None


async def test_map_image_discovery_ignores_invalid_ids(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    entry.runtime_data = coordinator
    coordinator.robots = {
        "robot": RobotData(
            "robot",
            "Neato",
            GEN3,
            maps=[{"id": None}, {"id": []}, {"id": {}}, {"id": 1}, {"id": ""}],
        )
    }
    add = MagicMock()
    await setup_images(hass, entry, add)
    add.assert_not_called()


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_rapid_map_delete_and_return_keeps_entities(hass, loaded):  # noqa: F811
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    floor_map = deepcopy(server.client.async_maps.return_value)
    records = [
        entity_id(hass, "image", "map_floor"),
        entity_id(hass, "switch", "map_floor_enabled"),
    ]
    # The mock reads complete without yielding. Both authoritative updates
    # therefore run before HA processes registry removal or new entity tasks.
    server.client.async_maps.return_value = []
    await coordinator.async_refresh_maps("TEST-robot")
    server.client.async_maps.return_value = floor_map
    await coordinator.async_refresh_maps("TEST-robot")
    await hass.async_block_till_done()
    for record in records:
        assert registry.async_get(record) is not None
        assert hass.states.get(record) is not None


@pytest.mark.parametrize("loaded", [GEN4], indirect=True)
async def test_rapid_schedule_delete_and_return_keeps_entity(hass, loaded):  # noqa: F811
    entry, _server, _generation = loaded
    coordinator = entry.runtime_data
    robot = coordinator.robots["TEST-robot"]
    event = {"id": "weekly", "enabled": True, "command": {}}
    robot.schedule = {"events": [event]}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    record = entity_id(hass, "switch", "schedule_event_weekly_enabled")
    robot.schedule = {"events": []}
    coordinator.async_update_listeners()
    robot.schedule = {"events": [event]}
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(record) is not None
    assert hass.states.get(record).state == "on"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_map_cleanup_preserves_other_robot_with_overlapping_prefix(
    hass,
    loaded,  # noqa: F811
):
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    other_serial = "TEST-robot_map_other"
    server.add_robot(other_serial, GEN3)
    maps = {
        serial: [{"id": "floor", "name": "Floor", "enabled": True}]
        for serial in ("TEST-robot", other_serial)
    }
    server.client.async_maps.side_effect = lambda serial: deepcopy(maps[serial])
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    records = [
        entity_id(hass, "image", "map_floor", serial=other_serial),
        entity_id(hass, "switch", "map_floor_enabled", serial=other_serial),
    ]
    maps["TEST-robot"] = []
    await coordinator.async_refresh_maps("TEST-robot")
    await hass.async_block_till_done()
    for record in records:
        assert er.async_get(hass).async_get(record) is not None
        assert hass.states.get(record) is not None
