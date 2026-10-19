"""Existing Gen4 schedule events are patched from fresh server data atomically."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.services import async_setup_services


@pytest.fixture
def schedule_setup(hass):
    """Use real services and the real coordinator with a changing remote schedule."""
    event = {
        "id": "event1",
        "enabled": True,
        "days": ["mon", "fri"],
        "time": "09:00",
        "extra_event": {"preserved": True},
        "command": {
            "ability": "cleaning.start",
            "auto_localization": True,
            "force_floorplan": True,
            "extra_command": {"preserved": True},
            "runs": [
                {
                    "map": {
                        "rank_id": "map1",
                        "track_id": zone,
                        "nogo_enabled": False,
                        "extra_map": {"zone": zone},
                    },
                    "settings": {
                        "mode": mode,
                        "navigation_mode": "gentle",
                        "extra_settings": {"zone": zone},
                    },
                    "extra_run": {"zone": zone},
                }
                for zone, mode in [("zone1", "turbo"), ("zone2", "eco")]
            ],
        },
    }
    server = {"enabled": True, "events": [event]}
    writes = []

    async def command(serial, payload):
        assert serial == "TEST-robot"
        ability = payload["ability"]
        if ability == "scheduling.show":
            return {"body": deepcopy(server)}
        if ability == "scheduling.update":
            writes.append(deepcopy(payload["event"]))
            server["events"] = [deepcopy(payload["event"])]
        return {"body": {}}

    client = MagicMock()
    client.async_command = AsyncMock(side_effect=command)
    client.async_maps = AsyncMock(return_value=[{"id": "map1", "promoted": True}])
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    robot = RobotData("TEST-robot", "Neato", GEN4, True)
    # Deliberately stale cache. The remote schedule is the only merge source.
    robot.schedule = {"events": [{"id": "event1", "command": {}}]}
    robot.maps = [{"id": "map1", "promoted": True}]
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots = {robot.serial: robot}
    entry.runtime_data = coordinator
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, robot.serial)}
    )
    async_setup_services(hass)
    return coordinator, robot, device.id, server, writes


async def call(hass, setup, **changes):
    return await hass.services.async_call(
        DOMAIN,
        "update_schedule_event",
        {"device_id": setup[2], "event_id": "event1", **changes},
        blocking=True,
        return_response=True,
    )


@pytest.mark.parametrize(
    "changes", [{"enabled": False}, {"time": "12:34"}, {"days": ["sun"]}, {}]
)
async def test_metadata_patch_preserves_all_fresh_cleaning_settings(
    hass, schedule_setup, changes
):
    coordinator, robot, _, server, writes = schedule_setup
    before = deepcopy(server["events"][0])
    caller_data = deepcopy(changes)
    await call(hass, schedule_setup, **changes)
    assert writes == [{**before, **changes}]
    assert robot.schedule["events"] == writes
    assert changes == caller_data
    abilities = [
        item.args[1]["ability"]
        for item in coordinator.client.async_command.await_args_list
    ]
    assert abilities == [
        "scheduling.show",
        "scheduling.update",
        "state.show",
        "scheduling.show",
    ]
    coordinator.client.async_maps.assert_not_awaited()


@pytest.mark.parametrize(
    "changes, setting, value",
    [
        ({"mode": "eco"}, "mode", "eco"),
        ({"navigation": "normal"}, "navigation_mode", "normal"),
    ],
)
async def test_explicit_setting_patches_each_run_and_preserves_other_fields(
    hass, schedule_setup, changes, setting, value
):
    before = deepcopy(schedule_setup[3]["events"][0])
    expected = deepcopy(before)
    for run in expected["command"]["runs"]:
        run["settings"][setting] = value
    await call(hass, schedule_setup, **changes)
    assert schedule_setup[4] == [expected]
    assert before["command"]["runs"][0]["settings"]["navigation_mode"] == "gentle"


async def test_nogo_patch_preserves_zone_ids_and_individual_settings(
    hass, schedule_setup
):
    expected = deepcopy(schedule_setup[3]["events"][0])
    for run in expected["command"]["runs"]:
        run["map"]["nogo_enabled"] = True
    await call(hass, schedule_setup, nogo_enabled=True)
    assert schedule_setup[4] == [expected]


@pytest.mark.parametrize(
    "selection",
    [{"map_id": "map1"}, {"cleaning_type": "auto"}, {"cleaning_type": "map"}],
)
async def test_explicit_map_selection_preserves_zones_and_run_options(
    hass, schedule_setup, selection
):
    coordinator, _, _, server, writes = schedule_setup
    expected = deepcopy(server["events"][0])
    await call(hass, schedule_setup, **selection)
    assert writes == [expected]


@pytest.mark.parametrize(
    "selection",
    [{"map_id": "map2"}, {"cleaning_type": "auto"}, {"cleaning_type": "map"}],
)
async def test_other_floorplan_requires_explicit_zone_choice(
    hass, schedule_setup, selection
):
    coordinator, _, _, _, writes = schedule_setup
    coordinator.client.async_maps.return_value = [{"id": "map2", "promoted": True}]
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, **selection)
    assert error.value.translation_key == "schedule_zones_required"
    assert writes == []


@pytest.mark.parametrize("zones", [[], ["newzone"]])
async def test_other_floorplan_with_explicit_zones_changes_target(
    hass, schedule_setup, zones
):
    _, _, _, server, writes = schedule_setup
    expected = deepcopy(server["events"][0])
    run = expected["command"]["runs"][0]
    expected["command"]["runs"] = [
        {
            **run,
            "map": {
                **run["map"],
                "rank_id": "map2",
                "track_id": zones[0] if zones else None,
            },
        }
    ]
    await call(hass, schedule_setup, map_id="map2", zone_ids=zones)
    assert writes == [expected]


@pytest.mark.parametrize("zones", [[], ["zone2"], ["zone2", "zone1"], ["newzone"]])
async def test_explicit_zones_replace_selection_without_erasing_run_options(
    hass, schedule_setup, zones
):
    before = deepcopy(schedule_setup[3]["events"][0])
    await call(hass, schedule_setup, zone_ids=zones)
    runs = schedule_setup[4][0]["command"]["runs"]
    assert [run["map"]["track_id"] for run in runs] == (zones or [None])
    for zone, run in zip(zones or [None], runs, strict=True):
        source = next(
            (
                item
                for item in before["command"]["runs"]
                if item["map"]["track_id"] == zone
            ),
            before["command"]["runs"][0],
        )
        assert run == {**source, "map": {**source["map"], "track_id": zone}}


@pytest.mark.parametrize(
    "target", [{"cleaning_type": "house"}, {"cleaning_type": "auto"}]
)
async def test_explicit_whole_home_target_removes_zone_runs(
    hass, schedule_setup, target
):
    coordinator, _, _, server, writes = schedule_setup
    coordinator.client.async_maps.return_value = []
    expected = deepcopy(server["events"][0])
    expected["command"]["runs"] = [{**expected["command"]["runs"][0], "map": None}]
    await call(hass, schedule_setup, **target)
    assert writes == [expected]


@pytest.mark.parametrize("field", ["enabled", "days", "time"])
async def test_new_event_requires_its_clock_fields(hass, schedule_setup, field):
    payload = {"enabled": True, "days": ["sun"], "time": "12:00"}
    payload.pop(field)
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            DOMAIN,
            "update_schedule_event",
            {"device_id": schedule_setup[2], **payload},
            blocking=True,
        )
    assert error.value.translation_key == "invalid_schedule_event"
    schedule_setup[0].client.async_command.assert_not_awaited()


@pytest.mark.parametrize(
    "events", [None, [], [{"id": "other"}], [{"id": "event1"}, {"id": "event1"}]]
)
async def test_missing_or_ambiguous_event_never_creates_or_overwrites(
    hass, schedule_setup, events
):
    coordinator, _, _, server, writes = schedule_setup
    server["events"] = events
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, time="12:34")
    assert error.value.translation_key == "action_unavailable"
    assert writes == []
    coordinator.client.async_command.assert_awaited_once_with(
        "TEST-robot", {"ability": "scheduling.show"}
    )


@pytest.mark.parametrize("failure", ["read", "write", "offline"])
async def test_schedule_failure_does_not_retry_or_send_stale_values(
    hass, schedule_setup, failure
):
    coordinator, robot, _, _, writes = schedule_setup
    if failure == "offline":
        robot.connected = False
    elif failure == "read":
        coordinator.client.async_command.side_effect = CannotConnect("offline")
    else:
        original = coordinator.client.async_command.side_effect

        async def fail_write(serial, payload):
            if payload["ability"] == "scheduling.update":
                raise CannotConnect("timeout")
            return await original(serial, payload)

        coordinator.client.async_command.side_effect = fail_write
    with pytest.raises(HomeAssistantError):
        await call(hass, schedule_setup, time="12:34")
    assert writes == []
    assert (
        coordinator.client.async_command.await_count
        == {"offline": 0, "read": 1, "write": 2}[failure]
    )


async def test_concurrent_updates_merge_under_one_robot_lock(hass, schedule_setup):
    coordinator, _, _, server, writes = schedule_setup
    first_read = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()
    original = coordinator.client.async_command.side_effect
    reads = 0

    async def hold_first_read(serial, payload):
        nonlocal reads
        if payload["ability"] == "scheduling.show":
            reads += 1
            if reads == 1:
                first_read.set()
                await release.wait()
        return await original(serial, payload)

    async def second_call():
        second_started.set()
        await call(hass, schedule_setup, mode="eco")

    coordinator.client.async_command.side_effect = hold_first_read
    tasks = []
    try:
        async with asyncio.timeout(5):
            tasks.append(
                hass.async_create_task(call(hass, schedule_setup, time="12:34"))
            )
            await first_read.wait()
            tasks.append(hass.async_create_task(second_call()))
            await second_started.wait()
            assert reads == 1 and writes == []
            release.set()
            await asyncio.gather(*tasks)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(writes) == 2
    assert writes[1]["time"] == "12:34"
    assert all(
        run["settings"]["mode"] == "eco"
        for run in server["events"][0]["command"]["runs"]
    )


@pytest.mark.parametrize(
    "command",
    [
        None,
        {"ability": "other.action"},
        {"ability": "cleaning.start", "runs": None},
        {"ability": "cleaning.start", "runs": []},
        {"ability": "cleaning.start", "runs": [None]},
        {"ability": "cleaning.start", "runs": [{"settings": None}]},
        {"ability": "cleaning.start", "runs": [{"settings": {}, "map": "bad"}]},
    ],
)
async def test_unrecognized_cleaning_commands_are_never_rebuilt(
    hass, schedule_setup, command
):
    _, _, _, server, writes = schedule_setup
    server["events"][0]["command"] = command
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, mode="eco")
    assert error.value.translation_key == "action_unavailable"
    assert writes == []


@pytest.mark.parametrize("command", [None, {"ability": "other.action", "runs": None}])
async def test_metadata_patch_preserves_unrecognized_command(
    hass, schedule_setup, command
):
    _, _, _, server, writes = schedule_setup
    server["events"][0]["command"] = command
    expected = {**deepcopy(server["events"][0]), "enabled": False}
    await call(hass, schedule_setup, enabled=False)
    assert writes == [expected]


@pytest.mark.parametrize("changes", [{"map_id": "map1"}, {"zone_ids": ["zone1"]}])
async def test_explicit_whole_home_with_map_or_zones_is_rejected(
    hass, schedule_setup, changes
):
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, cleaning_type="house", **changes)
    assert error.value.translation_key == "invalid_option"
    assert schedule_setup[4] == []


async def test_explicit_empty_zone_list_preserves_whole_home_target(
    hass, schedule_setup
):
    _, _, _, server, writes = schedule_setup
    event = server["events"][0]
    event["command"]["runs"] = [{**event["command"]["runs"][0], "map": None}]
    expected = deepcopy(event)
    await call(hass, schedule_setup, zone_ids=[], nogo_enabled=True)
    assert writes == [expected]


@pytest.mark.parametrize("targets", [[None], ["map1", "map2"], [123]])
async def test_zones_need_one_identified_floorplan(hass, schedule_setup, targets):
    _, _, _, server, writes = schedule_setup
    event = server["events"][0]
    template = deepcopy(event["command"]["runs"][0])
    event["command"]["runs"] = [
        {
            **deepcopy(template),
            "map": {**template["map"], "rank_id": target}
            if target is not None
            else None,
        }
        for target in targets
    ]
    with pytest.raises(ServiceValidationError) as error:
        await call(hass, schedule_setup, zone_ids=["zone1"])
    assert error.value.translation_key == "map_required"
    assert writes == []


async def test_removed_robot_is_rejected_without_read_or_write(schedule_setup):
    coordinator, robot, _, _, _ = schedule_setup
    coordinator.robots.clear()
    with pytest.raises(HomeAssistantError) as error:
        await coordinator.async_update_schedule_event(
            robot.serial, "event1", lambda event: event
        )
    assert error.value.translation_key == "offline"
    coordinator.client.async_command.assert_not_awaited()


async def test_merge_never_mutates_response_objects_from_the_client(
    hass, schedule_setup
):
    coordinator, _, _, server, writes = schedule_setup
    response = {"body": deepcopy(server)}
    original_response = deepcopy(response)
    original = coordinator.client.async_command.side_effect

    async def shared_response(serial, payload):
        if payload["ability"] == "scheduling.show":
            return response
        return await original(serial, payload)

    coordinator.client.async_command.side_effect = shared_response
    await call(hass, schedule_setup, mode="eco", days=["sun"], zone_ids=["zone2"])
    assert response == original_response
    assert writes[0]["days"] == ["sun"]
