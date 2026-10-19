"""Ensure advanced actions use verified generation-specific protocol payloads."""

from unittest.mock import AsyncMock, MagicMock

import probatio as vol
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.services import _resolve, async_setup_services


@pytest.fixture
def setup(hass):
    coordinator = MagicMock()
    coordinator.async_command = AsyncMock(
        return_value={"result": "ok", "data": {"value": 1}}
    )
    coordinator.async_request_refresh = AsyncMock()
    coordinator.async_stop = AsyncMock()
    coordinator.async_refresh_robot = AsyncMock()
    coordinator.async_refresh_maps = AsyncMock()
    coordinator.async_set_map_boundaries = AsyncMock(return_value={})
    coordinator.async_update_map = AsyncMock(return_value={})
    coordinator.client.async_maps = AsyncMock(return_value=[])
    coordinator.client.async_runs = AsyncMock(return_value=[])
    coordinator.client.async_map = AsyncMock(return_value={"width": 1})
    coordinator.client.async_run = AsyncMock(return_value={"height": 1})
    coordinator.client.async_map_boundaries = AsyncMock(return_value=[])
    coordinator.client.async_update_robot = AsyncMock(return_value={})
    coordinator.client.async_update_map = AsyncMock(return_value={})
    coordinator.client.async_map_action = AsyncMock(return_value={})
    coordinator.async_map_operation = coordinator.client.async_map_action
    robot = RobotData("TEST-robot", "Neato", GEN3, True)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    entry.mock_state(hass, ConfigEntryState.LOADED)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, robot.serial)},
    )
    coordinator.robots = {robot.serial: robot}
    async_setup_services(hass)
    return coordinator, robot, device.id, entry


async def call(hass, setup, service_name, **kwargs):
    return await hass.services.async_call(
        DOMAIN,
        service_name,
        {"device_id": setup[2], **kwargs},
        blocking=True,
        return_response=True,
    )


async def test_targets(hass, setup):
    coordinator, robot, device_id, entry = setup
    assert _resolve(hass, device_id) == (coordinator, robot)
    with pytest.raises(ServiceValidationError):
        _resolve(hass, "missing")
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    with pytest.raises(ServiceValidationError):
        _resolve(hass, device_id)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    coordinator.robots = {}
    with pytest.raises(ServiceValidationError):
        _resolve(hass, device_id)


async def test_clean_gen3(hass, setup):
    coordinator = setup[0]
    await call(hass, setup, "clean")
    assert coordinator.async_command.await_args.args[1] == {
        "cmd": "startCleaning",
        "params": {"category": 2, "mode": 1, "navigationMode": 1},
    }
    await call(
        hass,
        setup,
        "clean",
        map_id="map1",
        zone_ids=["zone1"],
        mode="turbo",
        navigation="deep",
    )
    assert coordinator.async_command.await_args.args[1]["params"] == {
        "category": 4,
        "mode": 2,
        "navigationMode": 3,
        "mapId": "map1",
        "boundaryId": "zone1",
    }
    for payload in [
        {"zone_ids": ["zone1"]},
        {"map_id": "map1", "zone_ids": ["a", "b"]},
        {"mode": "max"},
        {"navigation": "gentle"},
    ]:
        with pytest.raises(ServiceValidationError):
            await call(hass, setup, "clean", **payload)


async def test_clean_gen4(hass, setup):
    coordinator, robot, _, _ = setup
    robot.generation = GEN4
    await call(
        hass, setup, "clean", map_id="map1", zone_ids=["a", "b"], navigation="gentle"
    )
    command = coordinator.async_command.await_args.args[1]
    assert command["ability"] == "cleaning.start"
    assert [run["map"]["track_id"] for run in command["runs"]] == ["a", "b"]
    for payload in [{"navigation": "deep"}, {"mode": "max"}]:
        with pytest.raises(ServiceValidationError):
            await call(hass, setup, "clean", **payload)
    robot.info["label_serial"] = "AAAA N2"
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "clean", mode="max")
    robot.info["label_serial"] = "AAAAN2TEST"
    await call(hass, setup, "clean", mode="max")
    assert coordinator.async_command.await_args.args[1]["runs"][0]["map"] is None


async def test_schedule(hass, setup):
    coordinator, robot, _, _ = setup
    events = [{"mode": 1, "day": 0, "startTime": "10:30"}]
    await call(hass, setup, "set_schedule", events=events, time_basis="utc")
    assert coordinator.async_command.await_args.args[1] == {
        "cmd": "setSchedule",
        "params": {"events": events},
    }
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "delete_schedule_event", event_id="event")
    robot.generation = GEN4
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "set_schedule", events=[])
    coordinator.async_update_schedule_event = AsyncMock(return_value={})
    await call(
        hass,
        setup,
        "update_schedule_event",
        enabled=True,
        days=["mon"],
        time="13:00",
        event_id="old",
    )
    assert coordinator.async_update_schedule_event.await_args.args[1] == "old"
    await call(
        hass, setup, "update_schedule_event", enabled=False, days=["sun"], time="00:00"
    )
    assert "id" not in coordinator.async_command.await_args.args[1]["event"]
    await call(hass, setup, "delete_schedule_event", event_id="old")
    assert coordinator.async_command.await_args.args[1] == {
        "ability": "scheduling.delete",
        "id": "old",
    }


async def test_boundaries(hass, setup):
    coordinator, robot, _, _ = setup
    await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[])
    assert coordinator.async_command.await_args.args[1]["params"]["boundaries"] == []
    polygon = {
        "id": "a",
        "name": "Test",
        "color": "#ffffff",
        "type": "polygon",
        "vertices": [[0, 0], [1, 1]],
        "enabled": True,
    }
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    polygon["vertices"].append([0, 1])
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    polygon["relevancy"] = [0.5, 0.5]
    await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    robot.generation = GEN4
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    polygon["function"] = "cleaning"
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    del polygon["relevancy"]
    await call(hass, setup, "set_boundaries", map_id="map1", boundaries=[polygon])
    assert coordinator.async_set_map_boundaries.await_args.args == (
        robot.serial,
        "map1",
        [polygon],
    )


async def test_rest_actions(hass, setup):
    coordinator, robot, _, _ = setup
    await call(hass, setup, "rename_robot", name="New name")
    assert robot.name == "New name"
    await call(hass, setup, "update_map", map_id="map1", name="Floor", enabled=True)
    coordinator.client.async_map.assert_awaited_with(robot.serial, "map1")
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "map_action", map_id="map1", action="promote")
    robot.generation = GEN4
    for action in ("promote", "use", "unuse", "demote"):
        await call(hass, setup, "map_action", map_id="map1", action=action)
        coordinator.client.async_map_action.assert_awaited_with(
            robot.serial, "map1", action, None
        )
    coordinator.client.async_map.side_effect = CannotConnect("offline")
    with pytest.raises(HomeAssistantError):
        await call(hass, setup, "update_map", map_id="map1", name="Floor", enabled=True)


async def test_factory_reset(hass, setup):
    coordinator, robot, _, _ = setup
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "factory_reset", confirm=True)
    robot.generation = GEN4
    with pytest.raises(ServiceValidationError):
        await call(hass, setup, "factory_reset", confirm=False)
    coordinator.async_command.assert_not_awaited()
    await call(hass, setup, "factory_reset", confirm=True)
    coordinator.async_command.assert_awaited_once_with(
        robot.serial, {"ability": "settings.factory_reset"}
    )


@pytest.mark.parametrize(
    "resource",
    [
        "state",
        "info",
        "stats",
        "preferences",
        "schedule",
        "maps",
        "runs",
        "map",
        "run",
        "boundaries",
        "log_upload_state",
    ],
)
async def test_get_data(hass, setup, resource):
    result = await call(
        hass, setup, "get_data", resource=resource, map_id="map1", run_id="run1"
    )
    assert "data" in result


async def test_get_gen4(hass, setup):
    setup[1].generation = GEN4
    for resource in ("state", "info", "stats", "preferences", "schedule", "boundaries"):
        await call(hass, setup, "get_data", resource=resource, map_id="map1")
    for resource in ("log_upload_state",):
        with pytest.raises(ServiceValidationError):
            await call(hass, setup, "get_data", resource=resource)
    for resource in ("map", "boundaries", "run"):
        with pytest.raises(ServiceValidationError):
            await call(hass, setup, "get_data", resource=resource)


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("set_schedule", {"events": [{"mode": 1, "day": 9, "startTime": "10:00"}]}),
        ("set_schedule", {"events": [{"mode": 1, "day": 0, "startTime": "25:00"}]}),
        (
            "set_schedule",
            {"events": [{"mode": 1, "day": 0, "startTime": "10:00", "extra": 1}]},
        ),
        ("update_map", {"map_id": "../other", "name": "bad", "enabled": True}),
        ("clean", {"mode": "unknown"}),
    ],
)
async def test_schema_rejects_bad_input(hass, setup, action, payload):
    with pytest.raises(vol.Invalid):
        await call(hass, setup, action, **payload)
    setup[0].async_command.assert_not_awaited()
