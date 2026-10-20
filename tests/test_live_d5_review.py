"""Regression tests for problems seen on the live D5 and in the 0.1.8 review."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_integration import entity_id, loaded  # noqa: F401
from test_review_fixes import unit_coordinator
from test_schedule_merge import call, schedule_setup  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.coordinator import FOLLOW_UP_DELAY
from custom_components.vacuula.switch import VacuulaScheduleEventSwitch

SERIAL = "TEST-robot"


def gen3_event(**state):
    """Wrap a partial Gen3 state as the server's robot event."""
    return {"data": {"type": "state", "data": state}}


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_d5_sent_home_reports_returning_for_the_whole_trip(hass, loaded):  # noqa: F811
    """Show returning while the D5 recovers its location on the way home.

    The live D5 reports action 9 right after sendToBase, usually before the
    command returns, and sends no event when it switches to docking, so the
    trip used to show as cleaning.
    """
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    vacuum = entity_id(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Deliver the robot's pause and action-9 events before commands return."""
        result = await original(serial, payload)
        if payload.get("cmd") == "pauseCleaning":
            coordinator._handle_event(serial, gen3_event(state=3, action=1))
        if payload.get("cmd") == "sendToBase":
            server.state[serial].update(state=2, action=9)
            server.state[serial]["cleaning"]["category"] = 2
            coordinator._handle_event(serial, gen3_event(state=2, action=9))
        return result

    server.client.async_command.side_effect = command
    seen = []

    @callback
    def record(event):
        """Collect every state the vacuum entity writes."""
        if event.data["entity_id"] == vacuum and event.data["new_state"]:
            seen.append(event.data["new_state"].state)

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, record)
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        await hass.services.async_call(
            "vacuum", "return_to_base", {"entity_id": vacuum}, blocking=True
        )
    await hass.async_block_till_done()
    robot = coordinator.robots[SERIAL]
    assert robot.returning
    assert hass.states.get(vacuum).state == "returning"
    coordinator._handle_event(SERIAL, gen3_event(state=2, action=4))
    await hass.async_block_till_done()
    assert hass.states.get(vacuum).state == "returning"
    coordinator._handle_event(
        SERIAL, gen3_event(state=1, action=0, details={"isDocked": True})
    )
    await hass.async_block_till_done()
    unsubscribe()
    assert hass.states.get(vacuum).state == "docked"
    assert seen == ["paused", "returning", "docked"]
    assert not robot.returning
    # Recovering location during a later run is cleaning again.
    coordinator._handle_event(
        SERIAL,
        gen3_event(
            state=2, action=9, cleaning={"category": 2}, details={"isDocked": False}
        ),
    )
    await hass.async_block_till_done()
    assert hass.states.get(vacuum).state == "cleaning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_failed_home_command_does_not_mark_a_trip(hass, loaded):  # noqa: F811
    """Clear the trip mark when Vacuula reports that sendToBase failed."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    vacuum = entity_id(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Fail only the home command."""
        if payload.get("cmd") == "sendToBase":
            raise CannotConnect("robot timeout")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    with (
        patch(
            "custom_components.vacuula.coordinator.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            "vacuum", "return_to_base", {"entity_id": vacuum}, blocking=True
        )
    assert not coordinator.robots[SERIAL].returning


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_events_of_a_run_started_elsewhere_get_a_follow_up(hass, loaded):  # noqa: F811
    """Read state again after a run the app, a schedule or the robot started."""
    entry, _server, _generation = loaded
    coordinator = entry.runtime_data
    coordinator._handle_event(
        SERIAL, gen3_event(state=1, action=0, details={"isDocked": True})
    )
    assert SERIAL not in coordinator._follow_ups
    coordinator._handle_event(
        SERIAL, gen3_event(state=2, action=1, details={"isDocked": False})
    )
    assert SERIAL in coordinator._follow_ups


@pytest.mark.parametrize("loaded", [GEN4], indirect=True)
async def test_gen4_return_is_not_tracked(hass, loaded):  # noqa: F811
    """Keep Gen4 on its own docking action; only the D5's code 9 is ambiguous."""
    entry, _server, _generation = loaded
    vacuum = entity_id(hass, "vacuum", "vacuum")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    with patch(
        "custom_components.vacuula.coordinator.asyncio.sleep", new_callable=AsyncMock
    ):
        await hass.services.async_call(
            "vacuum", "return_to_base", {"entity_id": vacuum}, blocking=True
        )
    assert not entry.runtime_data.robots[SERIAL].returning
    assert hass.states.get(vacuum).state == "returning"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_state_is_read_again_after_the_robot_drives_off(hass, loaded):  # noqa: F811
    """Catch flags that change without an event a few seconds after a start."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    vacuum = entity_id(hass, "vacuum", "vacuum")
    battery = entity_id(hass, "sensor", "battery")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    assert SERIAL in coordinator._follow_ups
    before = hass.states.get(battery).state
    server.state[SERIAL]["details"]["charge"] = 55
    server.client.async_command.reset_mock()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=FOLLOW_UP_DELAY + 1)
    )
    await hass.async_block_till_done()
    assert before != "55"
    assert hass.states.get(battery).state == "55"
    sent = [item.args[1] for item in server.client.async_command.await_args_list]
    assert sent == [{"cmd": "getRobotState"}]
    assert SERIAL not in coordinator._follow_ups


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_follow_up_is_replaced_and_cancelled_on_unload(hass, loaded):  # noqa: F811
    """Keep one pending follow-up per robot and drop it when the entry unloads."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    coordinator._schedule_state_follow_up(SERIAL)
    first = coordinator._follow_ups[SERIAL]
    coordinator._schedule_state_follow_up(SERIAL)
    assert coordinator._follow_ups[SERIAL] is not first
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._follow_ups == {}
    server.client.async_command.reset_mock()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=FOLLOW_UP_DELAY + 1)
    )
    await hass.async_block_till_done()
    server.client.async_command.assert_not_awaited()


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_follow_up_skips_a_robot_that_disconnected(hass, loaded):  # noqa: F811
    """Send nothing when the robot left Vacuula before the follow-up was due."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    coordinator._schedule_state_follow_up(SERIAL)
    coordinator.robots[SERIAL].connected = False
    server.client.async_command.reset_mock()
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=FOLLOW_UP_DELAY + 1)
    )
    await hass.async_block_till_done()
    server.client.async_command.assert_not_awaited()


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_failed_follow_up_read_leaves_entities_alone(hass, loaded):  # noqa: F811
    """Keep the cached state when the follow-up read is not answered."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    battery = entity_id(hass, "sensor", "battery")
    before = hass.states.get(battery).state
    coordinator._schedule_state_follow_up(SERIAL)
    server.client.async_command.side_effect = CannotConnect("no answer")
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=FOLLOW_UP_DELAY + 1)
    )
    await hass.async_block_till_done()
    assert hass.states.get(battery).state == before


async def test_no_follow_up_is_scheduled_for_an_entry_that_is_not_running(hass):
    """Schedule nothing while the config entry is not loaded."""
    coordinator = unit_coordinator(hass)
    coordinator._schedule_state_follow_up("robot")
    assert coordinator._follow_ups == {}


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_navigation_select_shows_what_the_next_run_uses(hass, loaded):  # noqa: F811
    """Show Normal, which Start sends, not the last run's navigation."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    server.state[SERIAL]["cleaning"]["navigationMode"] = 2
    await coordinator.async_refresh_robot(SERIAL, full=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "select", "navigation_mode")).state == (
        "normal"
    )


@pytest.mark.parametrize("loaded", [GEN4], indirect=True)
async def test_failed_language_read_keeps_the_language_select(hass, loaded):  # noqa: F811
    """Keep the language list when only the separate language read fails."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    select = entity_id(hass, "select", "robot_language")
    original = server.client.async_command.side_effect

    async def command(serial, payload):
        """Fail only the language read, as a Vacuula robot timeout would."""
        if payload.get("ability") == "settings.robot_language":
            raise CannotConnect("robot timeout")
        return await original(serial, payload)

    server.client.async_command.side_effect = command
    await coordinator.async_refresh_robot(SERIAL, full=True)
    await hass.async_block_till_done()
    state = hass.states.get(select)
    assert state.state == "en"
    assert state.attributes["options"] == ["en", "de"]


@pytest.mark.parametrize("loaded", [GEN4], indirect=True)
async def test_map_image_keeps_its_name_while_maps_are_unknown(hass, loaded):  # noqa: F811
    """Keep the floorplan name during an upload instead of showing Map Floor."""
    entry, _server, _generation = loaded
    coordinator = entry.runtime_data
    image = entity_id(hass, "image", "map_floor")
    assert hass.states.get(image).attributes["friendly_name"] == "D10 Map Downstairs"
    coordinator._set_pending_promotion(SERIAL, "floor")
    await hass.async_block_till_done()
    state = hass.states.get(image)
    assert state.state == "unavailable"
    assert state.attributes["friendly_name"] == "D10 Map Downstairs"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_off_step_reminder_names_the_allowed_values(hass, loaded):  # noqa: F811
    """Explain the allowed range and step instead of blaming the robot."""
    entity = entity_id(hass, "number", "filter_reminder")
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity, "value": 31}, blocking=True
        )
    assert error.value.translation_key == "invalid_number"
    assert error.value.translation_placeholders == {
        "min": "30",
        "max": "90",
        "step": "15",
    }


def pending_recovery(setup):
    """Leave map1 demoted after a failed upload, with the recovery marker set."""
    coordinator = setup[0]
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    coordinator._set_pending_promotion("TEST-robot", "map1")


async def test_mapped_event_can_be_turned_off_during_recovery(hass, schedule_setup):  # noqa: F811
    """Allow disabling a mapped event through the action while recovery is pending."""
    coordinator, _robot, _device, server, writes = schedule_setup
    pending_recovery(schedule_setup)
    await call(hass, schedule_setup, enabled=False)
    assert [event["enabled"] for event in writes] == [False]
    assert coordinator.has_pending_map_recovery("TEST-robot")
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, enabled=True)
    assert error.value.translation_key == "map_promotion_required"
    assert len(writes) == 1


@pytest.mark.parametrize("enabled", [True, False])
async def test_event_switch_follows_the_recovery_guard(hass, schedule_setup, enabled):  # noqa: F811
    """Refuse to re-arm a mapped event with the switch; turning it off still works."""
    coordinator, robot, _device, server, writes = schedule_setup
    server["events"][0]["enabled"] = not enabled
    robot.schedule = deepcopy(server)
    pending_recovery(schedule_setup)
    switch = VacuulaScheduleEventSwitch(coordinator, "TEST-robot", "event1")
    if enabled:
        with pytest.raises(ServiceValidationError) as error:
            await switch.async_turn_on()
        assert error.value.translation_key == "map_promotion_required"
        assert writes == []
    else:
        await switch.async_turn_off()
        assert [event["enabled"] for event in writes] == [False]


async def test_other_refusals_are_not_rechecked(hass):
    """Read nothing extra when a builder refuses for a reason other than readiness."""
    coordinator = unit_coordinator(hass)

    def build(robot):
        """Refuse like a mapped start without a usable map."""
        raise ServiceValidationError(
            translation_domain="vacuula", translation_key="map_unavailable"
        )

    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_command("robot", build)
    assert error.value.translation_key == "map_unavailable"
    coordinator.client.async_command.assert_not_awaited()
