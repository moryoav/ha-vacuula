"""Load every platform through Home Assistant and exercise state and controls."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.data import RobotData

READ_COMMANDS = {
    "getRobotState",
    "getGeneralInfo",
    "getLocalStats",
    "getPreferences",
    "getSchedule",
    "state.show",
    "cleaning.show",
    "settings.cleaning_options",
    "settings.robot_language",
    "scheduling.show",
    "info.robot",
}


def state3():
    """Return Gen3 docked telemetry with cleaning capabilities and ready controls."""
    return {
        "state": 1,
        "action": 0,
        "error": None,
        "alert": None,
        "details": {
            "charge": 99,
            "isCharging": True,
            "isDocked": True,
            "dockHasBeenSeen": True,
            "isScheduleEnabled": True,
        },
        "cleaning": {
            "mode": 1,
            "navigationMode": 2,
            "category": 2,
            "modifier": 1,
            "spotHeight": 200,
            "spotWidth": 200,
        },
        "availableCommands": {
            "start": True,
            "stop": True,
            "pause": True,
            "resume": False,
            "goToBase": True,
        },
        "availableServices": {
            "findMe": "basic-1",
            "spotCleaning": "basic-1",
            "maps": "advanced-1",
            "logCopy": "basic-1",
        },
        "meta": {"modelName": "BotVacD5Connected", "firmware": "4.6.0"},
    }


def state4():
    """Return Gen4 docked telemetry with autonomy and cleaning-center details."""
    return {
        "state": "idle",
        "action": "invalid",
        "errors": [],
        "details": {
            "charge": 82,
            "is_charging": False,
            "is_docked": True,
            "is_quickboost": False,
            "base_type": "standard",
        },
        "available_commands": {
            "start": True,
            "cancel": True,
            "pause": True,
            "resume": False,
            "return_to_base": True,
        },
        "autonomy_states": {"started_on_base": True, "docking": 2},
        "cleaning_center": {
            "base_error": None,
            "bag_status": "bag_ok",
            "is_extracting": False,
        },
    }


@pytest.fixture
async def loaded(hass):
    """Load both generations with stateful mock settings and cleanly unload them."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="test-server", data={"url": "http://test.local:81"}
    )
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_server_info = AsyncMock(return_value={"version": "1.0"})
    client.async_robots = AsyncMock(
        return_value={
            "robot3": {"type": GEN3, "name": "Neato3", "connected": True},
            "robot4": {
                "type": GEN4,
                "name": "Neato4",
                "connected": True,
                "label_serial": "ABCDN20001",
            },
        }
    )
    client.async_maps = AsyncMock(return_value=[])
    client.async_runs = AsyncMock(return_value=[])
    states = {"robot3": state3(), "robot4": state4()}
    preferences = {
        "robotSounds": True,
        "dirtbinAlertReminderInterval": 60,
        "filterChangeReminderInterval": 43200,
        "brushChangeReminderInterval": 172800,
    }
    gen4_preferences = {
        "force_floorplan": False,
        "language": "en",
        "available_languages": ["en", "de"],
    }

    async def command(serial, payload):
        """Serve generation-specific reads and persist settings for confirmation."""
        name = payload.get("cmd") or payload.get("ability")
        body = {}
        if name in ("getRobotState", "state.show"):
            body = deepcopy(states[serial])
        elif name == "getGeneralInfo":
            body = {
                "model": "D5",
                "firmware": "4.6.0",
                "productNumber": "945-0005",
                "battery": {
                    "level": 99,
                    "timeToEmpty": 65535,
                    "timeToFullCharge": 13,
                    "totalCharges": 12,
                    "manufacturingDate": "2024-2-28",
                    "authorizationStatus": 1,
                    "vendor": "ZH",
                },
            }
        elif name == "getPreferences":
            body = deepcopy(preferences)
        elif name in ("getSchedule", "scheduling.show"):
            body = {"enabled": True, "events": []}
        elif name == "getLocalStats":
            body = {"cleanings": 9}
        elif name == "settings.cleaning_options":
            body = {"force_floorplan": gen4_preferences["force_floorplan"]}
        elif name == "settings.robot_language":
            body = {
                key: gen4_preferences[key]
                for key in ("language", "available_languages")
            }
        elif name == "info.robot":
            body = {"model": "D10", "firmware": "1.7.0", "label_serial": "ABCDN20001"}
        elif name == "setPreferences":
            preferences.update(payload["params"])
        elif name == "settings.set_cleaning_options":
            gen4_preferences["force_floorplan"] = payload["force_floorplan"]
        elif name == "settings.set_robot_language":
            gen4_preferences["language"] = payload["language"]
        elif name == "enableSchedule":
            states[serial]["details"]["isScheduleEnabled"] = True
        elif name == "disableSchedule":
            states[serial]["details"]["isScheduleEnabled"] = False
        if serial == "robot3":
            return (
                {"result": "ok", **body, "data": {}}
                if name == "getRobotState"
                else {"result": "ok", "data": body}
            )
        return {"code": 200, "body": body}

    client.async_command = AsyncMock(side_effect=command)
    with (
        patch("custom_components.vacuula.VacuulaClient", return_value=client),
        patch(
            "custom_components.vacuula.coordinator.VacuulaCoordinator.async_start",
            new=AsyncMock(),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, client, states
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def eid(hass, platform, serial, key):
    """Resolve an entity by platform, serial and key or fail with its identity."""
    result = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{serial}_{key}")
    assert result is not None, (platform, serial, key)
    return result


async def test_all_platforms_load_without_control_commands(hass, loaded):
    """Load all platforms from reads alone and expose only supported telemetry."""
    entry, client, _states = loaded
    assert all(
        (payload.get("cmd") or payload.get("ability")) in READ_COMMANDS
        for _serial, payload in (
            record.args for record in client.async_command.call_args_list
        )
    )
    assert hass.states.get(eid(hass, "vacuum", "robot3", "vacuum")).state == "docked"
    assert hass.states.get(eid(hass, "sensor", "robot3", "battery")).state == "99"
    assert (
        hass.states.get(eid(hass, "binary_sensor", "robot4", "charging")).state == "off"
    )
    assert hass.states.get(eid(hass, "switch", "robot3", "robot_sounds")).state == "on"
    assert (
        hass.states.get(eid(hass, "switch", "robot4", "force_floorplan")).state == "off"
    )
    assert (
        float(hass.states.get(eid(hass, "number", "robot3", "filter_reminder")).state)
        == 30
    )
    assert (
        hass.states.get(eid(hass, "select", "robot4", "robot_language")).state == "en"
    )
    assert (
        hass.states.get(eid(hass, "button", "robot3", "button_start")).state
        == "unknown"
    )
    assert (
        er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "robot3_time_to_empty")
        is None
    )
    assert entry.runtime_data.cleaning_options == {}


async def test_standard_actions_and_persistent_defaults(hass, loaded):
    """Persist local cleaning defaults and route native vacuum commands exactly."""
    entry, client, states = loaded
    vacuum3 = eid(hass, "vacuum", "robot3", "vacuum")
    for platform, key, data in (
        ("select", "cleaning_mode", {"option": "turbo"}),
        ("select", "navigation_mode", {"option": "deep"}),
        ("number", "spot_height", {"value": 150}),
    ):
        await hass.services.async_call(
            platform,
            "select_option" if platform == "select" else "set_value",
            {"entity_id": eid(hass, platform, "robot3", key), **data},
            blocking=True,
        )
    assert entry.options["cleaning_options"]["robot3"] == {
        "mode": "turbo",
        "navigation_mode": "deep",
        "spot_height": 150,
    }
    assert all(
        (payload.get("cmd") or payload.get("ability")) in READ_COMMANDS
        for _serial, payload in (
            record.args for record in client.async_command.call_args_list
        )
    )
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum3}, blocking=True
    )
    assert (
        call(
            "robot3",
            {
                "cmd": "startCleaning",
                "params": {"category": 2, "mode": 2, "navigationMode": 3},
            },
        )
        in client.async_command.call_args_list
    )
    await hass.services.async_call(
        "vacuum", "clean_spot", {"entity_id": vacuum3}, blocking=True
    )
    assert (
        call(
            "robot3",
            {
                "cmd": "startCleaning",
                "params": {
                    "category": 3,
                    "mode": 2,
                    "navigationMode": 3,
                    "spotHeight": 150,
                    "spotWidth": 200,
                },
            },
        )
        in client.async_command.call_args_list
    )
    for action, expected in (
        ("stop", "stopCleaning"),
        ("pause", "pauseCleaning"),
        ("return_to_base", "sendToBase"),
        ("locate", "findMe"),
    ):
        await hass.services.async_call(
            "vacuum", action, {"entity_id": vacuum3}, blocking=True
        )
        assert call("robot3", {"cmd": expected}) in client.async_command.call_args_list
    states["robot3"]["availableCommands"]["resume"] = True
    await entry.runtime_data.async_refresh_robot("robot3")
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum3}, blocking=True
    )
    assert (
        call("robot3", {"cmd": "resumeCleaning"}) in client.async_command.call_args_list
    )
    await hass.services.async_call(
        "vacuum",
        "start",
        {"entity_id": eid(hass, "vacuum", "robot4", "vacuum")},
        blocking=True,
    )
    assert (
        call(
            "robot4",
            {
                "ability": "cleaning.start",
                "runs": [
                    {
                        "map": None,
                        "settings": {"mode": "eco", "navigation_mode": "normal"},
                    }
                ],
            },
        )
        in client.async_command.call_args_list
    )


async def test_robot_settings_confirmed_by_readback(hass, loaded):
    """Confirm switch, reminder and language changes through their native services."""
    _entry, client, _states = loaded
    for serial, key in (("robot3", "robot_sounds"), ("robot3", "schedule")):
        entity = eid(hass, "switch", serial, key)
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": entity}, blocking=True
        )
        assert hass.states.get(entity).state == "off"
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity}, blocking=True
        )
        assert hass.states.get(entity).state == "on"
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": eid(hass, "number", "robot3", "filter_reminder"), "value": 45},
        blocking=True,
    )
    assert any(
        payload.get("params", {}).get("filterChangeReminderInterval") == 64800
        for _serial, payload in (
            record.args for record in client.async_command.call_args_list
        )
    )
    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": eid(hass, "switch", "robot4", "force_floorplan")},
        blocking=True,
    )
    assert (
        call(
            "robot4",
            {"ability": "settings.set_cleaning_options", "force_floorplan": True},
        )
        in client.async_command.call_args_list
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "robot4", "robot_language"), "option": "de"},
        blocking=True,
    )
    assert (
        hass.states.get(eid(hass, "select", "robot4", "robot_language")).state == "de"
    )


async def test_push_offline_readiness_and_new_robot(hass, loaded):
    """Apply readiness and connectivity updates and discover a later robot once."""
    entry, _client, _states = loaded
    coordinator = entry.runtime_data
    robot = coordinator.robots["robot3"]
    robot.state["availableCommands"]["stop"] = False
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert (
        hass.states.get(eid(hass, "button", "robot3", "button_stop")).state
        == "unavailable"
    )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "vacuum",
            "stop",
            {"entity_id": eid(hass, "vacuum", "robot3", "vacuum")},
            blocking=True,
        )
    robot.connected = False
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert (
        hass.states.get(eid(hass, "binary_sensor", "robot3", "connected")).state
        == "off"
    )
    assert (
        hass.states.get(eid(hass, "vacuum", "robot3", "vacuum")).state == "unavailable"
    )
    coordinator.robots["later"] = RobotData(
        "later", "Later robot", GEN3, connected=True, state=state3()
    )
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert hass.states.get(eid(hass, "vacuum", "later", "vacuum")).state == "docked"
    count = len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert (
        len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
        == count
    )


async def test_dynamic_map_and_schedule_event_switches(hass, loaded):
    """Toggle dynamic records without losing settings and remove deleted records."""
    entry, client, _states = loaded
    coordinator = entry.runtime_data
    maps = [{"id": "ground", "name": "Ground floor", "enabled": False}]
    event = {
        "id": "daily",
        "enabled": False,
        "days": ["mon", "wed"],
        "time": "07:30",
        "command": {
            "ability": "cleaning.start",
            "auto_localization": True,
            "force_floorplan": False,
            "runs": [
                {"map": None, "settings": {"mode": "eco", "navigation_mode": "normal"}}
            ],
        },
    }
    events = [deepcopy(event)]
    coordinator.robots["robot3"].maps = deepcopy(maps)
    coordinator.robots["robot4"].schedule = {
        "enabled": True,
        "events": deepcopy(events),
    }
    client.async_maps.side_effect = lambda serial: (
        deepcopy(maps) if serial == "robot3" else []
    )

    async def update_map(serial, map_id, name, enabled):
        """Save map enablement while verifying its identity and preserved name."""
        assert (serial, map_id, name) == ("robot3", "ground", "Ground floor")
        maps[0]["enabled"] = enabled

    client.async_update_map = AsyncMock(side_effect=update_map)
    original_command = client.async_command.side_effect

    async def command(serial, payload):
        """Store schedule edits and expose fresh events alongside normal mock reads."""
        if serial == "robot4":
            if payload.get("ability") == "scheduling.update":
                events[:] = [deepcopy(payload["event"])]
                return {"code": 200, "body": {}}
            if payload.get("ability") == "scheduling.show":
                return {
                    "code": 200,
                    "body": {"enabled": True, "events": deepcopy(events)},
                }
        return await original_command(serial, payload)

    client.async_command.side_effect = command
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    map_entity = eid(hass, "switch", "robot3", "map_ground_enabled")
    event_entity = eid(hass, "switch", "robot4", "schedule_event_daily_enabled")
    assert hass.states.get(map_entity).state == "off"
    assert hass.states.get(event_entity).state == "off"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": map_entity}, blocking=True
    )
    client.async_update_map.assert_awaited_once_with(
        "robot3", "ground", "Ground floor", True
    )
    assert hass.states.get(map_entity).state == "on"
    # A newer app edit must survive a switch toggle even if HA's event is stale.
    event["command"]["runs"][0]["settings"]["mode"] = "turbo"
    events[:] = [deepcopy(event)]
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": event_entity}, blocking=True
    )
    assert (
        call(
            "robot4",
            {"ability": "scheduling.update", "event": {**event, "enabled": True}},
        )
        in client.async_command.call_args_list
    )
    assert hass.states.get(event_entity).state == "on"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": event_entity}, blocking=True
    )
    assert (
        call("robot4", {"ability": "scheduling.update", "event": event})
        in client.async_command.call_args_list
    )
    assert hass.states.get(event_entity).state == "off"
    coordinator.robots["robot3"].connected = False
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    assert hass.states.get(map_entity).state == "on"
    coordinator.robots["robot3"].maps = []
    coordinator.robots["robot4"].schedule = {"events": []}
    coordinator.async_set_updated_data(coordinator.robots)
    await hass.async_block_till_done()
    # Records deleted on the server remove their switches instead of leaving
    # them unavailable forever.
    registry = er.async_get(hass)
    for entity_id in (map_entity, event_entity):
        assert hass.states.get(entity_id) is None
        assert registry.async_get(entity_id) is None
