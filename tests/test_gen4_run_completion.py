"""Distinguish a complete Gen4 cleaning from the status of any one zone."""

import pytest

from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.sensor import last_value


def gen4_robot(**fields):
    """Provide a dated cleaning whose final zone reports success."""
    return RobotData(
        "robot",
        "D10",
        GEN4,
        runs=[
            {
                "timing": {"start": "2026-10-20T08:00:00Z"},
                "runs": [{"state": "not_reachable"}, {"state": "done"}],
                **fields,
            }
        ],
    )


@pytest.mark.parametrize("earlier", ["not_reachable", "error", "canceled"])
def test_incomplete_cleaning_cannot_be_done_because_final_zone_succeeded(earlier):
    """Use the overall failure flag when an earlier zone failed or was skipped."""
    robot = gen4_robot(cleaning_complete=False)
    robot.runs[0]["runs"][0]["state"] = earlier
    assert last_value(robot, "last_status") == "incomplete"


@pytest.mark.parametrize("runs", [None, [], ["invalid"], [{"state": "paused"}]])
@pytest.mark.parametrize(
    ("complete", "expected"), [(True, "done"), (False, "incomplete")]
)
def test_overall_completion_does_not_require_subrun_details(runs, complete, expected):
    """Use a valid completion flag independently of missing or partial zone data."""
    robot = gen4_robot(cleaning_complete=complete, runs=runs)
    assert last_value(robot, "last_status") == expected


@pytest.mark.parametrize("invalid", [None, "false", 0, 1, {}, []])
def test_malformed_overall_flag_does_not_infer_success_from_a_zone(invalid):
    """Keep overall completion unknown when the robot did not provide a boolean."""
    robot = gen4_robot(cleaning_complete=invalid)
    assert last_value(robot, "last_status") is None


def test_missing_overall_flag_does_not_infer_success_from_a_zone():
    """Keep overall completion unknown when only a subrun says it finished."""
    assert last_value(gen4_robot(), "last_status") is None


@pytest.mark.parametrize("status", ["complete", "canceled", "error", "in_progress"])
def test_gen3_preserves_reported_run_status(status):
    """Leave Gen3's explicit run status unchanged by Gen4 completion semantics."""
    robot = RobotData(
        "robot",
        "D5",
        GEN3,
        runs=[{"run": {"start_at": "2026-10-20T08:00:00Z", "status": status}}],
    )
    assert last_value(robot, "last_status") == status
