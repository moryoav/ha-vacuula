"""Reject malformed optional telemetry without creating false healthy states."""

import pytest

from custom_components.vacuula.binary_sensor import DESCRIPTIONS as BINARY
from custom_components.vacuula.binary_sensor import binary_value
from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.sensor import DESCRIPTIONS as SENSORS
from custom_components.vacuula.sensor import sensor_value
from custom_components.vacuula.telemetry import source_data


@pytest.mark.parametrize(
    ("key", "state"),
    [
        ("problem", {"state": 1}),
        ("problem", {"error": {}}),
        ("dustbin_missing", {"error": []}),
        ("dustbin_full", {"alert": {}}),
    ],
)
def test_missing_or_invalid_errors_are_unknown(key, state):
    robot = RobotData("test", "Neato", GEN3, state=state)
    assert binary_value(robot, next(item for item in BINARY if item.key == key)) is None


@pytest.mark.parametrize("errors", ["oops", ["oops"]])
def test_malformed_gen4_problem(errors):
    robot = RobotData("test", "Neato", GEN4, state={"errors": errors})
    assert (
        binary_value(robot, next(item for item in BINARY if item.key == "problem"))
        is None
    )


def test_malformed_history_and_error_values():
    robot = RobotData(
        "test",
        "Neato",
        GEN4,
        runs=[
            {
                "timing": {"start": "2026-10-19T08:00:00Z"},
                "runs": ["invalid"],
            }
        ],
    )
    assert (
        sensor_value(
            robot, next(item for item in SENSORS if item.key == "last_run_area")
        )
        is None
    )
    robot.state = {"errors": "bad"}
    assert (
        sensor_value(robot, next(item for item in SENSORS if item.key == "error"))
        is None
    )
    robot.generation = GEN3
    robot.state = {"error": {"invalid": "data"}}
    assert (
        sensor_value(robot, next(item for item in SENSORS if item.key == "error"))
        is None
    )


def test_active_subrun_is_not_the_future_last_zone():
    active = {"state": "in_progress", "stats": {"area": 12}}
    robot = RobotData(
        "test", "Neato", GEN4, stats={"runs": [active, {"state": "not_started"}]}
    )
    assert source_data(robot, "active_run") is active
