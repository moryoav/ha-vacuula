"""Telemetry conversion, optional capabilities and exact control payloads."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.binary_sensor import (
    DESCRIPTIONS as BINARY_DESCRIPTIONS,
)
from custom_components.vacuula.binary_sensor import (
    binary_value,
)
from custom_components.vacuula.binary_sensor import (
    create_entities as binary_entities,
)
from custom_components.vacuula.button import (
    create_entities as button_entities,
)
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.entity import number, value_at
from custom_components.vacuula.number import (
    DESCRIPTIONS as NUMBER_DESCRIPTIONS,
)
from custom_components.vacuula.number import (
    VacuulaNumber,
)
from custom_components.vacuula.number import (
    create_entities as number_entities,
)
from custom_components.vacuula.select import (
    VacuulaSelect,
)
from custom_components.vacuula.select import (
    create_entities as select_entities,
)
from custom_components.vacuula.sensor import (
    DESCRIPTIONS,
    VacuulaSensor,
    last_value,
    latest_run,
    parse_timestamp,
    scalar_stats,
    sensor_value,
)
from custom_components.vacuula.sensor import (
    create_entities as sensor_entities,
)
from custom_components.vacuula.switch import (
    VacuulaMapSwitch,
    VacuulaScheduleEventSwitch,
    VacuulaSwitch,
    switch_value,
)
from custom_components.vacuula.switch import (
    create_entities as switch_entities,
)
from custom_components.vacuula.vacuum import (
    VacuulaVacuum,
    cleaning_mode,
    fan_modes,
    start_payload,
)


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    coordinator.robots["robot"] = RobotData("robot", "Neato", GEN3, connected=True)
    coordinator.async_command = AsyncMock(return_value={})
    coordinator.async_refresh_robot = AsyncMock()
    coordinator.async_refresh_maps = AsyncMock()
    coordinator.async_return_to_base = AsyncMock()
    return coordinator


def description(key):
    return next(item for item in DESCRIPTIONS if item.key == key)


def test_optional_numeric_values():
    assert number(True) is None
    assert number("2") is None
    assert number(float("nan")) is None
    assert number(float("inf")) is None
    assert number(float("-inf")) is None
    assert number(0) == 0
    assert number(0.5) == 0.5
    assert value_at({"a": []}, "a", "b") is None
    assert value_at({}, "missing") is None
    assert value_at({"a": {"b": 0}}, "a", "b") == 0


@pytest.mark.parametrize(
    ("key", "state", "expected"),
    [
        ("battery", {"details": {"charge": 0}}, 0),
        ("robot_state", {"state": 3}, "paused"),
        ("robot_action", {"action": 12}, "exploring_map"),
        ("active_cleaning_mode", {"cleaning": {"mode": 2}}, "turbo"),
        ("active_navigation_mode", {"cleaning": {"navigationMode": 2}}, "extra_care"),
        ("active_cleaning_category", {"cleaning": {"category": 4}}, "map"),
        ("active_cleaning_modifier", {"cleaning": {"modifier": 2}}, "double"),
        ("active_spot_height", {"cleaning": {"spotHeight": 0}}, 0),
        ("alert", {"alert": "dustbin_full"}, "dustbin_full"),
        ("error", {"error": None}, "none"),
    ],
)
def test_gen3_state_values(key, state, expected):
    robot = RobotData("x", "x", GEN3, state=state)
    assert sensor_value(robot, description(key)) == expected


@pytest.mark.parametrize("invalid", [None, True, "oops", [], {}])
def test_bad_battery_values(invalid):
    robot = RobotData("x", "x", GEN3, state={"details": {"charge": invalid}})
    assert sensor_value(robot, description("battery")) is None


@pytest.mark.parametrize(
    "key",
    [
        "robot_state",
        "robot_action",
        "active_cleaning_mode",
        "active_navigation_mode",
        "active_cleaning_category",
        "active_cleaning_modifier",
    ],
)
def test_absent_enums_stay_unknown(key):
    assert sensor_value(RobotData("x", "x", GEN3), description(key)) is None


def test_general_info_sentinels_and_date(coordinator):
    robot = coordinator.robots["robot"]
    robot.info = {
        "battery": {
            "timeToEmpty": 65535,
            "timeToFullCharge": 0,
            "manufacturingDate": "2024-2-28",
            "totalCharges": 0,
            "vendor": "ZH",
            "authorizationStatus": 1,
        },
        "productNumber": "945-0005",
    }
    assert sensor_value(robot, description("time_to_empty")) is None
    assert sensor_value(robot, description("time_to_full_charge")) == 0
    assert sensor_value(robot, description("battery_manufacturing_date")) == date(
        2024, 2, 28
    )
    assert sensor_value(robot, description("battery_total_charges")) == 0
    robot.info["battery"]["manufacturingDate"] = "bad-date"
    assert sensor_value(robot, description("battery_manufacturing_date")) is None
    robot.info["battery"]["timeToFullCharge"] = None
    assert not VacuulaSensor(
        coordinator, "robot", description("time_to_full_charge")
    ).available


def test_confirmed_empty_collections_and_raw_stats(coordinator):
    robot = coordinator.robots["robot"]
    assert sensor_value(robot, description("map_count")) is None
    assert sensor_value(robot, description("run_count")) is None
    robot.capabilities.update(maps_available=True, runs_available=True)
    assert sensor_value(robot, description("map_count")) == 0
    assert sensor_value(robot, description("run_count")) == 0
    robot.schedule = {"enabled": True, "events": [{"mode": 1}]}
    assert sensor_value(robot, description("schedule_event_count")) == 1
    robot.stats = {
        "cleanings": 0,
        "nested": {"longName": 1.5, "status": "ok"},
        "list": [1],
        "flag": True,
        "unknown": None,
    }
    values = dict(scalar_stats(robot.stats))
    assert values == {
        ("cleanings",): 0,
        ("nested", "longName"): 1.5,
        ("nested", "status"): "ok",
    }
    sensors = {item.unique_id: item for item in sensor_entities(coordinator, robot)}
    assert sensors["robot_telemetry_stats_cleanings"].native_value == 0
    assert not sensors[
        "robot_telemetry_stats_cleanings"
    ].entity_registry_enabled_default


def test_gen4_sensors_and_capabilities(coordinator):
    robot = coordinator.robots["robot"]
    robot.generation = GEN4
    robot.state = {
        "state": "busy",
        "action": "cleaning",
        "errors": [{"code": "lw_hanging", "severity": "warning"}],
        "details": {"is_quickboost": False, "base_type": "standard"},
        "autonomy_states": {"docking": 0},
        "cleaning_center": {"bag_status": "bag_ok"},
    }
    assert sensor_value(robot, description("robot_state")) == "busy"
    assert sensor_value(robot, description("robot_action")) == "cleaning"
    assert sensor_value(robot, description("error")) == "lw_hanging"
    assert sensor_value(robot, description("autonomy_docking")) == 0
    assert sensor_value(robot, description("bag_status")) == "bag_ok"
    robot.schedule = {"events": []}
    assert sensor_value(robot, description("schedule_event_count")) == 0
    assert not number_entities(coordinator, robot)
    assert not switch_entities(coordinator, robot)
    robot.preferences = {
        "force_floorplan": False,
        "language": "en",
        "available_languages": ["en", "de"],
    }
    assert {item.key for item in switch_entities(coordinator, robot)} == {
        "force_floorplan"
    }
    assert "robot_language" in {
        item.key for item in select_entities(coordinator, robot)
    }


def test_run_timestamps_and_gen3_statistics():
    robot = RobotData(
        "x",
        "x",
        GEN3,
        runs=[
            {"id": "old", "run": {"start_at": "2026-10-01T00:00:00+00:00"}},
            {
                "id": "new",
                "run": {
                    "start_at": "2026-10-02T01:00:00+00:00",
                    "end_at": "2026-10-02T01:02:00+00:00",
                    "status": "done",
                    "launched_from": "app",
                    "cleaned_area": 12.5,
                    "picked_up_count": 0,
                    "run_charge_at_start": 90,
                    "run_charge_at_end": 80,
                },
            },
        ],
    )
    assert latest_run(robot)["id"] == "new"
    for kind, expected in (
        ("last_area", 12.5),
        ("last_duration", 120),
        ("last_status", "done"),
        ("last_started_by", "app"),
        ("last_battery", 10),
        ("last_pickups", 0),
    ):
        assert last_value(robot, kind) == expected
    assert last_value(robot, "last_started").tzinfo is not None
    assert last_value(robot, "last_finished").tzinfo is not None
    assert parse_timestamp("2026-10-02T01:00:00") is None
    assert parse_timestamp("invalid") is None
    assert parse_timestamp(None) is None
    robot.runs[1]["run"]["end_at"] = "2026-10-01T01:00:00+00:00"
    assert last_value(robot, "last_duration") is None
    robot.runs[1]["run"]["run_charge_at_end"] = 99
    assert last_value(robot, "last_battery") is None
    robot.runs = []
    assert last_value(robot, "last_status") is None


def test_gen4_run_aggregates():
    robot = RobotData(
        "x",
        "x",
        GEN4,
        runs=[
            {
                "id": "cleaning",
                "timing": {
                    "start": "2026-10-02T01:00:00Z",
                    "end": "2026-10-02T01:05:00Z",
                },
                "started_by": "schedule",
                "cleaning_complete": False,
                "battery_consumed": 8,
                "runs": [
                    {"state": "done", "stats": {"area": 1.2, "pickup_count": 0}},
                    {"state": "paused", "stats": {"area": 2.3, "pickup_count": 1}},
                ],
            }
        ],
    )
    assert last_value(robot, "last_area") == pytest.approx(3.5)
    assert last_value(robot, "last_pickups") == 1
    assert last_value(robot, "last_battery") == 8
    assert last_value(robot, "last_status") == "paused"
    assert last_value(robot, "last_started_by") == "schedule"
    assert last_value(robot, "last_duration") == 300
    robot.runs[0]["cleaning_complete"] = True
    assert last_value(robot, "last_status") == "done"
    robot.runs[0]["runs"][0]["stats"]["area"] = None
    assert last_value(robot, "last_area") is None


@pytest.mark.parametrize(
    ("generation", "state", "expected"),
    [
        (GEN3, {"error": "dustbin_missing"}, True),
        (GEN3, {"error": None}, False),
        (GEN4, {"errors": []}, False),
        (GEN4, {"errors": [{"code": "lw_hanging"}]}, True),
    ],
)
def test_problem_binary_values(generation, state, expected):
    desc = next(item for item in BINARY_DESCRIPTIONS if item.key == "problem")
    assert binary_value(RobotData("x", "x", generation, state=state), desc) is expected


def test_binary_unknowns_and_connection(coordinator):
    robot = coordinator.robots["robot"]
    assert [
        item.entity_description.key for item in binary_entities(coordinator, robot)
    ] == ["connected"]
    sensor = binary_entities(coordinator, robot)[0]
    robot.connected = False
    assert sensor.available
    assert sensor.is_on is False
    coordinator.last_update_success = False
    assert not sensor.available


async def test_preference_units_bounds_and_unknown(coordinator):
    robot = coordinator.robots["robot"]
    robot.preferences = {"filterChangeReminderInterval": 64800}
    desc = next(item for item in NUMBER_DESCRIPTIONS if item.key == "filter_reminder")
    entity = VacuulaNumber(coordinator, "robot", desc)
    assert entity.native_value == 45
    await entity.async_set_native_value(60)
    coordinator.async_command.assert_awaited_once_with(
        "robot",
        {"cmd": "setPreferences", "params": {"filterChangeReminderInterval": 86400}},
    )
    for invalid in (29, 31, 91, float("nan")):
        with pytest.raises(ServiceValidationError):
            await entity.async_set_native_value(invalid)
    robot.preferences = {}
    assert not entity.available
    assert not number_entities(coordinator, robot)
    robot.capabilities["spotCleaning"] = "basic-1"
    assert {
        item.entity_description.key for item in number_entities(coordinator, robot)
    } == {"spot_width", "spot_height"}


async def test_select_validation_hardware_modes_and_options(coordinator):
    robot = coordinator.robots["robot"]
    mode = VacuulaSelect(coordinator, "robot", "cleaning_mode")
    assert mode.options == ["eco", "turbo"]
    await mode.async_select_option("turbo")
    assert coordinator.cleaning_options["robot"]["mode"] == "turbo"
    coordinator.async_command.assert_not_awaited()
    with pytest.raises(ServiceValidationError):
        await mode.async_select_option("max")
    robot.generation = GEN4
    robot.info["label_serial"] = "ABCDN20001"
    assert fan_modes(robot) == ["eco", "turbo", "max", "auto"]
    robot.info["label_serial"] = "ABCDN10001"
    assert fan_modes(robot) == ["eco", "turbo"]
    navigation = VacuulaSelect(coordinator, "robot", "navigation_mode")
    assert navigation.options == ["normal", "gentle"]
    robot.preferences = {"language": "en", "available_languages": ["en", "de", 123]}
    language = VacuulaSelect(coordinator, "robot", "robot_language")
    assert language.options == ["en", "de"]
    assert language.current_option == "en"
    await language.async_select_option("de")
    coordinator.async_command.assert_awaited_with(
        "robot", {"ability": "settings.set_robot_language", "language": "de"}
    )


async def test_vacuum_guards_defaults_and_activity(coordinator):
    robot = coordinator.robots["robot"]
    vacuum = VacuulaVacuum(coordinator, "robot")
    robot.state = {"state": 4, "details": {}, "cleaning": {"mode": 2}}
    assert vacuum.activity == "error"
    assert cleaning_mode(coordinator, "robot") == "turbo"
    for state, action, expected in (
        (3, 0, "paused"),
        (1, 0, "idle"),
        (2, 4, "returning"),
        (2, 1, "cleaning"),
        (2, 6, "paused"),
        (0, 0, None),
    ):
        robot.state.update(state=state, action=action)
        assert vacuum.activity == expected
    robot.state["availableCommands"] = {"start": False}
    with pytest.raises(ServiceValidationError):
        await vacuum.async_start()
    with pytest.raises(ServiceValidationError):
        await vacuum.async_clean_spot()
    with pytest.raises(ServiceValidationError):
        await vacuum.async_set_fan_speed("max")
    robot.capabilities["spotCleaning"] = "basic-1"
    with pytest.raises(ServiceValidationError):
        await vacuum.async_clean_spot()
    robot.state["availableCommands"]["start"] = True
    assert start_payload(coordinator, "robot", spot=True)["params"]["spotWidth"] == 200
    robot.generation = GEN4
    assert start_payload(coordinator, "robot")["runs"][0]["map"] is None


async def test_buttons_safe_optional_and_refresh(coordinator):
    robot = coordinator.robots["robot"]
    assert "explore" not in {item.key for item in button_entities(coordinator, robot)}
    robot.capabilities.update(logCopy="basic-1", maps="advanced-1")
    robot.state = {"details": {"isDocked": True}, "availableCommands": {"start": True}}
    buttons = {item.key: item for item in button_entities(coordinator, robot)}
    await buttons["explore"].async_press()
    coordinator.async_command.assert_awaited_with(
        "robot", {"cmd": "startPersistentMapExploration"}
    )
    await buttons["dismiss_alert"].async_press()
    coordinator.async_command.assert_awaited_with(
        "robot", {"cmd": "dismissCurrentAlert"}
    )
    await buttons["upload_logs"].async_press()
    coordinator.async_command.assert_awaited_with(
        "robot", {"cmd": "uploadLogs", "params": {"recentCount": 1}}
    )
    assert not buttons["upload_logs"].entity_registry_enabled_default
    robot.connected = False
    assert buttons["refresh"].available
    await buttons["refresh"].async_press()
    coordinator.async_refresh_robot.assert_awaited_with("robot", full=True)
    with pytest.raises(ServiceValidationError):
        await buttons["explore"].async_press()


async def test_switches_have_only_verified_payloads(coordinator):
    robot = coordinator.robots["robot"]
    assert switch_value(robot, "schedule") is None
    robot.schedule = {"enabled": False}
    assert switch_value(robot, "schedule") is False
    robot.preferences = {"robotSounds": True}
    sounds = VacuulaSwitch(coordinator, "robot", "robot_sounds")
    await sounds.async_turn_off()
    coordinator.async_command.assert_awaited_with(
        "robot", {"cmd": "setPreferences", "params": {"robotSounds": False}}
    )
    schedule = VacuulaSwitch(coordinator, "robot", "schedule")
    await schedule.async_turn_on()
    coordinator.async_command.assert_awaited_with("robot", {"cmd": "enableSchedule"})
    await schedule.async_turn_off()
    coordinator.async_command.assert_awaited_with("robot", {"cmd": "disableSchedule"})
    robot.generation = GEN4
    floorplan = VacuulaSwitch(coordinator, "robot", "force_floorplan")
    await floorplan.async_turn_off()
    coordinator.async_command.assert_awaited_with(
        "robot", {"ability": "settings.set_cleaning_options", "force_floorplan": False}
    )


async def test_map_toggle_preserves_name_and_missing_map(coordinator):
    robot = coordinator.robots["robot"]
    robot.maps = [{"id": "map-id", "name": "Floor name", "enabled": False}]
    coordinator.async_update_map = AsyncMock()
    entity = VacuulaMapSwitch(coordinator, "robot", "map-id")
    assert entity.translation_placeholders == {"map_name": "Floor name"}
    assert entity.is_on is False
    robot.connected = False
    assert entity.available
    await entity.async_turn_on()
    coordinator.async_update_map.assert_awaited_with(
        "robot", "map-id", "Floor name", True
    )
    await entity.async_turn_off()
    coordinator.async_update_map.assert_awaited_with(
        "robot", "map-id", "Floor name", False
    )
    robot.maps = []
    assert not entity.available
    assert entity.is_on is None
    entity._update_name()
    assert entity.translation_placeholders == {"map_name": "map-id"}
    with pytest.raises(ServiceValidationError):
        await entity.async_turn_on()


async def test_schedule_event_preserves_command_and_unknowns(coordinator):
    robot = coordinator.robots["robot"]
    robot.generation = GEN4
    event = {
        "id": "event",
        "time": "08:00",
        "days": ["mon"],
        "enabled": False,
        "command": {
            "ability": "cleaning.start",
            "runs": [{"map": None, "settings": {"mode": "eco"}}],
        },
    }
    robot.schedule = {
        "events": [event, None, {"id": "bad", "enabled": True, "command": None}]
    }
    entities = switch_entities(coordinator, robot)
    assert len(entities) == 1
    entity = entities[0]
    assert isinstance(entity, VacuulaScheduleEventSwitch)
    assert entity.translation_placeholders == {"event_name": "08:00"}
    await entity.async_turn_on()
    coordinator.async_command.assert_awaited_with(
        "robot", {"ability": "scheduling.update", "event": {**event, "enabled": True}}
    )
    assert event["enabled"] is False
    await entity.async_turn_off()
    coordinator.async_command.assert_awaited_with(
        "robot", {"ability": "scheduling.update", "event": event}
    )
    robot.schedule = {"events": None}
    assert entity.event is None
    assert entity.is_on is None
    assert not entity.available
    entity._update_name()
    assert entity.translation_placeholders == {"event_name": "event"}
    with pytest.raises(ServiceValidationError):
        await entity.async_turn_off()
