"""Cached robot data. No robot credentials are needed by the integration."""

from dataclasses import dataclass, field
from typing import Any

from .const import GEN3


@dataclass(slots=True)
class RobotData:
    """One robot's last confirmed state and optional capabilities."""

    serial: str
    name: str
    generation: str
    connected: bool = False
    state: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    schedule: dict[str, Any] = field(default_factory=dict)
    maps: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    # False once the server no longer lists the robot. Its cached data stays so
    # existing entities keep working until the user removes the device.
    listed: bool = True


def gen3_paused_run_can_return_home(robot: RobotData) -> bool:
    """Recognize the D5's resumable pause despite its false goToBase flag.

    A live D5 accepts sendToBase in this state. The Vacuula app also sends it
    after a successful pause without waiting for goToBase to become true.
    """
    commands = robot.state.get("availableCommands")
    details = robot.state.get("details")
    return (
        robot.generation == GEN3
        and robot.state.get("state") == 3
        and robot.state.get("action") in (1, 2, 3, 6, 11, 12, 15)
        and robot.state.get("error") is None
        and isinstance(commands, dict)
        and commands.get("resume") is True
        and isinstance(details, dict)
        and details.get("isDocked") is False
    )


def return_to_base_ready(robot: RobotData) -> bool | None:
    """Combine native docking, pause-then-home and confirmed Gen3 paused runs."""
    gen3 = robot.generation == GEN3
    commands = robot.state.get("availableCommands" if gen3 else "available_commands")
    if not isinstance(commands, dict):
        return None
    home = commands.get("goToBase" if gen3 else "return_to_base")
    pause = commands.get("pause")
    if home is True or pause is True or gen3_paused_run_can_return_home(robot):
        return True
    if home is False and pause is False:
        return False
    return None


def response_data(response: dict[str, Any]) -> dict[str, Any]:
    """Unwrap data replies while preserving Gen3 state fields at the root."""
    if isinstance(body := response.get("body"), dict):
        return body
    data = response.get("data")
    result = dict(data) if isinstance(data, dict) else {}
    result.update(
        (key, value)
        for key, value in response.items()
        if key not in {"version", "reqId", "result", "data"}
    )
    return result
