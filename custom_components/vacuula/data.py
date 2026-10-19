"""Cached robot data. No robot credentials are needed by the integration."""

from dataclasses import dataclass, field
from typing import Any


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
