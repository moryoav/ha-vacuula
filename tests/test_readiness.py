"""Command readiness includes the integration's pause-then-dock control."""

import pytest

from custom_components.vacuula.binary_sensor import DESCRIPTIONS, binary_value
from custom_components.vacuula.const import GEN3, GEN4
from custom_components.vacuula.data import RobotData


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize(
    ("home", "pause", "expected"),
    [
        (True, True, True),
        (True, False, True),
        (True, None, True),
        (False, True, True),
        (False, False, False),
        (False, None, None),
        (None, True, True),
        (None, False, None),
        (None, None, None),
        (False, "true", None),
        ("true", False, None),
        (False, 1, None),
        (0, False, None),
    ],
)
def test_home_readiness_considers_pause_without_inventing_unknown_values(
    generation, home, pause, expected
):
    gen3 = generation == GEN3
    robot = RobotData(
        "robot",
        "Neato",
        generation,
        state={
            "availableCommands" if gen3 else "available_commands": {
                "goToBase" if gen3 else "return_to_base": home,
                "pause": pause,
            }
        },
    )
    description = next(
        item for item in DESCRIPTIONS if item.key == "can_return_to_base"
    )
    assert binary_value(robot, description) is expected


@pytest.mark.parametrize("generation", [GEN3, GEN4])
def test_absent_readiness_remains_unknown(generation):
    description = next(
        item for item in DESCRIPTIONS if item.key == "can_return_to_base"
    )
    assert binary_value(RobotData("robot", "Neato", generation), description) is None
