"""Retain real home activity when a task-changing request is not accepted."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from test_integration import entity_id
from test_integration import loaded as integration_loaded

from custom_components.vacuula.client import CannotConnect, CommandError
from custom_components.vacuula.const import GEN3
from custom_components.vacuula.data import begin_return_trip, track_return_trip
from custom_components.vacuula.vacuum import VacuulaVacuum

SERIAL = "TEST-robot"
loaded = integration_loaded
TASK_CHANGES = [
    ("stopCleaning", "moving"),
    ("startCleaning", "moving"),
    ("startPersistentMapExploration", "moving"),
    ("resumeCleaning", "pending"),
]


def home_context(entry, server, phase):
    """Provide a known home trip or a pending departure with matching live state."""
    robot = entry.runtime_data.robots[SERIAL]
    live = server.state[SERIAL]
    live.update(state=2, action=9)
    live["details"]["isDocked"] = False
    live["availableCommands"].update(
        start=True, stop=True, pause=True, resume=False, goToBase=False
    )
    robot.state = deepcopy(live)
    begin_return_trip(robot)
    if phase == "moving":
        track_return_trip(robot)
    else:
        live.update(state=3, action=1)
        live["availableCommands"].update(start=False, pause=False, resume=True)
        robot.state = deepcopy(live)
    return robot, live


def push_state(coordinator, state):
    """Deliver a Gen3 event exactly as the server wraps partial state updates."""
    coordinator._handle_event(SERIAL, {"data": {"type": "state", "data": state}})


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize(("native_command", "phase"), TASK_CHANGES)
@pytest.mark.parametrize(
    "failure_type", [CommandError, CannotConnect, asyncio.CancelledError]
)
async def test_failed_task_change_keeps_home_events_out_of_cleaning(
    hass, loaded, native_command, phase, failure_type
):
    """A rejected or interrupted task change cannot relabel unchanged home motion."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    robot, live = home_context(entry, server, phase)
    vacuum = VacuulaVacuum(coordinator, SERIAL)
    vacuum_id = entity_id(hass, "vacuum", "vacuum")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    seen = []

    @callback
    def record(event):
        """Observe every vacuum state published while the command is in flight."""
        if event.data["entity_id"] == vacuum_id and event.data["new_state"]:
            seen.append(event.data["new_state"].state)

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Keep the robot navigating home and then reject or interrupt the request."""
        if payload.get("cmd") == native_command:
            live.update(state=2, action=9)
            push_state(coordinator, {"state": 2, "action": 9})
            assert vacuum.activity == "returning"
            raise failure_type("Task was not confirmed")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    expected_error = (
        asyncio.CancelledError
        if failure_type is asyncio.CancelledError
        else HomeAssistantError
    )
    try:
        with pytest.raises(expected_error):
            if native_command == "stopCleaning":
                await vacuum.async_stop()
            else:
                await coordinator.async_command(SERIAL, {"cmd": native_command})
        await hass.async_block_till_done()
    finally:
        unsubscribe()
    assert robot.returning
    assert vacuum.activity == "returning"
    assert hass.states.get(vacuum_id).state == "returning"
    assert "cleaning" not in seen
    assert (
        sum(
            call.args[1].get("cmd") == native_command
            for call in server.client.async_command.await_args_list
        )
        == 1
    )


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize(("native_command", "phase"), TASK_CHANGES)
async def test_rejection_never_restores_context_after_a_terminal_event(
    hass, loaded, native_command, phase
):
    """Preserve newer docking completion when an older command reports rejection."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    robot, live = home_context(entry, server, phase)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Publish independent docking completion before the request fails."""
        if payload.get("cmd") == native_command:
            live.update(state=1, action=0)
            live["details"]["isDocked"] = True
            push_state(
                coordinator,
                {"state": 1, "action": 0, "details": {"isDocked": True}},
            )
            raise CommandError("Task rejected")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(SERIAL, {"cmd": native_command})
    assert robot.return_phase is None
    assert not robot.returning
    assert VacuulaVacuum(coordinator, SERIAL).activity == "docked"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize(
    ("native_command", "phase"),
    [task for task in TASK_CHANGES if task[0] != "stopCleaning"],
)
async def test_accepted_new_task_clears_home_context_before_readback(
    hass, loaded, native_command, phase
):
    """Use cleaning activity after an acknowledged task replaces the home request."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    robot, live = home_context(entry, server, phase)
    original = server.client.async_command.side_effect
    accepted = False
    checked_readback = False

    async def command(serial, payload):
        """Acknowledge the new task and inspect context before its confirming read."""
        nonlocal accepted, checked_readback
        if payload.get("cmd") == native_command:
            live.update(state=2, action=9)
            push_state(coordinator, {"state": 2, "action": 9})
            accepted = True
            return {"result": "ok"}
        if payload.get("cmd") == "getRobotState" and accepted:
            assert robot.return_phase is None
            checked_readback = True
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    await coordinator.async_command(SERIAL, {"cmd": native_command})
    assert checked_readback
    assert robot.return_phase is None
    assert VacuulaVacuum(coordinator, SERIAL).activity == "cleaning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize("read_fails", [False, True])
async def test_stop_acknowledgement_waits_for_terminal_telemetry(
    hass, loaded, read_fails
):
    """Keep observed motion until Stop is confirmed by terminal telemetry."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    robot, _live = home_context(entry, server, "moving")
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Acknowledge cancellation before the robot publishes its terminal state."""
        if payload.get("cmd") == "stopCleaning":
            return {"result": "ok"}
        if read_fails and payload.get("cmd") == "getRobotState":
            raise CannotConnect("State reply lost")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    await VacuulaVacuum(coordinator, SERIAL).async_stop()
    assert robot.returning
    push_state(coordinator, {"state": 1, "action": 0})
    assert robot.return_phase is None
    assert VacuulaVacuum(coordinator, SERIAL).activity == "idle"
