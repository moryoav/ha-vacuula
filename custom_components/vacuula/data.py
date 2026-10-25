"""Cached robot data. No robot credentials are needed by the integration."""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from .const import GEN3

RETURN_PENDING_SECONDS = 60
RETURN_TRIP_SECONDS = 15 * 60
type ReturnTripPhase = Literal["pending", "moving", "paused"]


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
    # The D5 uses action 9 both while cleaning and while finding its base.
    # Keep bounded command intent until motion confirms which activity it is.
    return_phase: ReturnTripPhase | None = None
    return_deadline: float = 0

    @property
    def returning(self) -> bool:
        """Report observed home motion only while its command context is current."""
        return self.return_phase == "moving" and monotonic() < self.return_deadline


def clear_return_trip(robot: RobotData) -> None:
    """Discard expired or contradicted home intent without changing robot telemetry."""
    robot.return_phase = None
    robot.return_deadline = 0


def begin_return_trip(robot: RobotData) -> None:
    """Allow 60 seconds for Gen3 home motion after a single command is dispatched."""
    if robot.generation != GEN3:
        return
    robot.return_phase = "pending"
    robot.return_deadline = monotonic() + RETURN_PENDING_SECONDS


def track_return_trip(robot: RobotData) -> None:
    """Track Gen3 home departure, pauses and completion without extending its bound.

    Repeated paused reports during departure do not erase pending intent. Once
    home motion is observed, its 15-minute deadline remains absolute through
    subsequent events, polling and pause/resume. Idle, error and unrelated
    motion end the trip; expiry also prevents stale intent labeling a later run.
    """
    phase = robot.return_phase
    if phase is None:
        return
    now = monotonic()
    state = robot.state.get("state")
    action = robot.state.get("action")
    if now >= robot.return_deadline or state not in (2, 3):
        clear_return_trip(robot)
    elif state == 2 and action in (4, 9):
        robot.return_phase = "moving"
        if phase == "pending":
            robot.return_deadline = now + RETURN_TRIP_SECONDS
    elif state == 3:
        if phase == "moving":
            robot.return_phase = "paused"
    elif phase != "pending":
        clear_return_trip(robot)


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
