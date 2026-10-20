"""Pause before docking with confirmation, cancellation and command isolation."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import VacuulaClient, VacuulaError
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData


@pytest.fixture(params=[GEN3, GEN4])
def setup_robot(hass, request):
    generation = request.param
    gen3 = generation == GEN3
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock(spec=VacuulaClient)
    coordinator = VacuulaCoordinator(hass, entry, client)
    names = {
        "pause": "pauseCleaning" if gen3 else "cleaning.pause",
        "home": "sendToBase" if gen3 else "navigation.return_to_base",
        "state": "getRobotState" if gen3 else "state.show",
        "other": "findMe" if gen3 else "utilities.find_me",
        "key": "cmd" if gen3 else "ability",
        "commands": "availableCommands" if gen3 else "available_commands",
        "home_flag": "goToBase" if gen3 else "return_to_base",
    }
    robot = RobotData(
        "robot",
        "Neato",
        generation,
        connected=True,
        state={
            "state": 2 if gen3 else "busy",
            "action": 1 if gen3 else "cleaning",
            names["commands"]: {"pause": True, names["home_flag"]: False},
        },
    )
    coordinator.robots["robot"] = robot

    async def command(serial, payload):
        assert serial == "robot"
        name = payload[names["key"]]
        if name == names["state"]:
            state = deepcopy(robot.state)
            state[names["commands"]].update({"pause": False, names["home_flag"]: True})
            return {"data": state} if gen3 else {"code": 200, "body": state}
        return {"result": "ok"} if gen3 else {"code": 200, "body": {}}

    client.async_command.side_effect = command
    return coordinator, robot, client, names


def command_call(names, action):
    return call("robot", {names["key"]: names[action]})


async def test_active_cleaning_pauses_waits_confirms_then_docks(setup_robot):
    coordinator, _robot, client, names = setup_robot
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        await coordinator.async_return_to_base("robot")
    sleep.assert_awaited_once_with(3.5)
    assert client.async_command.await_args_list == [
        command_call(names, action) for action in ("pause", "state", "home", "state")
    ]
    client.async_maps.assert_not_awaited()
    client.async_runs.assert_not_awaited()


async def test_direct_docking_does_not_pause_or_wait(setup_robot):
    coordinator, robot, client, names = setup_robot
    robot.state[names["commands"]].update({"pause": False, names["home_flag"]: True})
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        await coordinator.async_return_to_base("robot")
    sleep.assert_not_awaited()
    assert client.async_command.await_args_list == [
        command_call(names, action) for action in ("home", "state")
    ]


async def test_refused_docking_does_not_send_commands(setup_robot):
    coordinator, robot, client, names = setup_robot
    robot.state[names["commands"]]["pause"] = False
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_return_to_base("robot")
    assert error.value.translation_key == "action_unavailable"
    client.async_command.assert_not_awaited()


async def test_pause_failure_never_sends_home(setup_robot):
    coordinator, _robot, client, names = setup_robot
    client.async_command.side_effect = VacuulaError("pause failed")
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        with pytest.raises(HomeAssistantError) as error:
            await coordinator.async_return_to_base("robot")
    assert error.value.translation_key == "command_failed"
    sleep.assert_not_awaited()
    assert client.async_command.await_args_list == [command_call(names, "pause")]


async def test_fresh_state_still_refuses_docking(setup_robot):
    coordinator, robot, client, names = setup_robot
    state = deepcopy(robot.state)
    state[names["commands"]]["pause"] = False
    client.async_command.side_effect = [{}, {"data": state}]
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(ServiceValidationError) as error:
            await coordinator.async_return_to_base("robot")
    assert error.value.translation_key == "action_unavailable"
    assert client.async_command.await_args_list == [
        command_call(names, action) for action in ("pause", "state")
    ]


async def test_failed_state_confirmation_never_sends_home(setup_robot):
    coordinator, robot, client, names = setup_robot
    client.async_command.side_effect = [{}, VacuulaError("state failed")]
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(HomeAssistantError) as error:
            await coordinator.async_return_to_base("robot")
    assert error.value.translation_key == "command_failed"
    # The failed read does not mark the robot offline; Vacuula's list decides.
    assert robot.connected
    assert client.async_command.await_args_list == [
        command_call(names, action) for action in ("pause", "state")
    ]


async def test_home_failure_is_reported_without_retry(setup_robot):
    coordinator, _robot, client, names = setup_robot
    original = client.async_command.side_effect

    async def command(serial, payload):
        if payload[names["key"]] == names["home"]:
            raise VacuulaError("home failed")
        return await original(serial, payload)

    client.async_command.side_effect = command
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(HomeAssistantError) as error:
            await coordinator.async_return_to_base("robot")
    assert error.value.translation_key == "command_failed"
    assert client.async_command.await_args_list == [
        command_call(names, action) for action in ("pause", "state", "home")
    ]


async def test_cancel_during_wait_never_sends_home(setup_robot):
    coordinator, _robot, client, names = setup_robot
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def sleep(delay):
        assert delay == 3.5
        waiting.set()
        await release.wait()

    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", side_effect=sleep
    ):
        task = asyncio.create_task(coordinator.async_return_to_base("robot"))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert client.async_command.await_args_list == [command_call(names, "pause")]
    assert not coordinator._lock("robot").locked()


async def test_pause_delay_and_home_are_serialized_against_other_commands(setup_robot):
    coordinator, _robot, client, names = setup_robot
    waiting = asyncio.Event()
    release = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay):
        assert delay == 3.5
        waiting.set()
        await release.wait()

    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", side_effect=sleep
    ):
        home = asyncio.create_task(coordinator.async_return_to_base("robot"))
        await waiting.wait()
        other = asyncio.create_task(
            coordinator.async_command(
                "robot", {names["key"]: names["other"]}, refresh=False
            )
        )
        await real_sleep(0)
        assert client.async_command.await_args_list == [command_call(names, "pause")]
        assert not other.done()
        release.set()
        await asyncio.gather(home, other)
    assert client.async_command.await_args_list == [
        command_call(names, action)
        for action in ("pause", "state", "home", "state", "other")
    ]


@pytest.mark.parametrize("serial", ["missing", "robot"])
async def test_unknown_or_offline_robot_does_not_send_commands(setup_robot, serial):
    coordinator, robot, client, _names = setup_robot
    robot.connected = False
    with pytest.raises(HomeAssistantError) as error:
        await coordinator.async_return_to_base(serial)
    assert error.value.translation_key == "offline"
    client.async_command.assert_not_awaited()
