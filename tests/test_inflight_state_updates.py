"""Reconcile robot push events with state replies already in flight."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_integration import loaded  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.vacuum import VacuulaVacuum


def state_event(generation, state):
    """Wrap a state delta in the server's generation-specific event envelope."""
    if generation == GEN3:
        return {"data": {"type": "state", "data": state}}
    return {"data": {"to": {"service": "peer.state"}, "body": state}}


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
async def test_start_keeps_newer_busy_event_before_old_state_reply(hass, loaded):  # noqa: F811
    """Refuse a new start when a newer event supersedes an idle HTTP snapshot."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    gen3 = generation == GEN3
    flags_key = "availableCommands" if gen3 else "available_commands"
    dock_key = "isDocked" if gen3 else "is_docked"
    original = server.client.async_command.side_effect
    state_read = "getRobotState" if gen3 else "state.show"
    writes = []
    delivered = False

    async def command(serial, payload):
        """Deliver a busy event before returning the previously captured idle reply."""
        nonlocal delivered
        name = payload.get("cmd", payload.get("ability"))
        response = await original(serial, payload)
        if name == state_read and not delivered:
            delivered = True
            newer = {
                "state": 2 if gen3 else "busy",
                "action": 1 if gen3 else "cleaning",
                flags_key: {"start": False, "resume": False, "pause": True},
                "details": {dock_key: False},
            }
            server.state[serial].update(state=newer["state"], action=newer["action"])
            server.state[serial][flags_key].update(newer[flags_key])
            server.state[serial]["details"].update(newer["details"])
            coordinator._handle_event(serial, state_event(generation, newer))
        elif name != state_read:
            writes.append(deepcopy(payload))
        return response

    server.client.async_command.side_effect = command
    with pytest.raises(ServiceValidationError) as error:
        await VacuulaVacuum(coordinator, "TEST-robot").async_start()
    assert error.value.translation_key == "action_unavailable"
    assert writes == []
    assert coordinator.robots["TEST-robot"].state["state"] == (2 if gen3 else "busy")


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
async def test_partial_newer_events_preserve_other_fresh_reply_fields(hass, loaded):  # noqa: F811
    """Merge newer partial details and flags without losing fresh battery data."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    gen3 = generation == GEN3
    flags_key = "availableCommands" if gen3 else "available_commands"
    dock_key = "isDocked" if gen3 else "is_docked"
    server.state["TEST-robot"]["details"]["charge"] = 91
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Send separate partial pushes after capturing the fresh HTTP snapshot."""
        response = await original(serial, payload)
        coordinator._handle_event(
            serial, state_event(generation, {"details": {dock_key: False}})
        )
        coordinator._handle_event(
            serial, state_event(generation, {flags_key: {"start": False}})
        )
        return response

    server.client.async_command.side_effect = command
    async with coordinator._lock("TEST-robot"):
        assert await coordinator._async_read_state_locked(
            coordinator.robots["TEST-robot"]
        )
    state = coordinator.robots["TEST-robot"].state
    assert state["details"]["charge"] == 91
    assert state["details"][dock_key] is False
    assert state[flags_key]["start"] is False
    assert state[flags_key]["resume"] is False


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_read_does_not_overlay_its_events_on_a_later_read(
    hass,
    loaded,  # noqa: F811
    cancelled,
):
    """Keep pushes during failed or cancelled I/O without poisoning a later read."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    original = server.client.async_command.side_effect

    async def failed_command(serial, payload):
        """Receive a busy event while the older HTTP request subsequently fails."""
        coordinator._handle_event(
            serial,
            state_event(
                generation,
                {"state": 2 if generation == GEN3 else "busy"},
            ),
        )
        if cancelled:
            raise asyncio.CancelledError
        raise CannotConnect("State response lost")

    robot = coordinator.robots["TEST-robot"]
    server.client.async_command.side_effect = failed_command
    async with coordinator._lock(robot.serial):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await coordinator._async_read_state_locked(robot)
        else:
            assert not await coordinator._async_read_state_locked(robot)
    assert not coordinator._state_read_updates
    assert robot.state["state"] == (2 if generation == GEN3 else "busy")
    server.client.async_command.side_effect = original
    async with coordinator._lock(robot.serial):
        assert await coordinator._async_read_state_locked(robot)
    assert robot.state["state"] == (1 if generation == GEN3 else "idle")


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
@pytest.mark.parametrize("body", [None, [], "invalid"])
async def test_malformed_push_cannot_invalidate_a_fresh_reply(hass, loaded, body):  # noqa: F811
    """Ignore malformed state envelopes instead of retaining stale cached fields."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    original = server.client.async_command.side_effect
    server.state["TEST-robot"]["details"]["charge"] = 84

    async def command(serial, payload):
        """Deliver an invalid push body during an otherwise valid state read."""
        response = await original(serial, payload)
        coordinator._handle_event(serial, state_event(generation, body))
        return response

    server.client.async_command.side_effect = command
    async with coordinator._lock("TEST-robot"):
        assert await coordinator._async_read_state_locked(
            coordinator.robots["TEST-robot"]
        )
    assert coordinator.robots["TEST-robot"].state["details"]["charge"] == 84
