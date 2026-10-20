"""Require current docking for immediate explicit maps without restricting schedules."""

# ruff: noqa: F811

from copy import deepcopy

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_schedule_merge import schedule_setup  # noqa: F401

from custom_components.vacuula.const import DOMAIN, GEN3, GEN4


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("docked", [False, None, 0, "true"])
async def test_immediate_explicit_map_requires_fresh_confirmed_docking(
    hass, schedule_setup, generation, docked
):
    """Reject off-dock or unknown state before sending an explicit mapped run."""
    coordinator, robot, device_id, _, _ = schedule_setup
    robot.generation = generation
    dock_key = "isDocked" if generation == GEN3 else "is_docked"
    robot.state = {"details": {dock_key: True}}
    coordinator.client.async_maps.return_value = [
        {"id": "map1", "enabled": True, "promoted": True}
    ]
    commands = []

    async def command(serial, payload):
        """Return a fresh refusal state while recording every robot request."""
        commands.append(deepcopy(payload))
        state = {"details": {dock_key: docked}}
        return (
            {"result": "ok", "data": state}
            if generation == GEN3
            else {"code": 200, "body": state}
        )

    coordinator.client.async_command.side_effect = command
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            "clean",
            {"device_id": device_id, "map_id": "map1", "zone_ids": ["zone1"]},
            blocking=True,
        )
    assert error.value.translation_key == (
        "map_cleaning_requires_dock" if docked is False else "dock_status_unknown"
    )
    assert commands == [
        {"cmd": "getRobotState"} if generation == GEN3 else {"ability": "state.show"}
    ]


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_explicit_map_uses_fresh_docked_transition(
    hass, schedule_setup, generation
):
    """Allow a robot that has docked since the cached state without losing its zone."""
    coordinator, robot, device_id, _, _ = schedule_setup
    robot.generation = generation
    dock_key = "isDocked" if generation == GEN3 else "is_docked"
    robot.state = {"details": {dock_key: False}}
    coordinator.client.async_maps.return_value = [{"id": "map1"}]
    commands = []

    async def command(serial, payload):
        """Expose docking for the guarded start and acknowledge the explicit target."""
        commands.append(deepcopy(payload))
        state = {"details": {dock_key: True}}
        return (
            {"result": "ok", "data": state}
            if generation == GEN3
            else {"code": 200, "body": state}
        )

    coordinator.client.async_command.side_effect = command
    try:
        await hass.services.async_call(
            DOMAIN,
            "clean",
            {"device_id": device_id, "map_id": "map1", "zone_ids": ["zone1"]},
            blocking=True,
        )
    finally:
        await coordinator.async_stop()
    if generation == GEN3:
        assert commands[1]["params"]["mapId"] == "map1"
        assert commands[1]["params"]["boundaryId"] == "zone1"
    else:
        assert commands[1]["runs"][0]["map"] == {
            "rank_id": "map1",
            "track_id": "zone1",
            "nogo_enabled": True,
        }


@pytest.mark.parametrize("docked", [False, None])
async def test_future_explicit_map_event_does_not_require_current_docking(
    hass, schedule_setup, docked
):
    """Store future map and zone targets while the robot is off dock or unknown."""
    coordinator, robot, device_id, _server, writes = schedule_setup
    robot.state = {"details": {"is_docked": docked}}
    await hass.services.async_call(
        DOMAIN,
        "update_schedule_event",
        {
            "device_id": device_id,
            "enabled": True,
            "days": ["mon"],
            "time": "09:00",
            "map_id": "map1",
            "zone_ids": ["zone1"],
        },
        blocking=True,
    )
    assert writes[0]["command"]["runs"][0]["map"] == {
        "rank_id": "map1",
        "track_id": "zone1",
        "nogo_enabled": True,
    }
