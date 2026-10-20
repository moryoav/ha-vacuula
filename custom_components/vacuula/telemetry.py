"""Enumerate additional scalar telemetry without exposing maps or private identity."""

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .data import RobotData

_PRIVATE = (
    "serial",
    "password",
    "secret",
    "token",
    "certificate",
    "address",
    "ssid",
    "credential",
    "apikey",
    "api_key",
)
_IDENTITY = {
    "id",
    "uuid",
    "robot",
    "name",
    "mac",
    "ip",
    "label_serial",
    "key",
    "auth",
    "authorization",
    "authentication",
}


def safe_text(value: Any) -> str | None:
    """Bound diagnostic text and omit URLs, email addresses and private-key text."""
    if not isinstance(value, str) or any(
        marker in value.lower() for marker in ("://", "-----begin", "@")
    ):
        return None
    return value[:255]


def source_data(robot: RobotData, source: str) -> dict[str, Any]:
    """Return only bounded dictionaries; histories expose the latest dated record."""
    if source in {"state", "info", "stats"}:
        return {"state": robot.state, "info": robot.info, "stats": robot.stats}[source]
    if source == "last_run":
        record = latest_record(robot)
        return record.get("run", record) if record else {}
    if source in {"active_run", "last_subrun"}:
        record = robot.stats if source == "active_run" else latest_record(robot) or {}
        runs = record.get("runs")
        if source == "active_run" and isinstance(runs, list):
            active = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict)
                    and item.get("state")
                    in ("in_progress", "paused", "suspend_charging", "error")
                ),
                None,
            )
            if active is not None:
                return active
        return (
            runs[-1]
            if isinstance(runs, list) and runs and isinstance(runs[-1], dict)
            else {}
        )
    return {}


def latest_record(robot: RobotData) -> dict[str, Any] | None:
    """Compare absolute instants, ignoring incomplete or timezone-free records."""
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for item in robot.runs:
        run = item.get("run", item)
        if not isinstance(run, dict):
            continue
        timing = run.get("timing")
        value = run.get("start_at") or (
            timing.get("start") if isinstance(timing, dict) else None
        )
        if not isinstance(value, str):
            continue
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            dated.append((stamp, item))
    return max(dated, key=lambda pair: pair[0])[1] if dated else None


def scalars(
    data: dict[str, Any], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Avoid arrays, object dumps, non-finite values and sensitive or identity keys."""
    for key, value in data.items():
        lower = key.lower()
        if (
            lower in _IDENTITY
            or lower.endswith(("_id", "_uuid"))
            or any(marker in lower for marker in _PRIVATE)
        ):
            continue
        current = (*path, key)
        if isinstance(value, dict) and len(current) < 4:
            yield from scalars(value, current)
        elif isinstance(value, str):
            if (text := safe_text(value)) is not None:
                yield current, text
        elif isinstance(value, bool):
            yield current, value
        elif (
            isinstance(value, int | float)
            and value == value
            and abs(value) != float("inf")
        ):
            yield current, value


def key_and_label(source: str, path: tuple[str, ...]) -> tuple[str, str]:
    """Encode path parts independently to prevent nested-key collisions."""
    key = "telemetry_" + source + "_" + "|".join(quote(part, safe="") for part in path)
    label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", " ".join((source, *path)))
    return key, label.replace("_", " ").capitalize()
