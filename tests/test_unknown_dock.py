"""Keep unknown docking telemetry from selecting mapless Automatic cleaning."""

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import cleaning_target


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("dock_value", [None, "false", 0, 1, {}, []])
@pytest.mark.parametrize("category", ["auto", "map"])
def test_unknown_dock_never_bypasses_an_active_floorplan(
    generation, dock_value, category
):
    """Require a boolean dock report before choosing an immediate mapped policy."""
    gen3 = generation == GEN3
    robot = RobotData(
        "robot",
        "Neato",
        generation,
        state={"details": {"isDocked" if gen3 else "is_docked": dock_value}},
        maps=[{"id": "floor", "enabled" if gen3 else "promoted": True}],
    )
    with pytest.raises(ServiceValidationError) as error:
        cleaning_target(robot, category)
    assert error.value.translation_key == "dock_status_unknown"


@pytest.mark.parametrize("generation", [GEN3, GEN4])
def test_future_schedule_does_not_require_current_dock_telemetry(generation):
    """Allow mapped future schedule selection when current dock state is unknown."""
    gen3 = generation == GEN3
    robot = RobotData(
        "robot",
        "Neato",
        generation,
        maps=[{"id": "floor", "enabled" if gen3 else "promoted": True}],
    )
    target = cleaning_target(robot, require_dock=False)
    assert target == (
        {"category": 4}
        if gen3
        else {"map": {"rank_id": "floor", "track_id": None, "nogo_enabled": True}}
    )


@pytest.mark.parametrize("generation", [GEN3, GEN4])
def test_known_offdock_still_allows_the_documented_automatic_fallback(generation):
    """Keep ordinary off-dock starts available when the robot explicitly reports it."""
    gen3 = generation == GEN3
    robot = RobotData(
        "robot",
        "Neato",
        generation,
        state={"details": {"isDocked" if gen3 else "is_docked": False}},
        maps=[{"id": "floor", "enabled" if gen3 else "promoted": True}],
    )
    assert cleaning_target(robot) == ({"category": 2} if gen3 else {"map": None})
