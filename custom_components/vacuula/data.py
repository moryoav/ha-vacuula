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
    # True after this integration sent a Gen3 robot home, until the robot
    # leaves the trip. The D5 reports the first part of the trip as
    # "recovering location" (action 9), the same code it uses while cleaning.
    returning: bool = False


def track_return_trip(robot: RobotData) -> None:
    """End a tracked trip home once the robot is no longer driving to its base."""
    if robot.returning and not (
        robot.state.get("state") == 2 and robot.state.get("action") in (4, 9)
    ):
        robot.returning = False


def event_targets_map(event: dict[str, Any]) -> bool:
    """Return whether a Gen4 schedule event has a run that targets a floorplan."""
    command = event.get("command")
    runs = command.get("runs") if isinstance(command, dict) else None
    return isinstance(runs, list) and any(
        isinstance(run, dict) and isinstance(run.get("map"), dict) for run in runs
    )


def return_to_base_ready(robot: RobotData) -> bool | None:
    """Combine native docking with the pause-then-home sequence."""
    gen3 = robot.generation == GEN3
    commands = robot.state.get("availableCommands" if gen3 else "available_commands")
    if not isinstance(commands, dict):
        return None
    home = commands.get("goToBase" if gen3 else "return_to_base")
    pause = commands.get("pause")
    if home is True or pause is True:
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
