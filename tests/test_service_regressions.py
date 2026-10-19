"""Regression coverage for local schedules, map changes and read-only actions."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.services import async_setup_services


@pytest.fixture
def action_setup(hass):
    """Register real action schemas around a robot with observable I/O."""
    coordinator = MagicMock()
    coordinator.async_command = AsyncMock(return_value={"data": {"value": 1}})
    coordinator.async_set_map_boundaries = AsyncMock(return_value={"saved": True})
    coordinator.async_refresh_maps = AsyncMock()
    coordinator.async_refresh_robot = AsyncMock()
    coordinator.client.async_map = AsyncMock(return_value={"width": 1})
    robot = RobotData("TEST-robot", "Neato", GEN3, True)
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    entry.mock_state(hass, ConfigEntryState.LOADED)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, robot.serial)}
    )
    coordinator.robots = {robot.serial: robot}
    async_setup_services(hass)
    return coordinator, robot, device.id


async def _call(hass, setup, action, **data):
    return await hass.services.async_call(
        DOMAIN,
        action,
        {"device_id": setup[2], **data},
        blocking=True,
        return_response=True,
    )


@pytest.mark.parametrize(
    ("timezone", "now", "day", "time", "utc_day", "utc_time"),
    [
        ("Asia/Jerusalem", "2026-10-19T09:00:00Z", 1, "09:00", 1, "06:00"),
        ("Asia/Jerusalem", "2026-01-19T09:00:00Z", 1, "09:00", 1, "07:00"),
        ("Asia/Kathmandu", "2026-10-19T09:00:00Z", 0, "00:30", 6, "18:45"),
        ("America/New_York", "2026-10-19T09:00:00Z", 6, "23:45", 0, "03:45"),
        ("UTC", "2026-10-19T09:00:00Z", 0, "00:00", 0, "00:00"),
    ],
)
async def test_local_schedule_uses_ha_offset_and_wraps_week(
    hass, action_setup, freezer, timezone, now, day, time, utc_day, utc_time
):
    """Timezone conversion preserves weekly instants, including midnight edges."""
    freezer.move_to(now)
    hass.config.time_zone = timezone
    event = {"mode": 2, "day": day, "startTime": time, "boundaryId": "zone1"}
    await _call(hass, action_setup, "set_schedule", events=[event])
    assert action_setup[0].async_command.await_args.args[1] == {
        "cmd": "setSchedule",
        "params": {
            "events": [
                {**event, "day": utc_day, "startTime": utc_time},
            ]
        },
    }
    assert event["day"] == day
    assert event["startTime"] == time


async def test_raw_utc_schedule_can_be_round_tripped(hass, action_setup):
    """An explicit UTC basis preserves schedules returned by get_data."""
    hass.config.time_zone = "Asia/Jerusalem"
    events = [{"mode": 1, "day": 0, "startTime": "09:00"}]
    await _call(hass, action_setup, "set_schedule", events=events, time_basis="utc")
    assert (
        action_setup[0].async_command.await_args.args[1]["params"]["events"] == events
    )


@pytest.mark.parametrize("resource", ["state", "schedule", "boundaries"])
async def test_get_data_does_not_request_followup_reads(hass, action_setup, resource):
    """Read-only action requests exactly its command without full robot refresh."""
    coordinator, _, _ = action_setup
    await _call(hass, action_setup, "get_data", resource=resource, map_id="map1")
    assert coordinator.async_command.await_count == 1
    assert coordinator.async_command.await_args.kwargs == {"refresh": False}
    coordinator.async_refresh_robot.assert_not_awaited()
    coordinator.async_refresh_maps.assert_not_awaited()


async def test_gen3_boundary_write_has_no_duplicate_refresh(hass, action_setup):
    coordinator, robot, _ = action_setup
    await _call(hass, action_setup, "set_boundaries", map_id="map1", boundaries=[])
    coordinator.async_command.assert_awaited_once_with(
        robot.serial,
        {"cmd": "setMapBoundaries", "params": {"mapId": "map1", "boundaries": []}},
    )
    coordinator.async_refresh_robot.assert_not_awaited()
    coordinator.async_refresh_maps.assert_not_awaited()


async def test_gen4_boundary_write_uses_atomic_save_and_promote(hass, action_setup):
    """The coordinator owns the complete transaction, then only maps are read."""
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    result = await _call(
        hass, action_setup, "set_boundaries", map_id="map1", boundaries=[]
    )
    assert result == {"response": {"saved": True}}
    coordinator.async_set_map_boundaries.assert_awaited_once_with(
        robot.serial, "map1", []
    )
    coordinator.async_refresh_maps.assert_awaited_once_with(robot.serial)
    coordinator.async_refresh_robot.assert_not_awaited()
    coordinator.async_command.assert_not_awaited()


async def test_gen4_boundary_failure_is_reported_without_success_refresh(
    hass, action_setup
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    coordinator.async_set_map_boundaries.side_effect = CannotConnect("rejected")
    with pytest.raises(HomeAssistantError):
        await _call(hass, action_setup, "set_boundaries", map_id="map1", boundaries=[])
    coordinator.async_refresh_maps.assert_not_awaited()


async def test_clean_auto_reads_maps_before_using_saved_map(hass, action_setup):
    """A saved Gen3 map changes the default category, preserving no-go lines."""
    coordinator, robot, _ = action_setup
    robot.state = {"details": {"isDocked": True}}

    async def get_maps(serial):
        robot.maps = [{"id": "2026-10-19T10:00:00Z", "enabled": True}]
        robot.capabilities["maps_available"] = True

    coordinator.async_refresh_maps.side_effect = get_maps
    await _call(hass, action_setup, "clean")
    coordinator.async_refresh_maps.assert_awaited_once_with(robot.serial)
    assert coordinator.async_command.await_args.args[1]["params"]["category"] == 4


async def test_explicit_house_is_mapless_and_rejects_conflicting_map(
    hass, action_setup
):
    coordinator, robot, _ = action_setup
    robot.maps = [{"id": "map1", "enabled": True}]
    await _call(hass, action_setup, "clean", cleaning_type="house")
    assert coordinator.async_command.await_args.args[1]["params"]["category"] == 2
    coordinator.async_refresh_maps.assert_not_awaited()
    coordinator.async_command.reset_mock()
    with pytest.raises(ServiceValidationError):
        await _call(hass, action_setup, "clean", cleaning_type="house", map_id="map1")
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("nogo_enabled", [True, False])
async def test_gen4_auto_clean_uses_promoted_map(hass, action_setup, nogo_enabled):
    """The advanced action shares the safe mapped default and explicit controls."""
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    robot.state = {"details": {"is_docked": True}}
    robot.maps = [{"id": "map1", "promoted": True}]
    await _call(hass, action_setup, "clean", nogo_enabled=nogo_enabled)
    command = coordinator.async_command.await_args.args[1]
    assert command["runs"][0]["map"] == {
        "rank_id": "map1",
        "track_id": None,
        "nogo_enabled": nogo_enabled,
    }


async def test_clean_requires_map_when_explicitly_selected(hass, action_setup):
    coordinator = action_setup[0]
    with pytest.raises(ServiceValidationError):
        await _call(hass, action_setup, "clean", cleaning_type="map")
    coordinator.async_command.assert_not_awaited()
