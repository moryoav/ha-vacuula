"""Check controls against live readiness through Home Assistant's entity services."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from test_integration import entity_id, loaded  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import GEN3
from custom_components.vacuula.vacuum import VacuulaVacuum

SERIAL = "TEST-robot"


def command_names(generation):
    """Return the native flag dictionary and protocol key for one generation."""
    return (
        ("availableCommands", "cmd")
        if generation == GEN3
        else ("available_commands", "ability")
    )


@pytest.mark.parametrize(
    "action", ["start", "pause", "stop", "resume", "return_to_base"]
)
async def test_button_service_rechecks_stale_readiness(hass, loaded, action):  # noqa: F811
    """Let an enabled button reach live validation despite a stale false flag."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    robot = coordinator.robots[SERIAL]
    flags_key, command_key = command_names(generation)
    flag, expected = {
        "start": ("start", "startCleaning" if generation == GEN3 else "cleaning.start"),
        "pause": ("pause", "pauseCleaning" if generation == GEN3 else "cleaning.pause"),
        "stop": (
            "stop" if generation == GEN3 else "cancel",
            "stopCleaning" if generation == GEN3 else "cleaning.cancel",
        ),
        "resume": (
            "resume",
            "resumeCleaning" if generation == GEN3 else "cleaning.resume",
        ),
        "return_to_base": (
            "goToBase" if generation == GEN3 else "return_to_base",
            "sendToBase" if generation == GEN3 else "navigation.return_to_base",
        ),
    }[action]
    live = server.state[SERIAL]
    live[flags_key][flag] = True
    live[flags_key]["pause"] = action == "pause"
    robot.state = deepcopy(live)
    robot.state[flags_key][flag] = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    button = entity_id(hass, "button", f"button_{action}")
    assert hass.states.get(button).state != "unavailable"
    server.client.async_command.reset_mock()
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    sent = [call.args[1] for call in server.client.async_command.await_args_list]
    assert sent[0] == {
        command_key: "getRobotState" if generation == GEN3 else "state.show"
    }
    assert sum(payload.get(command_key) == expected for payload in sent) == 1


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize("action", ["clean_spot", "explore"])
async def test_gen3_special_button_rechecks_stale_start(hass, loaded, action):  # noqa: F811
    """Recheck spot and exploration after the robot silently enables Start."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    coordinator.robots[SERIAL].state["availableCommands"]["start"] = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    button = entity_id(hass, "button", f"button_{action}")
    assert hass.states.get(button).state != "unavailable"
    server.client.async_command.reset_mock()
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    sent = [call.args[1] for call in server.client.async_command.await_args_list]
    expected = (
        "startCleaning" if action == "clean_spot" else "startPersistentMapExploration"
    )
    assert sent[0] == {"cmd": "getRobotState"}
    assert sum(payload.get("cmd") == expected for payload in sent) == 1


async def test_start_chooses_fresh_resume_without_maps_or_recovery_guard(hass, loaded):  # noqa: F811
    """Resume the current run when stale cached state still suggests a new start."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    flags_key, command_key = command_names(generation)
    live = server.state[SERIAL]
    live[flags_key].update(start=False, resume=True)
    live["state"] = 3 if generation == GEN3 else "paused"
    coordinator._set_pending_promotion(SERIAL, "floor")
    server.client.async_maps.side_effect = CannotConnect("maps unavailable")
    server.client.async_maps.reset_mock()
    server.client.async_command.reset_mock()
    await hass.services.async_call(
        "vacuum",
        "start",
        {"entity_id": entity_id(hass, "vacuum", "vacuum")},
        blocking=True,
    )
    sent = [call.args[1] for call in server.client.async_command.await_args_list]
    expected = "resumeCleaning" if generation == GEN3 else "cleaning.resume"
    assert sum(payload.get(command_key) == expected for payload in sent) == 1
    server.client.async_maps.assert_not_awaited()
    assert coordinator.has_pending_map_recovery(SERIAL)


async def test_start_chooses_fresh_new_run_instead_of_stale_resume(hass, loaded):  # noqa: F811
    """Read current maps when a previously paused run ended before Start executes."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    flags_key, command_key = command_names(generation)
    coordinator.robots[SERIAL].state[flags_key].update(start=False, resume=True)
    server.client.async_command.reset_mock()
    server.client.async_maps.reset_mock()
    await hass.services.async_call(
        "vacuum",
        "start",
        {"entity_id": entity_id(hass, "vacuum", "vacuum")},
        blocking=True,
    )
    sent = [call.args[1] for call in server.client.async_command.await_args_list]
    start = next(
        payload
        for payload in sent
        if payload.get(command_key) in {"startCleaning", "cleaning.start"}
    )
    if generation == GEN3:
        assert start["params"]["category"] == 4
    else:
        assert start["runs"][0]["map"] == {
            "rank_id": "floor",
            "track_id": None,
            "nogo_enabled": True,
        }
    server.client.async_maps.assert_awaited_once_with(SERIAL)


async def test_failed_start_state_read_sends_no_movement_command(hass, loaded):  # noqa: F811
    """Fail safely when Start cannot confirm whether to resume or start a new run."""
    entry, server, _generation = loaded
    server.client.async_command.side_effect = CannotConnect("robot timeout")
    server.client.async_command.reset_mock()
    server.client.async_maps.reset_mock()
    with pytest.raises(HomeAssistantError) as error:
        await VacuulaVacuum(entry.runtime_data, SERIAL).async_start()
    assert error.value.translation_key == "command_failed"
    assert server.client.async_command.await_count == 1
    server.client.async_maps.assert_not_awaited()


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize("state", [1, 4])
async def test_terminal_home_readback_keeps_return_trip_cleared(hass, loaded, state):  # noqa: F811
    """Do not resurrect a completed or failed trip after its authoritative readback."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    robot = coordinator.robots[SERIAL]
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Finish docking before the successful home request's state readback."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=state, action=0)
            server.state[serial]["details"]["isDocked"] = state == 1
        return result

    server.client.async_command.side_effect = command
    await coordinator.async_return_to_base(SERIAL)
    assert not robot.returning
    coordinator._handle_event(
        SERIAL,
        {
            "data": {
                "type": "state",
                "data": {
                    "state": 2,
                    "action": 9,
                    "cleaning": {"category": 2},
                    "details": {"isDocked": False},
                },
            }
        },
    )
    assert VacuulaVacuum(coordinator, SERIAL).activity == "cleaning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_delayed_home_motion_after_paused_readback_is_returning(hass, loaded):  # noqa: F811
    """Retain home intent while a readback briefly still reports the paused run."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    await VacuulaVacuum(coordinator, SERIAL).async_start()
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Delay the robot's movement transition beyond acknowledgement and readback."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=3, action=1)
        return result

    server.client.async_command.side_effect = command
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        await coordinator.async_return_to_base(SERIAL)
    coordinator._handle_event(
        SERIAL, {"data": {"type": "state", "data": {"state": 2, "action": 9}}}
    )
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_home_button_still_refuses_confirmed_missing_base(hass, loaded):  # noqa: F811
    """Keep a callable Home button from bypassing the D5's fresh false home flag."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    live = server.state[SERIAL]
    live.update(state=3, action=1)
    live["details"].update(isDocked=False, dockHasBeenSeen=False)
    live["availableCommands"].update(pause=False, goToBase=False, resume=True)
    coordinator.robots[SERIAL].state = deepcopy(live)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    server.client.async_command.reset_mock()
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, "button", "button_return_to_base")},
            blocking=True,
        )
    assert error.value.translation_key == "base_not_found"
    assert [call.args[1] for call in server.client.async_command.await_args_list] == [
        {"cmd": "getRobotState"}
    ]


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_exploration_refuses_fresh_off_dock_state(hass, loaded):  # noqa: F811
    """Require a docked robot when the callable exploration button validates it."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    live = server.state[SERIAL]
    live["details"]["isDocked"] = False
    coordinator.robots[SERIAL].state = deepcopy(live)
    server.client.async_command.reset_mock()
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, "button", "button_explore")},
            blocking=True,
        )
    assert error.value.translation_key == "action_unavailable"
    assert [call.args[1] for call in server.client.async_command.await_args_list] == [
        {"cmd": "getRobotState"}
    ]
