"""Controls must select their final target after queued map operations finish."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.button import VacuulaButton
from custom_components.vacuula.client import VacuulaClient
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import VacuulaVacuum


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock(spec=VacuulaClient)
    client.async_command.return_value = {"code": 200, "body": {}}
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots["robot"] = RobotData(
        "robot",
        "Neato",
        GEN4,
        connected=True,
        state={
            "state": "idle",
            "details": {"is_docked": True},
            "available_commands": {"start": True, "resume": False},
        },
    )
    return coordinator


async def test_start_never_uses_a_target_from_before_a_queued_promotion(coordinator):
    """A completed promotion must not be followed by a stale mapless start."""
    client = coordinator.client
    floor_map = {"id": "floor", "promoted": False}
    first_read = asyncio.Event()
    release_read = asyncio.Event()
    reads = 0
    starts = []

    async def maps(serial):
        nonlocal reads
        reads += 1
        if reads == 1:
            first_read.set()
            await release_read.wait()
        return [deepcopy(floor_map)]

    async def promote(serial, map_id, action, payload):
        assert action == "promote"
        floor_map["promoted"] = True
        return {}

    async def command(serial, payload):
        if payload.get("ability") == "cleaning.start":
            starts.append((floor_map["promoted"], deepcopy(payload)))
        return {"code": 200, "body": coordinator.robots["robot"].state}

    client.async_maps.side_effect = maps
    client.async_map_action.side_effect = promote
    client.async_command = AsyncMock(side_effect=command)
    start = asyncio.create_task(VacuulaVacuum(coordinator, "robot").async_start())
    await first_read.wait()
    promotion = asyncio.create_task(
        coordinator.async_map_operation("robot", "floor", "promote")
    )
    await asyncio.sleep(0)
    release_read.set()
    await asyncio.gather(start, promotion)
    assert len(starts) == 1
    promoted_when_sent, command = starts[0]
    assert command["runs"][0]["map"] == (
        {"rank_id": "floor", "track_id": None, "nogo_enabled": True}
        if promoted_when_sent
        else None
    )


async def test_second_queued_start_respects_the_first_start_readback(coordinator):
    """Readiness must be checked when execution begins, after queued commands."""
    client = coordinator.client
    reading = asyncio.Event()
    release = asyncio.Event()
    reads = 0
    starts = []

    async def maps(serial):
        nonlocal reads
        reads += 1
        if reads == 1:
            reading.set()
            await release.wait()
        return []

    async def command(serial, payload):
        if payload.get("ability") == "cleaning.start":
            starts.append(deepcopy(payload))
            coordinator.robots[serial].state = {
                "state": "busy",
                "action": "cleaning",
                "available_commands": {"start": False, "pause": True},
            }
        return {"code": 200, "body": deepcopy(coordinator.robots[serial].state)}

    client.async_maps.side_effect = maps
    client.async_command.side_effect = command
    vacuum = VacuulaVacuum(coordinator, "robot")
    first = asyncio.create_task(vacuum.async_start())
    await reading.wait()
    second = asyncio.create_task(vacuum.async_start())
    await asyncio.sleep(0)
    release.set()
    await first
    with pytest.raises(ServiceValidationError) as error:
        await second
    assert error.value.translation_key == "action_unavailable"
    assert len(starts) == 1


async def test_command_queued_before_disconnect_never_reaches_the_robot(coordinator):
    """A connection check before waiting on a long map upload is stale."""
    builder = MagicMock(return_value={"ability": "utilities.find_me"})
    lock = coordinator._lock("robot")
    await lock.acquire()
    command = asyncio.create_task(coordinator.async_command("robot", builder))
    await asyncio.sleep(0)
    coordinator.robots["robot"].connected = False
    lock.release()
    with pytest.raises(HomeAssistantError) as error:
        await command
    assert error.value.translation_key == "offline"
    builder.assert_not_called()
    coordinator.client.async_command.assert_not_awaited()


async def test_disconnect_during_map_read_does_not_send_a_clean_command(coordinator):
    async def maps(serial):
        coordinator.robots[serial].connected = False
        return []

    coordinator.client.async_maps.side_effect = maps
    with pytest.raises(HomeAssistantError) as error:
        await VacuulaVacuum(coordinator, "robot").async_start()
    assert error.value.translation_key == "offline"
    coordinator.client.async_command.assert_not_awaited()


@pytest.mark.parametrize("action", ["pause", "stop", "resume"])
@pytest.mark.parametrize("platform", ["vacuum", "button"])
async def test_queued_native_controls_recheck_readiness(coordinator, action, platform):
    robot = coordinator.robots["robot"]
    flag = "cancel" if action == "stop" else action
    robot.state["available_commands"][flag] = True
    lock = coordinator._lock("robot")
    await lock.acquire()
    vacuum = VacuulaVacuum(coordinator, "robot")
    action_method = (
        vacuum.async_start if action == "resume" else getattr(vacuum, f"async_{action}")
    )
    task = asyncio.create_task(
        action_method()
        if platform == "vacuum"
        else VacuulaButton(coordinator, "robot", action).async_press()
    )
    await asyncio.sleep(0)
    robot.state["available_commands"][flag] = False
    lock.release()
    with pytest.raises(ServiceValidationError) as error:
        await task
    assert error.value.translation_key == "action_unavailable"
    coordinator.client.async_command.assert_not_awaited()


async def test_queued_return_home_rechecks_connection(coordinator):
    coordinator.robots["robot"].state["available_commands"].update(
        {"pause": False, "return_to_base": True}
    )
    lock = coordinator._lock("robot")
    await lock.acquire()
    task = asyncio.create_task(coordinator.async_return_to_base("robot"))
    await asyncio.sleep(0)
    coordinator.robots["robot"].connected = False
    lock.release()
    with pytest.raises(HomeAssistantError) as error:
        await task
    assert error.value.translation_key == "offline"
    coordinator.client.async_command.assert_not_awaited()


@pytest.mark.parametrize("action", ["spot", "explore"])
async def test_queued_mapping_or_spot_control_rechecks_readiness(coordinator, action):
    robot = coordinator.robots["robot"]
    robot.generation = GEN3
    robot.capabilities = {"spotCleaning": "basic-1", "maps": "advanced-1"}
    robot.state = {
        "state": 1,
        "details": {"isDocked": True},
        "availableCommands": {"start": True},
    }
    lock = coordinator._lock("robot")
    await lock.acquire()
    task = asyncio.create_task(
        VacuulaVacuum(coordinator, "robot").async_clean_spot()
        if action == "spot"
        else VacuulaButton(coordinator, "robot", "explore").async_press()
    )
    await asyncio.sleep(0)
    robot.state["availableCommands"]["start"] = False
    lock.release()
    with pytest.raises(ServiceValidationError) as error:
        await task
    assert error.value.translation_key == "action_unavailable"
    coordinator.client.async_command.assert_not_awaited()
