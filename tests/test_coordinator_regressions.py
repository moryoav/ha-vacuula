"""Exercise persistence and the actual command readback policy."""

from copy import deepcopy

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_coordinator import make_client

from custom_components.vacuula.client import CommandError, UnsupportedCommand
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://test.local:81"},
        options={"keep": "other option"},
    )
    entry.add_to_hass(hass)
    result = VacuulaCoordinator(hass, entry, make_client())
    result.robots = {"TEST-robot": RobotData("TEST-robot", "Neato", GEN3, True)}
    return result


async def test_cleaning_defaults_survive_actual_storage_round_trip(
    coordinator, hass_storage
):
    """Later edits must invalidate HA's cached serialized config-entry fragment."""
    hass = coordinator.hass
    await coordinator.async_set_cleaning_option("TEST-robot", "mode", "turbo")
    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    first_options = coordinator.entry.options
    before = deepcopy(dict(first_options))
    await coordinator.async_set_cleaning_option("TEST-robot", "mode", "eco")
    await coordinator.async_set_cleaning_option("TEST-robot", "navigation_mode", "deep")
    await coordinator.async_set_cleaning_option("TEST-robot", "category", "map")
    await coordinator.async_set_cleaning_option("TEST-robot", "spot_width", 300)
    await coordinator.async_set_cleaning_option("TEST-robot", "spot_height", 250)

    # Saving another entry must not hide a stale per-entry storage fragment.
    another = MockConfigEntry(domain="unrelated", data={})
    another.add_to_hass(hass)
    hass.config_entries.async_update_entry(another, options={"edited": True})
    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    await hass.async_block_till_done()
    saved = next(
        entry
        for entry in hass_storage["core.config_entries"]["data"]["entries"]
        if entry["entry_id"] == coordinator.entry.entry_id
    )
    assert dict(first_options) == before
    assert saved["options"] == {
        "keep": "other option",
        "cleaning_options": {
            "TEST-robot": {
                "mode": "eco",
                "navigation_mode": "deep",
                "category": "map",
                "spot_width": 300,
                "spot_height": 250,
            }
        },
    }
    restored = MockConfigEntry(
        domain=DOMAIN, data=saved["data"], options=saved["options"]
    )
    reloaded = VacuulaCoordinator(hass, restored, make_client())
    assert reloaded.cleaning_options == coordinator.cleaning_options
    coordinator.cleaning_options["TEST-robot"]["mode"] = "turbo"
    assert coordinator.entry.options["cleaning_options"]["TEST-robot"]["mode"] == "eco"


@pytest.mark.parametrize("status", [404, 405, 503])
async def test_optional_robot_error_does_not_disable_later_cleaning_stats(
    coordinator, status
):
    robot = coordinator.robots["TEST-robot"]
    robot.generation = GEN4
    coordinator.client.async_command.side_effect = [
        CommandError("Not available while idle", status=status),
        {"code": 200, "body": {"status": "running", "cleaned_area": 12}},
    ]
    assert await coordinator._async_optional(robot, "cleaning.show") is None
    assert await coordinator._async_optional(robot, "cleaning.show") == {
        "status": "running",
        "cleaned_area": 12,
    }
    assert not coordinator._unsupported


async def test_explicit_unsupported_reply_still_disables_optional_query(coordinator):
    robot = coordinator.robots["TEST-robot"]
    coordinator.client.async_command.side_effect = UnsupportedCommand("Not implemented")
    await coordinator._async_optional(robot, "getLocalStats")
    await coordinator._async_optional(robot, "getLocalStats")
    coordinator.client.async_command.assert_awaited_once()


async def test_read_only_command_has_no_refresh_fanout(coordinator):
    result = await coordinator.async_command(
        "TEST-robot", {"cmd": "getGeneralInfo"}, refresh=False
    )
    assert result == {"result": "ok", "data": {}}
    coordinator.client.async_command.assert_awaited_once_with(
        "TEST-robot", {"cmd": "getGeneralInfo"}
    )
    coordinator.client.async_maps.assert_not_awaited()
    coordinator.client.async_runs.assert_not_awaited()


async def test_normal_action_reads_only_state(coordinator):
    await coordinator.async_command("TEST-robot", {"cmd": "findMe"})
    assert [
        call.args[1] for call in coordinator.client.async_command.await_args_list
    ] == [
        {"cmd": "findMe"},
        {"cmd": "getRobotState"},
    ]
    coordinator.client.async_maps.assert_not_awaited()
    coordinator.client.async_runs.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "generation", "query", "attribute"),
    [
        ({"cmd": "setSchedule"}, GEN3, "getSchedule", "schedule"),
        ({"cmd": "enableSchedule"}, GEN3, "getSchedule", "schedule"),
        ({"cmd": "disableSchedule"}, GEN3, "getSchedule", "schedule"),
        ({"ability": "scheduling.update"}, GEN4, "scheduling.show", "schedule"),
        ({"ability": "scheduling.delete"}, GEN4, "scheduling.show", "schedule"),
    ],
)
async def test_setting_action_reads_only_changed_resource(
    coordinator, command, generation, query, attribute
):
    robot = coordinator.robots["TEST-robot"]
    robot.generation = generation
    selector = "cmd" if generation == GEN3 else "ability"

    async def send(serial, payload):
        if payload == {selector: query}:
            return {"data": {"enabled": True}, "code": 200}
        return {"data": {}, "code": 200}

    coordinator.client.async_command.side_effect = send
    await coordinator.async_command(robot.serial, command)
    assert getattr(robot, attribute) == {"enabled": True, "code": 200}
    assert [
        call.args[1] for call in coordinator.client.async_command.await_args_list
    ] == [
        command,
        {selector: "getRobotState" if generation == GEN3 else "state.show"},
        {selector: query},
    ]
    coordinator.client.async_maps.assert_not_awaited()
    coordinator.client.async_runs.assert_not_awaited()


async def test_map_read_is_targeted_and_failed_read_keeps_cache(coordinator):
    coordinator.client.async_maps.return_value = [{"id": "timestamp", "enabled": True}]
    await coordinator.async_refresh_maps("TEST-robot")
    assert coordinator.robots["TEST-robot"].maps == [
        {"id": "timestamp", "enabled": True}
    ]
    coordinator.client.async_command.assert_not_awaited()
    coordinator.client.async_runs.assert_not_awaited()
    coordinator.client.async_maps.side_effect = CommandError("Unavailable")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_refresh_maps("TEST-robot")
    assert coordinator.robots["TEST-robot"].maps[0]["id"] == "timestamp"


async def test_preference_readback_does_not_fetch_maps_or_history(coordinator):
    robot = coordinator.robots["TEST-robot"]
    preferences = {"robotSounds": True, "anotherSetting": 7}

    async def send(serial, payload):
        if payload.get("cmd") == "getPreferences":
            return {"result": "ok", "data": dict(preferences)}
        if payload.get("cmd") == "setPreferences":
            preferences.update(payload["params"])
        return {"result": "ok", "data": {}}

    coordinator.client.async_command.side_effect = send
    await coordinator.async_command(
        robot.serial, {"cmd": "setPreferences", "params": {"robotSounds": False}}
    )
    assert robot.preferences == {"robotSounds": False, "anotherSetting": 7}
    assert coordinator.client.async_command.await_count == 4
    coordinator.client.async_maps.assert_not_awaited()
    coordinator.client.async_runs.assert_not_awaited()
