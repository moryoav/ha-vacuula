"""Select advanced cleaning targets from fresh telemetry under the robot lock."""

# ruff: noqa: F811

from copy import deepcopy

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_schedule_merge import schedule_setup  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("actual_docked", [False, True])
async def test_auto_clean_uses_fresh_dock_state(
    hass,
    schedule_setup,
    generation,
    actual_docked,  # noqa: F811
):
    """A stale off-dock cache cannot discard an active map and its no-go regions."""
    coordinator, robot, device_id, _, _ = schedule_setup
    robot.generation = generation
    dock_key = "isDocked" if generation == GEN3 else "is_docked"
    robot.state = {"details": {dock_key: not actual_docked}}
    coordinator.client.async_maps.return_value = [
        {"id": "map1", "enabled": True, "promoted": True}
    ]
    commands = []

    async def command(serial, payload):
        """Expose current docking for reads and record the chosen command."""
        commands.append(deepcopy(payload))
        state = {
            "state": 1 if generation == GEN3 else "idle",
            "details": {dock_key: actual_docked},
        }
        return (
            {"result": "ok", "data": state}
            if generation == GEN3
            else {"code": 200, "body": state}
        )

    coordinator.client.async_command.side_effect = command
    try:
        await hass.services.async_call(
            DOMAIN, "clean", {"device_id": device_id}, blocking=True
        )
    finally:
        await coordinator.async_stop()
    state_command = (
        {"cmd": "getRobotState"} if generation == GEN3 else {"ability": "state.show"}
    )
    assert commands[0] == state_command
    if generation == GEN3:
        assert commands[1]["cmd"] == "startCleaning"
        assert commands[1]["params"]["category"] == (4 if actual_docked else 2)
    else:
        expected = (
            {"rank_id": "map1", "track_id": None, "nogo_enabled": True}
            if actual_docked
            else None
        )
        assert commands[1]["ability"] == "cleaning.start"
        assert commands[1]["runs"][0]["map"] == expected


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_unreadable_state_cannot_start_advanced_cleaning(
    hass,
    schedule_setup,
    generation,  # noqa: F811
):
    """Refuse stale target selection when the fresh docking read fails."""
    coordinator, robot, device_id, _, _ = schedule_setup
    robot.generation = generation
    coordinator.client.async_command.side_effect = CannotConnect("State unavailable")
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            DOMAIN, "clean", {"device_id": device_id}, blocking=True
        )
    assert error.value.translation_key == "command_failed"
    coordinator.client.async_command.assert_awaited_once_with(
        robot.serial,
        {"cmd": "getRobotState"} if generation == GEN3 else {"ability": "state.show"},
    )
    coordinator.client.async_maps.assert_not_awaited()
