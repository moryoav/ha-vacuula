"""Adversarial home readiness, acknowledgement and bounded activity tracking."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_integration import loaded as integration_loaded

from custom_components.vacuula import data as robot_data
from custom_components.vacuula.client import (
    CannotConnect,
    CommandError,
    CommandTimedOut,
)
from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.data import (
    RETURN_PENDING_SECONDS,
    RETURN_TRIP_SECONDS,
    RobotData,
    begin_return_trip,
    track_return_trip,
)
from custom_components.vacuula.vacuum import VacuulaVacuum

SERIAL = "TEST-robot"
loaded = integration_loaded


def protocol(generation):
    """Return the generation's native keys and exact home sequence commands."""
    gen3 = generation == GEN3
    return {
        "key": "cmd" if gen3 else "ability",
        "flags": "availableCommands" if gen3 else "available_commands",
        "home_flag": "goToBase" if gen3 else "return_to_base",
        "state": "getRobotState" if gen3 else "state.show",
        "pause": "pauseCleaning" if gen3 else "cleaning.pause",
        "home": "sendToBase" if gen3 else "navigation.return_to_base",
    }


def paused_robot(entry, server, generation):
    """Provide a resumable paused run whose live state explicitly allows home."""
    names = protocol(generation)
    live = server.state[SERIAL]
    live.update(
        state=3 if generation == GEN3 else "paused",
        action=1 if generation == GEN3 else "cleaning",
    )
    live["details"]["isDocked" if generation == GEN3 else "is_docked"] = False
    live[names["flags"]].update(start=False, pause=False, resume=True)
    live[names["flags"]][names["home_flag"]] = True
    entry.runtime_data.robots[SERIAL].state = deepcopy(live)
    return names, live


def gen3_event(**state):
    """Wrap one partial Gen3 state in the app's exact event envelope."""
    return {"data": {"type": "state", "data": state}}


def sent_names(server, names):
    """List commands in dispatch order without hiding state reads."""
    return [
        call.args[1][names["key"]]
        for call in server.client.async_command.await_args_list
    ]


async def test_stale_positive_home_never_authorizes_the_live_false_flag(hass, loaded):
    """Confirm cached home permission before issuing movement to an ended run."""
    entry, server, generation = loaded
    names, live = paused_robot(entry, server, generation)
    live.update(
        state=1 if generation == GEN3 else "idle",
        action=0 if generation == GEN3 else "idle",
    )
    live[names["flags"]].update(start=True, resume=False)
    live[names["flags"]][names["home_flag"]] = False
    server.client.async_command.reset_mock()
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.async_return_to_base(SERIAL)
    assert sent_names(server, names) == [names["state"]]


async def test_stale_positive_pause_uses_the_live_direct_home_path(hass, loaded):
    """Skip a pause that became unavailable after the app already paused the robot."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    coordinator.robots[SERIAL].state[names["flags"]].update(pause=True, resume=False)
    coordinator.robots[SERIAL].state[names["flags"]][names["home_flag"]] = False
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Reject an invalid extra pause as the real firmware can do."""
        if (
            payload.get(names["key"]) == names["pause"]
            and not live[names["flags"]]["pause"]
        ):
            raise CommandError("Pause unavailable")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    await coordinator.async_return_to_base(SERIAL)
    assert sent_names(server, names) == [names["state"], names["home"], names["state"]]


@pytest.mark.parametrize("after_pause", [False, True])
async def test_missing_native_home_flag_never_allows_movement(
    hass,
    loaded,
    after_pause,
):
    """Require explicit home permission in both generations before and after pause."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect
    if after_pause:
        live.update(state=2 if generation == GEN3 else "busy")
        live[names["flags"]].update(pause=True, resume=False)
        live[names["flags"]][names["home_flag"]] = False
    else:
        live[names["flags"]].pop(names["home_flag"])

    async def command(serial, payload):
        """Omit native home readiness from the confirmed post-pause reply."""
        result = await original(serial, payload)
        if payload.get(names["key"]) == names["pause"]:
            live[names["flags"]].pop(names["home_flag"])
        return result

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(ServiceValidationError) as error:
            await coordinator.async_return_to_base(SERIAL)
    assert error.value.translation_key == "action_unavailable"
    assert names["home"] not in sent_names(server, names)


async def test_empty_or_failed_preflight_cannot_send_home(hass, loaded):
    """Fail closed if current native permission cannot be obtained at all."""
    entry, server, generation = loaded
    names, _live = paused_robot(entry, server, generation)
    for reply in ({}, CannotConnect("Robot timed out")):
        server.client.async_command.reset_mock()
        server.client.async_command.side_effect = [reply]
        with pytest.raises(HomeAssistantError):
            await entry.runtime_data.async_return_to_base(SERIAL)
        assert sent_names(server, names) == [names["state"]]


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize("error_type", [CannotConnect, CommandError])
async def test_lost_home_ack_preserves_observed_navigation_and_reads_again(
    hass,
    loaded,
    error_type,
):
    """Surface command failure while observed home motion keeps its real activity."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Deliver actual home movement before failing its HTTP acknowledgement."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=2, action=9)
            coordinator._handle_event(serial, gen3_event(state=2, action=9))
            raise error_type("Home acknowledgement failed")
        return result

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    assert coordinator.robots[SERIAL].returning
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"
    assert sent_names(server, names).count(names["home"]) == 1
    assert SERIAL in coordinator._follow_ups
    server.client.async_command.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert sent_names(server, names) == [names["state"]]
    assert coordinator.robots[SERIAL].returning


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_uncertain_ack_keeps_bounded_intent_for_delayed_motion(hass, loaded):
    """Correlate delayed motion without claiming the robot moved from ACK loss alone."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Lose the acknowledgement before any movement event has arrived."""
        if payload.get("cmd") == "sendToBase":
            raise CannotConnect("Home acknowledgement lost")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.return_phase == "pending"
    assert not robot.returning
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert robot.returning
    assert sent_names(server, names).count(names["home"]) == 1


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_robot_timeout_keeps_bounded_intent_for_delayed_motion(hass, loaded):
    """Treat Vacuula's 504 robot timeout as uncertain delivery, not a rejection."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Time out Home after Vacuula may already have sent it to the robot."""
        if payload.get("cmd") == "sendToBase":
            raise CommandTimedOut("Robot did not answer in time", status=504)
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.return_phase == "pending"
    # The robot did receive Home and drives off while recovering its location.
    live.update(state=2, action=9)
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert robot.returning
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"
    # A second Home press must not pause the trip or send Home again.
    await coordinator.async_return_to_base(SERIAL)
    sent = sent_names(server, names)
    assert sent.count(names["home"]) == 1
    assert names["pause"] not in sent


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_explicit_home_rejection_clears_unobserved_intent(hass, loaded):
    """Do not associate later navigation with a home command the robot rejected."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Reject only Home while preserving normal authoritative state reads."""
        if payload.get("cmd") == "sendToBase":
            raise CommandError("Home rejected")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.return_phase is None
    assert not robot.returning
    assert sent_names(server, names).count(names["home"]) == 1


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_repeated_paused_events_and_reads_preserve_pending_departure(
    hass,
    loaded,
):
    """Treat duplicate paused readbacks and SSE events consistently before departure."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    _names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Delay physical departure beyond acknowledgement and readback."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=3, action=1)
        return result

    server.client.async_command.side_effect = command
    await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.return_phase == "pending"
    deadline = robot.return_deadline
    for _ in range(3):
        coordinator._handle_event(SERIAL, gen3_event(state=3, action=1))
        async with coordinator._lock(SERIAL):
            await coordinator._async_read_state_locked(robot)
        assert robot.return_phase == "pending"
        assert robot.return_deadline == deadline
        assert not robot.returning
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize(
    "next_command",
    [
        "resumeCleaning",
        "startCleaning",
        "startPersistentMapExploration",
        "stopCleaning",
    ],
)
async def test_explicit_new_task_supersedes_pending_home_intent(
    hass,
    loaded,
    next_command,
):
    """Do not label later cleaning recovery as home after an explicit new task."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Acknowledge commands before their unchanged paused readback catches up."""
        if payload.get("cmd") == next_command == "stopCleaning":
            return await original(serial, payload)
        if payload.get("cmd") in {"sendToBase", next_command}:
            return {"result": "ok", "data": {}}
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.return_phase == "pending"
    if next_command == "resumeCleaning":
        await VacuulaVacuum(coordinator, SERIAL).async_start()
    else:
        payload = {"cmd": next_command}
        if next_command == "startCleaning":
            payload["params"] = {"category": 2, "mode": 1, "navigationMode": 1}
        await coordinator.async_command(SERIAL, payload)
    assert robot.return_phase is None
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert VacuulaVacuum(coordinator, SERIAL).activity == "cleaning"
    assert sent_names(server, names).count("sendToBase") == 1


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_native_pause_resume_preserves_a_resumable_trip_home(hass, loaded):
    """Keep home context if firmware explicitly allows pausing and resuming docking."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.returning
    deadline = robot.return_deadline
    live.update(state=2, action=9)
    live["availableCommands"].update(pause=True, resume=False)
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Resume the advertised paused navigation task using its action-9 report."""
        result = await original(serial, payload)
        if payload.get("cmd") == "resumeCleaning":
            live.update(state=2, action=9)
            coordinator._handle_event(serial, gen3_event(state=2, action=9))
        return result

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    vacuum = VacuulaVacuum(coordinator, SERIAL)
    await vacuum.async_pause()
    assert robot.return_phase == "paused"
    assert vacuum.activity == "paused"
    await vacuum.async_start()
    assert robot.returning
    assert vacuum.activity == "returning"
    assert robot.return_deadline == deadline
    assert names["home"] not in sent_names(server, names)


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_cancelled_home_keeps_observed_motion_and_unload_cancels_readback(
    hass,
    loaded,
):
    """Avoid retrying cancelled movement or leaving a follow-up after entry unload."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect
    moved = asyncio.Event()

    async def command(serial, payload):
        """Publish departure and hold acknowledgement until the task is cancelled."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=2, action=9)
            coordinator._handle_event(serial, gen3_event(state=2, action=9))
            moved.set()
            await asyncio.Event().wait()
        return result

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    task = asyncio.create_task(coordinator.async_return_to_base(SERIAL))
    await moved.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert coordinator.robots[SERIAL].returning
    assert not coordinator._lock(SERIAL).locked()
    assert sent_names(server, names).count(names["home"]) == 1
    assert SERIAL in coordinator._follow_ups
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not coordinator._follow_ups
    server.client.async_command.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    server.client.async_command.assert_not_awaited()


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_unload_cancels_an_already_running_home_follow_up(hass, loaded):
    """Cancel an in-flight state-only reconciliation when its entry is unloaded."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    _names, _live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect

    async def home_command(serial, payload):
        """Lose the home acknowledgement after observed navigation starts."""
        result = await original(serial, payload)
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=2, action=9)
            coordinator._handle_event(serial, gen3_event(state=2, action=9))
            raise CannotConnect("Home acknowledgement lost")
        return result

    server.client.async_command.side_effect = home_command
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    reading = asyncio.Event()
    cancelled = asyncio.Event()

    async def state_read(serial, payload):
        """Hold the follow-up state request until entry unload cancels it."""
        assert payload == {"cmd": "getRobotState"}
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server.client.async_command.side_effect = state_read
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await asyncio.wait_for(reading.wait(), timeout=1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert cancelled.is_set()
    assert not coordinator._lock(SERIAL).locked()


@pytest.mark.parametrize("after_pause", [False, True])
async def test_disconnect_during_home_state_reads_sends_no_home(
    hass,
    loaded,
    after_pause,
):
    """Check connectivity again after both awaited home-readiness snapshots."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    if after_pause:
        live.update(state=2 if generation == GEN3 else "busy")
        live[names["flags"]].update(pause=True, resume=False)
    reads = 0
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Disconnect while the selected authoritative state request is in flight."""
        nonlocal reads
        result = await original(serial, payload)
        if payload.get(names["key"]) == names["state"]:
            reads += 1
            if reads == (2 if after_pause else 1):
                coordinator.robots[serial].connected = False
        return result

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        with pytest.raises(HomeAssistantError) as error:
            await coordinator.async_return_to_base(SERIAL)
    assert error.value.translation_key == "offline"
    assert names["home"] not in sent_names(server, names)


def test_pending_and_moving_intent_have_absolute_monotonic_bounds(monkeypatch):
    """Expire pending departure and a moving/paused trip without renewing on reports."""
    now = [1000.0]
    monkeypatch.setattr(robot_data, "monotonic", lambda: now[0])
    robot = RobotData("robot", "Neato", GEN3, state={"state": 3, "action": 1})
    begin_return_trip(robot)
    pending_deadline = robot.return_deadline
    assert pending_deadline == now[0] + RETURN_PENDING_SECONDS
    now[0] += RETURN_PENDING_SECONDS - 1
    track_return_trip(robot)
    assert robot.return_phase == "pending"
    assert robot.return_deadline == pending_deadline
    now[0] += 1
    track_return_trip(robot)
    assert robot.return_phase is None
    begin_return_trip(robot)
    robot.state.update(state=2, action=9)
    track_return_trip(robot)
    trip_deadline = robot.return_deadline
    assert trip_deadline == now[0] + RETURN_TRIP_SECONDS
    assert robot.returning
    now[0] += 100
    track_return_trip(robot)
    assert robot.return_deadline == trip_deadline
    robot.state.update(state=3)
    track_return_trip(robot)
    assert robot.return_phase == "paused"
    assert not robot.returning
    robot.state.update(state=2)
    track_return_trip(robot)
    assert robot.returning
    assert robot.return_deadline == trip_deadline
    now[0] = trip_deadline
    assert not robot.returning
    track_return_trip(robot)
    assert robot.return_phase is None


@pytest.mark.parametrize("state", [1, 4, None])
def test_terminal_or_unknown_state_clears_home_intent(state):
    """Do not carry earlier home intent into a new cleaning run after completion."""
    robot = RobotData("robot", "Neato", GEN3, state={"state": 2, "action": 9})
    begin_return_trip(robot)
    track_return_trip(robot)
    assert robot.returning
    robot.state["state"] = state
    track_return_trip(robot)
    assert robot.return_phase is None
    robot.state.update(state=2, action=9)
    track_return_trip(robot)
    assert not robot.returning


def test_unrelated_motion_clears_active_but_not_pending_home_intent():
    """Allow an old busy readback during departure but stop tracking real cleaning."""
    robot = RobotData("robot", "Neato", GEN3, state={"state": 2, "action": 1})
    begin_return_trip(robot)
    track_return_trip(robot)
    assert robot.return_phase == "pending"
    robot.state["action"] = 4
    track_return_trip(robot)
    assert robot.returning
    robot.state["action"] = 1
    track_return_trip(robot)
    assert robot.return_phase is None


def test_gen4_uses_native_docking_without_gen3_command_intent():
    """Keep Gen4 activity entirely dependent on its unambiguous native action."""
    robot = RobotData(
        "robot", "Neato", GEN4, state={"state": "busy", "action": "docking"}
    )
    begin_return_trip(robot)
    track_return_trip(robot)
    assert robot.return_phase is None
    assert not robot.returning
