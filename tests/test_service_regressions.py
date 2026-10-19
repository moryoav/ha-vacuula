"""Regression coverage for local schedules, map changes and read-only actions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
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
    """The coordinator owns the transaction and its map readback."""
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    result = await _call(
        hass, action_setup, "set_boundaries", map_id="map1", boundaries=[]
    )
    assert result == {"response": {"saved": True}}
    coordinator.async_set_map_boundaries.assert_awaited_once_with(
        robot.serial, "map1", []
    )
    coordinator.async_refresh_maps.assert_not_awaited()
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


@pytest.mark.parametrize("docked", [True, False, None])
@pytest.mark.parametrize("nogo_enabled", [True, False])
async def test_gen4_schedule_defaults_to_fresh_promoted_map(
    hass, action_setup, docked, nogo_enabled
):
    """A future scheduled run uses the floorplan even when saved off its dock."""
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    robot.state = {"details": {"is_docked": docked}}
    robot.maps = [{"id": "stale", "promoted": False}]

    async def get_maps(serial):
        assert serial == robot.serial
        robot.maps = [{"id": "current", "promoted": True}]

    coordinator.async_refresh_maps.side_effect = get_maps
    await _call(
        hass,
        action_setup,
        "update_schedule_event",
        enabled=True,
        days=["mon"],
        time="09:00",
        nogo_enabled=nogo_enabled,
    )
    coordinator.validate_map_recovery.assert_called_once_with(robot.serial)
    coordinator.async_refresh_maps.assert_awaited_once_with(robot.serial)
    assert coordinator.async_command.await_args.args[1] == {
        "ability": "scheduling.update",
        "event": {
            "enabled": True,
            "days": ["mon"],
            "time": "09:00",
            "command": {
                "ability": "cleaning.start",
                "runs": [
                    {
                        "map": {
                            "rank_id": "current",
                            "track_id": None,
                            "nogo_enabled": nogo_enabled,
                        },
                        "settings": {"mode": "eco", "navigation_mode": "normal"},
                    }
                ],
                "auto_localization": False,
                "force_floorplan": False,
            },
        },
    }


@pytest.mark.parametrize("maps", [[], [{"id": "map1", "promoted": False}]])
async def test_gen4_schedule_without_promoted_map_uses_whole_home(
    hass, action_setup, maps
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    robot.maps = maps
    await _call(
        hass,
        action_setup,
        "update_schedule_event",
        enabled=True,
        days=["sun"],
        time="09:00",
    )
    assert (
        coordinator.async_command.await_args.args[1]["event"]["command"]["runs"][0][
            "map"
        ]
        is None
    )


async def test_gen4_schedule_explicit_whole_home_ignores_promoted_map(
    hass, action_setup
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    robot.maps = [{"id": "map1", "promoted": True}]
    await _call(
        hass,
        action_setup,
        "update_schedule_event",
        enabled=True,
        days=["sun"],
        time="09:00",
        cleaning_type="house",
    )
    assert (
        coordinator.async_command.await_args.args[1]["event"]["command"]["runs"][0][
            "map"
        ]
        is None
    )
    coordinator.async_refresh_maps.assert_not_awaited()
    coordinator.validate_map_recovery.assert_not_called()


async def test_gen4_schedule_explicit_zone_keeps_target_and_settings(
    hass, action_setup
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    robot.state = {"details": {"is_docked": False}}
    await _call(
        hass,
        action_setup,
        "update_schedule_event",
        enabled=True,
        days=["sun"],
        time="09:00",
        map_id="map2",
        zone_ids=["zone1", "zone2"],
        mode="turbo",
        navigation="gentle",
    )
    runs = coordinator.async_command.await_args.args[1]["event"]["command"]["runs"]
    assert [run["map"]["track_id"] for run in runs] == ["zone1", "zone2"]
    assert all(run["map"]["rank_id"] == "map2" for run in runs)
    assert all(run["map"]["nogo_enabled"] is True for run in runs)
    assert all(
        run["settings"] == {"mode": "turbo", "navigation_mode": "gentle"}
        for run in runs
    )
    coordinator.async_refresh_maps.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [{"cleaning_type": "map"}, {"cleaning_type": "house", "map_id": "map1"}],
)
async def test_gen4_schedule_invalid_map_selection_does_not_write(
    hass, action_setup, payload
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            action_setup,
            "update_schedule_event",
            enabled=True,
            days=["sun"],
            time="09:00",
            **payload,
        )
    coordinator.async_command.assert_not_awaited()


async def test_gen4_schedule_map_refresh_failure_does_not_write(hass, action_setup):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    coordinator.async_refresh_maps.side_effect = HomeAssistantError("unreachable")
    with pytest.raises(HomeAssistantError):
        await _call(
            hass,
            action_setup,
            "update_schedule_event",
            enabled=True,
            days=["sun"],
            time="09:00",
        )
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("action", ["clean", "update_schedule_event"])
async def test_clean_and_schedule_require_known_floorplan_recovery(
    hass, action_setup, action
):
    coordinator, robot, _ = action_setup
    robot.generation = GEN4
    coordinator.validate_map_recovery.side_effect = ServiceValidationError(
        translation_domain=DOMAIN, translation_key="map_promotion_required"
    )
    payload = (
        {"enabled": True, "days": ["sun"], "time": "09:00"}
        if action == "update_schedule_event"
        else {}
    )
    with pytest.raises(ServiceValidationError) as error:
        await _call(hass, action_setup, action, **payload)
    assert error.value.translation_key == "map_promotion_required"
    coordinator.async_refresh_maps.assert_not_awaited()
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("time_basis", ["utc", "local"])
@pytest.mark.parametrize("explicit_id", [False, True])
async def test_returned_gen3_boundary_id_is_preserved_on_schedule_write(
    hass, action_setup, freezer, time_basis, explicit_id
):
    """Normalize an identified response zone, without changing caller data."""
    freezer.move_to("2026-10-19T09:00:00Z")
    hass.config.time_zone = "Asia/Jerusalem"
    coordinator = action_setup[0]
    event = {
        "mode": 1,
        "day": 0,
        "startTime": "09:00",
        "boundary": {"id": "zone1", "name": "Living room"},
    }
    if explicit_id:
        event["boundaryId"] = "zone1"
    coordinator.async_command.return_value = {"data": {"events": [event]}}
    response = await _call(hass, action_setup, "get_data", resource="schedule")
    await _call(
        hass,
        action_setup,
        "set_schedule",
        events=response["data"]["events"],
        time_basis=time_basis,
    )
    assert coordinator.async_command.await_args.args[1] == {
        "cmd": "setSchedule",
        "params": {
            "events": [
                {
                    "mode": 1,
                    "day": 0,
                    "startTime": "09:00" if time_basis == "utc" else "06:00",
                    "boundaryId": "zone1",
                }
            ]
        },
    }
    assert "boundary" in event
    assert event["startTime"] == "09:00"


async def test_gen3_null_boundary_is_a_whole_home_schedule(hass, action_setup):
    coordinator = action_setup[0]
    await _call(
        hass,
        action_setup,
        "set_schedule",
        events=[{"mode": 1, "day": 0, "startTime": "09:00", "boundary": None}],
        time_basis="utc",
    )
    assert coordinator.async_command.await_args.args[1]["params"]["events"] == [
        {"mode": 1, "day": 0, "startTime": "09:00"}
    ]


@pytest.mark.parametrize(
    "boundary, explicit_id",
    [
        ({"name": "Living room"}, None),
        ({"id": "../invalid"}, None),
        ({"id": "zone1"}, "zone2"),
    ],
)
async def test_gen3_unresolved_or_conflicting_schedule_zones_never_write(
    hass, action_setup, boundary, explicit_id
):
    coordinator = action_setup[0]
    event = {"mode": 1, "day": 0, "startTime": "09:00", "boundary": boundary}
    if explicit_id:
        event["boundaryId"] = explicit_id
    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            action_setup,
            "set_schedule",
            events=[{"mode": 1, "day": 1, "startTime": "09:00"}, event],
            time_basis="utc",
        )
    assert error.value.translation_key == "invalid_schedule_boundary"
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("action", ["clean", "update_schedule_event"])
@pytest.mark.parametrize("cleaning_type", ["auto", "map", "house"])
async def test_queued_clean_and_schedule_recheck_map_recovery_under_lock(
    hass, action_setup, action, cleaning_type
):
    """A marker created while a cleaning command waits cannot be bypassed."""
    _, robot, device_id = action_setup
    device = dr.async_get(hass).async_get(device_id)
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    client = MagicMock()
    client.async_command = AsyncMock(return_value={"body": {}})
    coordinator = VacuulaCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    coordinator.robots = {robot.serial: robot}
    robot.generation = GEN4
    robot.state = {"details": {"is_docked": True}}
    robot.maps = [{"id": "map1", "promoted": True}]
    maps_read = asyncio.Event()

    async def refresh_maps(serial):
        assert serial == robot.serial
        maps_read.set()

    coordinator.async_refresh_maps = AsyncMock(side_effect=refresh_maps)
    payload = (
        {"enabled": True, "days": ["sun"], "time": "09:00"}
        if action == "update_schedule_event"
        else {}
    )
    service_started = asyncio.Event()

    async def call_action():
        service_started.set()
        return await _call(
            hass, action_setup, action, cleaning_type=cleaning_type, **payload
        )

    task = None
    try:
        async with asyncio.timeout(5):
            async with coordinator._lock(robot.serial):
                task = hass.async_create_task(call_action())
                await service_started.wait()
                if cleaning_type != "house":
                    await maps_read.wait()
                assert not task.done()
                client.async_command.assert_not_awaited()
                coordinator._set_pending_promotion(robot.serial, "map1")
            if cleaning_type == "house":
                await task
                sent = client.async_command.await_args_list[0].args[1]
                if action == "update_schedule_event":
                    sent = sent["event"]["command"]
                assert sent["runs"][0]["map"] is None
            else:
                with pytest.raises(ServiceValidationError) as error:
                    await task
                assert error.value.translation_key == "map_promotion_required"
                client.async_command.assert_not_awaited()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
