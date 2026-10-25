"""Regression tests for the 0.1.5 review fixes."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import probatio as vol
import pytest
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_discovery_address import offline
from test_integration import entity_id, loaded  # noqa: F401
from test_server_identity import advertisement
from test_services import call, setup  # noqa: F401

from custom_components.vacuula import async_remove_config_entry_device
from custom_components.vacuula.binary_sensor import DESCRIPTIONS as BINARY
from custom_components.vacuula.binary_sensor import binary_value
from custom_components.vacuula.client import (
    CannotConnect,
    CommandError,
    UnsupportedCommand,
)
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.entity import async_remove_stale_entity
from custom_components.vacuula.image import VacuulaMapImage
from custom_components.vacuula.sensor import DESCRIPTIONS as SENSORS
from custom_components.vacuula.sensor import sensor_value
from custom_components.vacuula.services import _BOUNDARY
from custom_components.vacuula.switch import VacuulaScheduleEventSwitch


def unit_coordinator(hass, generation=GEN3):
    """Return a coordinator with one connected robot and a mocked client."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_command = AsyncMock(return_value={"code": 200, "body": {}})
    client.async_maps = AsyncMock(return_value=[])
    client.async_map_action = AsyncMock(return_value={})
    client.async_update_map = AsyncMock()
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots["robot"] = RobotData("robot", "Neato", generation, True)
    return coordinator


def description(descriptions, key):
    """Look up an entity description by its telemetry key."""
    return next(item for item in descriptions if item.key == key)


def is_state_read(payload):
    """Recognize the state-read payload for either robot generation."""
    return payload.get("cmd") == "getRobotState" or (
        payload.get("ability") == "state.show"
    )


@pytest.mark.parametrize("loaded", [GEN3, GEN4], indirect=True)
async def test_failed_robot_read_keeps_robot_and_controls_available(hass, loaded):  # noqa: F811
    """Vacuula's 504 timeout must not make the robot unavailable and drop stop."""
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    vacuum = entity_id(hass, "vacuum", "vacuum")
    original = server.client.async_command.side_effect

    async def timeout(serial, payload):
        """Time out state reads while leaving other commands usable."""
        if is_state_read(payload):
            raise CommandError("Robot did not answer in time", status=504)
        return await original(serial, payload)

    server.client.async_command.side_effect = timeout
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(vacuum).state != STATE_UNAVAILABLE
    assert hass.states.get(entity_id(hass, "binary_sensor", "connected")).state == "on"
    server.client.async_command.side_effect = original
    server.client.async_command.reset_mock()
    await hass.services.async_call(
        "vacuum", "locate", {"entity_id": vacuum}, blocking=True
    )
    first = server.client.async_command.await_args_list[0].args[1]
    assert first == (
        {"cmd": "findMe"} if generation == GEN3 else {"ability": "utilities.find_me"}
    )


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_failed_map_read_during_outage_keeps_entities_unavailable(hass, loaded):  # noqa: F811
    """Keep controls unavailable when a server outage also prevents fresh map reads."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    vacuum = entity_id(hass, "vacuum", "vacuum")
    server.client.async_server_info.side_effect = CannotConnect("server down")
    server.client.async_maps.side_effect = CannotConnect("server down")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(vacuum).state == STATE_UNAVAILABLE
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "TEST-robot"), entry.entry_id
    )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "clean", {"device_id": device.id}, blocking=True
        )
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert hass.states.get(vacuum).state == STATE_UNAVAILABLE


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_refresh_button_reports_failed_reads(hass, loaded):  # noqa: F811
    """Translate map-read failures from the refresh button into an action error."""
    _entry, server, _generation = loaded
    server.client.async_maps.side_effect = CannotConnect("server down")
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, "button", "button_refresh")},
            blocking=True,
        )
    assert error.value.translation_key == "command_failed"


def test_gen4_informational_notices_are_not_problems():
    """Ignore informational Gen4 notices but expose a later warning as a problem."""
    robot = RobotData(
        "robot",
        "D10",
        GEN4,
        True,
        state={"errors": [{"code": "charging_base", "severity": "invalid"}]},
    )
    problem = description(BINARY, "problem")
    error = description(SENSORS, "error")
    assert binary_value(robot, problem) is False
    assert sensor_value(robot, error) == "none"
    robot.state["errors"].append({"code": "lw_hanging", "severity": "warning"})
    assert binary_value(robot, problem) is True
    assert sensor_value(robot, error) == "lw_hanging"


def test_gen3_alert_sensor_reports_none_when_no_alert():
    """Distinguish a cleared Gen3 alert from a named active alert."""
    robot = RobotData("robot", "D5", GEN3, True, state={"error": None, "alert": None})
    alert = description(SENSORS, "alert")
    assert sensor_value(robot, alert) == "none"
    robot.state["alert"] = "dustbin_full"
    assert sensor_value(robot, alert) == "dustbin_full"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_removed_robot_returns_with_its_entities(hass, loaded):  # noqa: F811
    """Recreate a removed device and its entities when the robot reappears."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    device = devices.async_get_device_by_identifier(
        (DOMAIN, "TEST-robot"), entry.entry_id
    )
    original_runs = server.client.async_runs.side_effect

    async def runs(serial):
        """Return history only for robots still listed by the simulated server."""
        return await original_runs(serial) if serial in server.robots else []

    server.client.async_runs.side_effect = runs
    metadata = server.robots.pop("TEST-robot")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert await async_remove_config_entry_device(hass, entry, device)
    # What the device page does once the integration allows the removal.
    devices.async_remove_device(device.id)
    await hass.async_block_till_done()
    assert (
        devices.async_get_device_by_identifier((DOMAIN, "TEST-robot"), entry.entry_id)
        is None
    )
    assert entities.async_get_entity_id("vacuum", DOMAIN, "TEST-robot_vacuum") is None

    server.robots["TEST-robot"] = metadata
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        devices.async_get_device_by_identifier((DOMAIN, "TEST-robot"), entry.entry_id)
        is not None
    )
    vacuum = entities.async_get_entity_id("vacuum", DOMAIN, "TEST-robot_vacuum")
    assert vacuum is not None
    assert hass.states.get(vacuum).state == "docked"


async def test_floorplan_must_be_in_use_and_the_active_one_stays_in_use(hass):
    """Reject promotion of an unlisted map and unuse of the active floorplan."""
    coordinator = unit_coordinator(hass, GEN4)
    client = coordinator.client
    client.async_maps.return_value = [{"id": "used", "promoted": True}]
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_map_operation("robot", "unlisted", "promote")
    assert error.value.translation_key == "map_not_in_use"
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_map_operation("robot", "used", "unuse")
    assert error.value.translation_key == "map_unuse_promoted"
    client.async_map_action.assert_not_awaited()
    assert not coordinator.has_pending_map_recovery("robot")
    client.async_maps.return_value = [{"id": "used", "promoted": False}]
    await coordinator.async_map_operation("robot", "used", "unuse")
    client.async_map_action.assert_awaited_once_with("robot", "used", "unuse", None)


async def test_map_switch_keeps_a_name_changed_in_the_app(hass):
    """Use the freshly read map name when changing its enabled flag."""
    coordinator = unit_coordinator(hass)
    coordinator.robots["robot"].maps = [{"id": "m", "name": "Kitchen", "enabled": True}]
    coordinator.client.async_maps.return_value = [
        {"id": "m", "name": "Ground floor", "enabled": True}
    ]
    await coordinator.async_set_map_enabled("robot", "m", False)
    coordinator.client.async_update_map.assert_awaited_once_with(
        "robot", "m", "Ground floor", False
    )


async def test_applied_map_change_is_not_reported_failed_when_readback_fails(hass):
    """Treat a successful map write as successful even if its follow-up read fails."""
    coordinator = unit_coordinator(hass)
    coordinator.client.async_maps.side_effect = CannotConnect("read failed")
    await coordinator.async_update_map("robot", "m", "Kitchen", True)
    coordinator.client.async_update_map.assert_awaited_once_with(
        "robot", "m", "Kitchen", True
    )


async def test_map_action_succeeds_when_only_the_readback_fails(hass, setup):  # noqa: F811
    """Preserve a successful map action result when the later refresh fails."""
    coordinator, robot, _, _ = setup
    robot.generation = GEN4
    coordinator.async_refresh_maps.side_effect = HomeAssistantError("read failed")
    await call(hass, setup, "map_action", map_id="map1", action="use")
    coordinator.client.async_map_action.assert_awaited_with(
        robot.serial, "map1", "use", None
    )


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_entity_and_device_names_follow_renames(hass, loaded):  # noqa: F811
    """Update map entity labels and the device name after server-side renames."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    image = entity_id(hass, "image", "map_floor")
    switch = entity_id(hass, "switch", "map_floor_enabled")
    assert hass.states.get(image).attributes["friendly_name"] == "D7 Map Downstairs"
    server.client.async_maps.return_value = [
        {"id": "floor", "name": "Upstairs", "enabled": True}
    ]
    await coordinator.async_refresh_maps("TEST-robot")
    await hass.async_block_till_done()
    assert hass.states.get(image).attributes["friendly_name"] == "D7 Map Upstairs"
    assert (
        hass.states.get(switch).attributes["friendly_name"] == "D7 Map Upstairs enabled"
    )
    server.robots["TEST-robot"]["name"] = "Kitchen robot"
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "TEST-robot"), entry.entry_id
    )
    assert device.name == "Kitchen robot"


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_deleted_map_entities_are_removed_and_can_return(hass, loaded):  # noqa: F811
    """Remove confirmed deleted maps and recreate returning map entities."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    image = entity_id(hass, "image", "map_floor")
    switch = entity_id(hass, "switch", "map_floor_enabled")
    floor = server.client.async_maps.return_value
    server.client.async_maps.return_value = []
    await coordinator.async_refresh_maps("TEST-robot")
    await hass.async_block_till_done()
    for removed in (image, switch):
        assert registry.async_get(removed) is None
        assert hass.states.get(removed) is None
    server.client.async_maps.return_value = floor
    await coordinator.async_refresh_maps("TEST-robot")
    await hass.async_block_till_done()
    assert (
        registry.async_get_entity_id("image", DOMAIN, "TEST-robot_map_floor") == image
    )
    assert hass.states.get(image) is not None


def test_boundary_schema_accepts_what_the_app_stores():
    """Accept finite app-style boundary coordinates and reject non-finite vertices."""
    boundary = {
        "id": "zone",
        "name": "",
        "color": "#abc",
        "type": "polyline",
        "vertices": [[-0.01, 0.5], [1.02, 0.5]],
        "enabled": True,
        "function": "nogo",
    }
    assert _BOUNDARY(boundary)["vertices"] == [[-0.01, 0.5], [1.02, 0.5]]
    with pytest.raises(vol.Invalid):
        _BOUNDARY({**boundary, "vertices": [[float("nan"), 0.5], [1, 1]]})


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_cleaning_defaults_stay_editable_while_robot_offline(hass, loaded):  # noqa: F811
    """Keep local cleaning mode and spot dimensions editable during disconnection."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    server.robots["TEST-robot"]["connected"] = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert (
        hass.states.get(entity_id(hass, "vacuum", "vacuum")).state == STATE_UNAVAILABLE
    )
    select = entity_id(hass, "select", "cleaning_mode")
    assert hass.states.get(select).state == "eco"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "turbo"},
        blocking=True,
    )
    assert coordinator.cleaning_options["TEST-robot"]["mode"] == "turbo"
    spot = entity_id(hass, "number", "spot_width")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": spot, "value": 300}, blocking=True
    )
    assert coordinator.cleaning_options["TEST-robot"]["spot_width"] == 300


async def test_server_outage_reason_is_translated(hass):
    """Attach the domain and translation key to coordinator server-outage errors."""
    coordinator = unit_coordinator(hass)
    coordinator.client.async_server_info = AsyncMock(side_effect=CannotConnect("down"))
    with pytest.raises(UpdateFailed) as error:
        await coordinator._async_update_data()
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "server_unavailable"


async def test_address_form_defaults_to_vacuula_default_port(hass):
    """Offer the default Vacuula hostname without forcing an explicit port."""
    with patch(
        "custom_components.vacuula.config_flow.async_discover_servers",
        new=AsyncMock(return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
    assert result["step_id"] == "user"
    marker = next(iter(result["data_schema"].schema))
    assert marker.default() == "http://vacuula.local"


async def test_servers_behind_one_proxy_address_stay_separate(hass):
    """Disjoint robot lists identify different servers sharing one address."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id="https://vac1.example.com",
        data={"url": "https://vac1.example.com", "robot_ids": ["R1"]},
    )
    existing.add_to_hass(hass)
    before = dict(existing.data)
    with (
        patch("custom_components.vacuula.config_flow.VacuulaClient") as client,
        patch(
            "custom_components.vacuula.config_flow."
            "HomeAssistantTCPConnector.async_resolve_host",
            new_callable=AsyncMock,
            return_value=[{"host": "198.51.100.7"}],
        ),
        patch("custom_components.vacuula.async_setup_entry", return_value=True),
    ):
        client.return_value.async_server_info = AsyncMock(return_value={})
        client.return_value.async_robots = AsyncMock(return_value={"R2": {}})
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "user"},
            data={"url": "https://vac2.example.com"},
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert dict(existing.data) == before


async def test_address_update_keeps_a_unique_id_held_by_an_ignored_discovery(
    hass, caplog
):
    """Update an entry's address without taking an ignored announcement's unique ID."""
    MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IGNORE, unique_id="http://192.0.2.1:81", data={}
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://old.local:81",
        data={"url": "http://old.local:81", "robot_ids": ["R1"]},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as client:
        client.return_value.async_server_info = AsyncMock(return_value={})
        client.return_value.async_robots = AsyncMock(return_value={"R1": {}})
        offline(client, "http://old.local:81")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=advertisement()
        )
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://192.0.2.1:81"
    assert entry.unique_id == "http://old.local:81"
    assert "already in use" not in caplog.text


async def test_forgetting_a_robot_releases_its_stream_notice_and_entities(hass):
    """Cancel a forgotten robot's stream and clear recovery and entity bookkeeping."""
    coordinator = unit_coordinator(hass, GEN4)
    stream = asyncio.create_task(asyncio.sleep(60))
    coordinator._streams["robot"] = stream
    coordinator._set_pending_promotion("robot", "map1")
    coordinator.known_entities = {
        "sensor": {"robot": {"robot_battery"}, "other": {"x"}}
    }
    coordinator.async_forget_robot("robot")
    with suppress(asyncio.CancelledError):
        await stream
    assert stream.cancelled()
    assert not coordinator.has_pending_map_recovery("robot")
    assert coordinator.known_entities == {"sensor": {"other": {"x"}}}
    robot = coordinator.robots["robot"]
    assert not robot.listed
    assert not robot.connected


async def test_schedule_reread_only_runs_for_a_connected_gen4_robot(hass):
    """Skip Gen3 schedule rereads and retain Gen4 events after unsupported replies."""
    coordinator = unit_coordinator(hass, GEN3)
    await coordinator._async_read_schedule_locked("robot")
    coordinator.client.async_command.assert_not_awaited()
    coordinator.robots["robot"].generation = GEN4
    coordinator.robots["robot"].schedule = {"events": []}
    coordinator.client.async_command.side_effect = UnsupportedCommand("unsupported")
    await coordinator._async_read_schedule_locked("robot")
    assert coordinator.robots["robot"].schedule == {"events": []}


async def test_map_switch_reports_a_missing_map_and_a_failed_write(hass):
    """Distinguish a missing map from a failed enabled-state update."""
    coordinator = unit_coordinator(hass)
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_set_map_enabled("robot", "gone", True)
    assert error.value.translation_key == "action_unavailable"
    coordinator.client.async_maps.return_value = [
        {"id": "m", "name": "Kitchen", "enabled": True}
    ]
    coordinator.client.async_update_map.side_effect = CannotConnect("write failed")
    with pytest.raises(HomeAssistantError) as error:
        await coordinator.async_set_map_enabled("robot", "m", False)
    assert error.value.translation_key == "command_failed"


@pytest.mark.parametrize(("errors", "expected"), [(None, "none"), ("bad", None)])
def test_gen4_cleared_or_malformed_error_lists(errors, expected):
    """Treat cleared errors as healthy and malformed lists as unknown."""
    robot = RobotData("robot", "D10", GEN4, True, state={"errors": errors})
    assert sensor_value(robot, description(SENSORS, "error")) == expected
    assert binary_value(robot, description(BINARY, "problem")) is (
        None if expected is None else False
    )


def test_gen4_state_without_an_error_list_reports_nothing():
    """Keep error sensors unknown when the Gen4 state omits its error collection."""
    robot = RobotData("robot", "D10", GEN4, True, state={"state": "idle"})
    assert sensor_value(robot, description(SENSORS, "error")) is None
    assert binary_value(robot, description(BINARY, "problem")) is None


async def test_removing_an_unregistered_stale_entity_only_forgets_it(hass):
    """Clear stale tracking safely when the entity has no registry entry."""
    coordinator = unit_coordinator(hass)
    entity = VacuulaMapImage(coordinator, "robot", "map1")
    module = type(entity).__module__
    coordinator.known_entities = {module: {"robot": {"robot_map_map1"}}}
    async_remove_stale_entity(entity, "robot")
    assert coordinator.known_entities == {module: {"robot": set()}}


async def test_schedule_event_name_without_days_uses_the_time(hass):
    """Use the event's clock time as its label when weekdays are absent."""
    coordinator = unit_coordinator(hass, GEN4)
    coordinator.robots["robot"].schedule = {
        "events": [{"id": "e", "time": "09:30", "enabled": True, "command": {}}]
    }
    entity = VacuulaScheduleEventSwitch(coordinator, "robot", "e")
    assert entity.translation_placeholders == {"event_name": "09:30"}


@pytest.mark.parametrize("loaded", [GEN3], indirect=True)
async def test_robot_missing_from_vacuula_keeps_its_map_entities(hass, loaded):  # noqa: F811
    """Only a successful map read of a listed robot can show a deleted map."""
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    image = entity_id(hass, "image", "map_floor")
    switch = entity_id(hass, "switch", "map_floor_enabled")
    maps = server.client.async_maps.return_value
    original_runs = server.client.async_runs.side_effect

    async def runs(serial):
        """Return empty history for an unlisted robot whose maps still exist."""
        return await original_runs(serial) if serial in server.robots else []

    # Empty lists for a robot the server does not know are the risky answer.
    server.client.async_maps.side_effect = lambda serial: (
        deepcopy(maps) if serial in server.robots else []
    )
    server.client.async_runs.side_effect = runs
    metadata = server.robots.pop("TEST-robot")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    for kept in (image, switch):
        assert registry.async_get(kept) is not None
        assert hass.states.get(kept).state == STATE_UNAVAILABLE
    # The robot returns through a server event, before its maps are read again.
    server.robots["TEST-robot"] = metadata
    await coordinator._async_snapshot(deepcopy(server.robots))
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert registry.async_get(image) is not None
    assert registry.async_get(switch) is not None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(image).state != STATE_UNAVAILABLE
    assert hass.states.get(switch).state == "on"


async def test_unlisted_robot_keeps_its_schedule_event_switches(hass):
    """Preserve schedule switches while the robot is absent from the server list."""
    coordinator = unit_coordinator(hass, GEN4)
    robot = coordinator.robots["robot"]
    robot.schedule = {"events": []}
    robot.listed = False
    entity = VacuulaScheduleEventSwitch(coordinator, "robot", "e")
    with (
        patch("custom_components.vacuula.switch.async_remove_stale_entity") as remove,
        patch.object(entity, "async_write_ha_state"),
    ):
        entity._handle_coordinator_update()
    remove.assert_not_called()
