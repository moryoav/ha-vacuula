"""Keep additional telemetry finite, private, bounded and tied to real fields."""

from unittest.mock import MagicMock

import pytest
from homeassistant.const import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.binary_sensor import create_entities as binary_entities
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.sensor import create_entities as sensor_entities
from custom_components.vacuula.telemetry import (
    key_and_label,
    latest_record,
    scalars,
    source_data,
)


def test_scalars_keep_raw_boolean_numeric_and_bounded_text():
    """Expose finite scalar telemetry while bounding text and rejecting other shapes."""
    assert dict(
        scalars(
            {
                "count": 0,
                "distance": 1.25,
                "negative": -1,
                "enabled": True,
                "disabled": False,
                "status": "x" * 300,
                "missing": None,
                "values": [1, 2],
                "blob": b"binary",
                "nan": float("nan"),
                "inf": float("inf"),
                "negative_inf": float("-inf"),
            }
        )
    ) == {
        ("count",): 0,
        ("distance",): 1.25,
        ("negative",): -1,
        ("enabled",): True,
        ("disabled",): False,
        ("status",): "x" * 255,
    }


@pytest.mark.parametrize(
    "key",
    [
        "serial",
        "label_serial",
        "SerialNumber",
        "PASSWORD",
        "token_value",
        "secretKey",
        "certificate",
        "ipAddress",
        "wifiSSID",
        "id",
        "uuid",
        "robot",
        "name",
        "mac",
        "ip",
        "zone_id",
        "request_uuid",
    ],
)
def test_private_and_identity_keys_are_excluded_at_any_depth(key):
    """Filter identity and credential field names even inside nested telemetry."""
    assert list(scalars({"safe": {key: "PRIVATE"}})) == []


@pytest.mark.parametrize(
    "value",
    ["https://private.local/path", "user@example.com", "-----BEGIN PRIVATE KEY-----"],
)
def test_private_text_is_excluded_without_copying_payload(value):
    """Filter URLs, email addresses and private-key text from dynamic telemetry."""
    assert list(scalars({"message": value})) == []


def test_scalars_stop_at_four_path_components_and_do_not_flatten_arrays():
    """Bound telemetry traversal depth and leave array contents unexposed."""
    assert list(
        scalars(
            {
                "a": {"b": {"c": {"visible": 1, "too_deep": {"hidden": 2}}}},
                "array": [{"hidden": 3}],
            }
        )
    ) == [(("a", "b", "c", "visible"), 1)]


def test_keys_encode_each_path_component_without_nested_key_collisions():
    """Encode telemetry paths without literal and nested separator collisions."""
    paths = [("a|b",), ("a", "b"), ("a%7Cb",), ("a/b",), ("a_b",)]
    keys = [key_and_label("stats", path)[0] for path in paths]
    assert len(keys) == len(set(keys))
    assert keys[:3] == [
        "telemetry_stats_a%7Cb",
        "telemetry_stats_a|b",
        "telemetry_stats_a%257Cb",
    ]
    assert key_and_label("stats", ("cleanedArea",)) == (
        "telemetry_stats_cleanedArea",
        "Stats cleaned area",
    )
    assert key_and_label("state", ("cleanedArea",))[0] != keys[0]


def test_latest_record_compares_absolute_timestamps_and_ignores_incomplete():
    """Choose history by absolute time and ignore incomplete records."""
    robot = RobotData("test", "Neato", GEN4)
    older = {"run": {"timing": {"start": "2026-10-19T12:00:00+03:00"}}}
    latest = {"run": {"start_at": "2026-10-19T10:00:00Z", "marker": 2}}
    robot.runs = [
        latest,
        {"run": None},
        {"run": []},
        older,
        {"run": {"start_at": 123}},
        {"run": {"start_at": "invalid"}},
        {"run": {"start_at": "2026-10-19T23:00:00"}},
        {"run": {"timing": "invalid"}},
        {},
    ]
    assert latest_record(robot) is latest
    assert source_data(robot, "last_run") == latest["run"]
    robot.runs = [older]
    assert latest_record(robot) is older
    robot.runs = [{"run": None}, {}, {"start_at": "2026-10-19"}]
    assert latest_record(robot) is None
    assert source_data(robot, "last_run") == {}


def test_source_data_selects_only_requested_telemetry_and_latest_subrun():
    """Select the requested telemetry source and the relevant latest subrun data."""
    robot = RobotData(
        "test",
        "Neato",
        GEN4,
        state={"state_value": 1},
        info={"info_value": 2},
        stats={"stats_value": 3, "runs": [{"progress": 4}, {"progress": 5}]},
        runs=[{"start_at": "2026-10-19T10:00:00Z", "runs": [{"area": 6}, {"area": 7}]}],
    )
    assert source_data(robot, "state") == {"state_value": 1}
    assert source_data(robot, "info") == {"info_value": 2}
    assert source_data(robot, "stats") is robot.stats
    assert source_data(robot, "active_run") == {"progress": 5}
    assert source_data(robot, "last_subrun") == {"area": 7}
    assert source_data(robot, "unexpected") == {}
    robot.runs = []
    assert source_data(robot, "last_subrun") == {}


@pytest.mark.parametrize("runs", [None, {}, [], [None], ["invalid"]])
def test_invalid_subrun_containers_return_no_telemetry(runs):
    """Return empty telemetry for absent or malformed subrun collections."""
    robot = RobotData(
        "test",
        "Neato",
        GEN4,
        stats={"runs": runs},
        runs=[{"start_at": "2026-10-19T10:00:00Z", "runs": runs}],
    )
    assert source_data(robot, "active_run") == {}
    assert source_data(robot, "last_subrun") == {}


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_dynamic_entities_preserve_raw_values_without_units(hass, generation):
    """Disable dynamic diagnostics by default and preserve raw values and types."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    robot = RobotData(
        "test",
        "Neato",
        generation,
        True,
        state={
            "details": {"charge": 99, "extraMetric": 10.5},
            "extraFlag": False,
            "error": None,
            "errors": [],
            "serial": "PRIVATE",
            "url": "http://private.local",
            "nan": float("nan"),
        },
        info={
            "extraInfo": "ready",
            "extraFlag": True,
            "battery": {"timeToEmpty": 65535},
        },
        stats={
            "extraCount": 2,
            "extraFlag": False,
            "runs": [{"progress": 0.75, "extraFlag": True}],
        },
        runs=[
            {
                "run": {
                    "start_at": "2026-10-19T10:00:00Z",
                    "extraField": 5,
                    "extraFlag": True,
                },
                "runs": [{"extraMetric": 6, "extraFlag": False}],
            }
        ],
    )
    coordinator.robots = {robot.serial: robot}
    coordinator.async_set_updated_data(coordinator.robots)
    sensors = {
        entity.entity_description.key: entity
        for entity in sensor_entities(coordinator, robot)
    }
    binaries = {
        entity.entity_description.key: entity
        for entity in binary_entities(coordinator, robot)
    }
    expected_sensors = {
        "telemetry_state_details|extraMetric": 10.5,
        "telemetry_info_extraInfo": "ready",
        "telemetry_stats_extraCount": 2,
        "telemetry_active_run_progress": 0.75,
        "telemetry_last_run_extraField": 5,
        "telemetry_last_subrun_extraMetric": 6,
    }
    expected_binaries = {
        "telemetry_state_extraFlag": False,
        "telemetry_info_extraFlag": True,
        "telemetry_stats_extraFlag": False,
        "telemetry_active_run_extraFlag": True,
        "telemetry_last_run_extraFlag": True,
        "telemetry_last_subrun_extraFlag": False,
    }
    for key, value in expected_sensors.items():
        entity = sensors[key]
        assert entity.native_value == value
        assert entity.available
        assert entity.entity_registry_enabled_default is False
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert entity.native_unit_of_measurement is None
        assert entity.device_class is None
    for key, value in expected_binaries.items():
        entity = binaries[key]
        assert entity.is_on is value
        assert entity.available
        assert entity.entity_registry_enabled_default is False
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        assert entity.device_class is None
    assert "telemetry_state_details|charge" not in sensors
    assert "telemetry_state_extraFlag" not in sensors
    assert "telemetry_info_extraInfo" not in binaries
    assert not any(
        "serial" in key or key.endswith("_url") or key.endswith("_nan")
        for key in sensors
    )
    if generation == GEN3:
        assert "telemetry_info_battery|timeToEmpty" not in sensors
        assert "time_to_empty" not in sensors
    robot.info["extraFlag"] = "not-a-boolean"
    assert binaries["telemetry_info_extraFlag"].is_on is None
    assert not binaries["telemetry_info_extraFlag"].available
