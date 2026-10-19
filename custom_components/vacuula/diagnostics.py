"""Cached, deliberately limited diagnostics for Vacuula."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import VacuulaConfigEntry

_REDACTED = "**REDACTED**"
_SAFE_KEYS = frozenset(
    {
        "version",
        "server_version",
        "firmware",
        "firmware_version",
        "model",
        "model_name",
        "hardware_version",
        "software",
        "software_version",
        "protocol",
        "generation",
        "manufacturer",
        "build",
        "branch",
        "state",
        "action",
        "error",
        "errors",
        "code",
        "category",
        "mode",
        "navigationMode",
        "navigation_mode",
        "modifier",
        "isCharging",
        "isDocked",
        "isSleeping",
        "isScheduleEnabled",
        "isDirtbinInstalled",
        "isDirtbinFull",
        "isCleanable",
        "isCleanableByAutoSchedule",
        "isPaused",
        "isEnabled",
        "enabled",
        "active",
        "connected",
        "level",
        "charge",
        "chargeLevel",
        "battery",
        "batteryLevel",
        "battery_level",
        "battery_charge",
        "chargePercent",
        "dock",
        "dirtbin",
        "brush",
        "filter",
        "maintenance",
        "cleaning",
        "cleaningStatus",
        "cleaning_mode",
        "cleaningFrequency",
        "cleaning_frequency",
        "auto_return",
        "auto_dock",
        "robotSounds",
        "robotSoundsEnabled",
        "sound_enabled",
        "sound",
        "volume",
        "language",
        "robot_language",
        "eco",
        "turbo",
        "gentle",
        "normal",
        "extra_care",
        "deep",
        "nogo_enabled",
        "repeat",
        "cleanedArea",
        "cleaned_area",
        "cleanedTime",
        "cleaned_time",
        "totalCleaningTime",
        "totalCleaningArea",
        "totalNumberOfRuns",
        "total_number_of_runs",
        "total_cleaning_time",
        "total_cleaning_area",
        "runCount",
        "run_count",
        "count",
        "area",
        "duration",
        "total",
        "total_time",
        "total_area",
        "status",
        "result",
        "capabilities",
        "availableServices",
        "findMe",
        "generalInfo",
        "houseCleaning",
        "spotCleaning",
        "schedule",
        "preferences",
        "localStats",
    }
)
_INFO_KEYS = frozenset(
    {
        "version",
        "server_version",
        "firmware",
        "firmware_version",
        "model",
        "model_name",
        "hardware_version",
        "software",
        "software_version",
        "protocol",
        "generation",
        "manufacturer",
        "build",
        "branch",
    }
)


def _snapshot(value: Any, private: tuple[str, ...]) -> Any:
    """Copy reviewed fields and redact identifiers embedded in string values."""
    if isinstance(value, Mapping):
        return {
            key: _snapshot(item, private)
            for key, item in value.items()
            if isinstance(key, str) and key in _SAFE_KEYS
        }
    if isinstance(value, list | tuple):
        return [_snapshot(item, private) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if any(token in value for token in private) or any(
            marker in lowered for marker in ("://", "-----begin", "@")
        ):
            return _REDACTED
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    return _REDACTED


def _info(value: dict[str, Any], private: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _snapshot(item, private)
        for key, item in value.items()
        if key in _INFO_KEYS and isinstance(item, str | bool | int | float)
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VacuulaConfigEntry
) -> dict[str, Any]:
    """Return cached operational data without making any network requests."""
    coordinator = entry.runtime_data
    private = tuple(
        token
        for token in (
            coordinator.client.base_url,
            *(robot.serial for robot in coordinator.robots.values()),
            *(robot.name for robot in coordinator.robots.values()),
        )
        if token
    )
    robots = []
    for robot in coordinator.robots.values():
        robots.append(
            {
                "generation": robot.generation,
                "connected": robot.connected,
                "info": _info(robot.info, private),
                "state": _snapshot(robot.state, private),
                "stats": _snapshot(robot.stats, private),
                "preferences": _snapshot(robot.preferences, private),
                "capabilities": _snapshot(robot.capabilities, private),
                "schedule": {
                    "enabled": _snapshot(
                        robot.schedule.get("enabled", robot.schedule.get("isEnabled")),
                        private,
                    ),
                    "event_count": len(robot.schedule.get("events", []))
                    if isinstance(robot.schedule.get("events"), list)
                    else None,
                },
                "map_count": len(robot.maps),
                "run_count": len(robot.runs),
            }
        )
    return {
        "server": _info(coordinator.server_info, private),
        "last_update_success": coordinator.last_update_success,
        "robots": robots,
    }
