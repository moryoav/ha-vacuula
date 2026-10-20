"""Preserve bounded home intent across repeated administrator or user requests."""

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_home_intent import SERIAL, gen3_event, paused_robot, sent_names
from test_home_intent import loaded as home_loaded

from custom_components.vacuula.client import CommandError
from custom_components.vacuula.const import GEN3
from custom_components.vacuula.vacuum import VacuulaVacuum

loaded = home_loaded


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
@pytest.mark.parametrize("departed", [False, True])
async def test_repeated_home_does_not_resend_or_erase_accepted_intent(
    hass, loaded, departed
):
    """Keep an accepted Home through duplicates without restarting its deadline."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    names, live = paused_robot(entry, server, generation)
    original = server.client.async_command.side_effect
    homes = 0

    async def command(serial, payload):
        """Accept one Home before departure and reject any duplicate transmission."""
        nonlocal homes
        if payload.get("cmd") == "sendToBase":
            homes += 1
            if homes > 1:
                raise CommandError("Duplicate Home rejected")
            return {"result": "ok", "data": {}}
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    server.client.async_command.reset_mock()
    await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    if departed:
        live.update(state=2, action=9)
        live["availableCommands"]["pause"] = True
        coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    deadline = robot.return_deadline
    await coordinator.async_return_to_base(SERIAL)
    assert homes == 1
    assert names["pause"] not in sent_names(server, names)
    assert robot.return_deadline == deadline
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_rejected_home_preserves_an_explicitly_paused_trip(hass, loaded):
    """A rejected fresh Home must not erase a previously observed, resumable trip."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    _names, live = paused_robot(entry, server, generation)
    await coordinator.async_return_to_base(SERIAL)
    robot = coordinator.robots[SERIAL]
    assert robot.returning
    live.update(state=3, action=9)
    live["availableCommands"].update(pause=False, resume=True, goToBase=True)
    coordinator._handle_event(SERIAL, gen3_event(state=3, action=9))
    deadline = robot.return_deadline
    original = server.client.async_command.side_effect

    async def reject_home(serial, payload):
        """Reject the new home request without replacing the earlier navigation task."""
        if payload.get("cmd") == "sendToBase":
            raise CommandError("New Home rejected")
        return await original(serial, payload)

    server.client.async_command.side_effect = reject_home
    with pytest.raises(HomeAssistantError):
        await coordinator.async_return_to_base(SERIAL)
    assert robot.return_phase == "paused"
    assert robot.return_deadline == deadline
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=9))
    assert VacuulaVacuum(coordinator, SERIAL).activity == "returning"
