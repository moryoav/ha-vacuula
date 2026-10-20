"""Validated actions for maps, schedules and advanced Vacuula features."""

import math
from contextlib import suppress
from copy import deepcopy
from typing import Any, cast
from zoneinfo import ZoneInfo

import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .client import VacuulaError
from .const import DOMAIN, GEN3, GEN4
from .coordinator import VacuulaCoordinator
from .data import RobotData, response_data
from .vacuum import cleaning_target

_IDENTIFIER = vol.All(
    str,
    vol.Match(r"^[A-Za-z0-9_-][A-Za-z0-9_.:-]{0,255}$"),
)
_NAME = vol.All(str, vol.Length(min=1, max=128))
_TIME = vol.Match(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_MODE = vol.In(["eco", "turbo", "auto", "max"])
_TARGET = {vol.Required("device_id"): cv.string}
_GEN3_EVENT = vol.Schema(
    {
        vol.Required("mode"): vol.In([1, 2]),
        vol.Required("day"): vol.All(int, vol.Range(min=0, max=6)),
        vol.Required("startTime"): _TIME,
        vol.Optional("boundaryId"): _IDENTIFIER,
        vol.Optional("boundary"): vol.Any(None, dict),
    }
)


def _finite(value: float) -> float:
    """Reject non-finite coordinates before accepting boundary geometry."""
    if not math.isfinite(value):
        raise vol.Invalid("must be a finite number")
    return value


# Accept what the Vacuula app stores, so boundaries read with get_data can be
# written back: it saves empty names, older colours that are not #rrggbb, and
# points placed just outside the map (normalized coordinates are not clamped).
_VERTEX = vol.All([vol.All(vol.Coerce(float), _finite)], vol.Length(min=2, max=2))
_BOUNDARY = vol.Schema(
    {
        vol.Required("id"): _IDENTIFIER,
        vol.Required("name"): vol.All(str, vol.Length(max=128)),
        vol.Required("color"): vol.All(str, vol.Length(max=64)),
        vol.Required("type"): vol.In(["polygon", "polyline"]),
        vol.Required("vertices"): vol.All([_VERTEX], vol.Length(min=2, max=1000)),
        vol.Required("enabled"): bool,
        vol.Optional("function"): vol.In(["cleaning", "nogo"]),
        vol.Optional("relevancy"): _VERTEX,
    }
)


def _error(key: str) -> ServiceValidationError:
    """Build a translated validation error for an invalid action request."""
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key)


def _resolve(
    hass: HomeAssistant, device_id: str
) -> tuple[VacuulaCoordinator, RobotData]:
    """Resolve a device to a robot owned by a loaded Vacuula entry."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise _error("unknown_robot")
    entry = hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise _error("unknown_robot")
    if entry.state is not ConfigEntryState.LOADED:
        raise _error("entry_not_loaded")
    coordinator = cast(ConfigEntry[VacuulaCoordinator], entry).runtime_data
    for domain, serial in device.identifiers:
        if domain == DOMAIN and serial in coordinator.robots:
            return coordinator, coordinator.robots[serial]
    raise _error("unknown_robot")


def _require_generation(robot: RobotData, generation: str) -> None:
    """Reject an action that the robot generation does not support."""
    if robot.generation != generation:
        raise _error("unsupported_action")


def _cleaning_settings(robot: RobotData, data: dict[str, Any]) -> tuple[str, str]:
    """Validate cleaning mode and navigation against generation and model support."""
    mode = data.get("mode", "eco")
    navigation = data.get("navigation", "normal")
    if robot.generation == GEN3:
        if mode not in {"eco", "turbo"} or navigation not in {
            "normal",
            "extra_care",
            "deep",
        }:
            raise _error("unsupported_action")
    elif navigation not in {"normal", "gentle"}:
        raise _error("unsupported_action")
    elif (
        mode in {"auto", "max"} and str(robot.info.get("label_serial", ""))[4:6] != "N2"
    ):
        raise _error("unsupported_action")
    return mode, navigation


def _clean_command(
    robot: RobotData,
    data: dict[str, Any],
    *,
    use_default_map: bool = False,
    require_dock: bool = True,
) -> dict[str, Any]:
    """Build a generation-specific cleaning command from explicit or default targets.

    Require a map for zones and keep whole-home requests separate from mapped
    cleaning. Saved target selection can optionally require the robot dock.
    """
    mode, navigation = _cleaning_settings(robot, data)
    map_id = data.get("map_id")
    zones = data.get("zone_ids", [])
    if zones and map_id is None:
        raise _error("map_required")
    if map_id and data.get("cleaning_type") == "house":
        raise _error("invalid_option")
    target = (
        cleaning_target(
            robot, data.get("cleaning_type", "auto"), require_dock=require_dock
        )
        if use_default_map and map_id is None
        else {}
    )
    if robot.generation == GEN3:
        if len(zones) > 1:
            # Upstream marks Gen3 multi-boundary payloads unverified.
            raise _error("single_zone_only")
        params: dict[str, Any] = {
            "category": 4 if map_id else target.get("category", 2),
            "mode": {"eco": 1, "turbo": 2}[mode],
            "navigationMode": {"normal": 1, "extra_care": 2, "deep": 3}[navigation],
        }
        if map_id:
            params["mapId"] = map_id
        if zones:
            params["boundaryId"] = zones[0]
        return {"cmd": "startCleaning", "params": params}
    settings = {"mode": mode, "navigation_mode": navigation}
    default_map = target.get("map")
    if isinstance(default_map, dict):
        default_map = {**default_map, "nogo_enabled": data.get("nogo_enabled", True)}
    runs = [
        {
            "map": {
                "rank_id": map_id,
                "track_id": zone,
                "nogo_enabled": data.get("nogo_enabled", True),
            }
            if map_id
            else default_map,
            "settings": settings,
        }
        for zone in zones or [None]
    ]
    return {"ability": "cleaning.start", "runs": runs}


def _gen3_schedule_events(
    hass: HomeAssistant, data: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert local weekly clock slots to the robot's UTC clock.

    Match Vacuula's current-offset conversion. Gen3 stores a fixed UTC weekly
    schedule, so local times must be written again after a DST offset change.
    Normalize a returned boundary object to the writable boundaryId. Never drop
    an unresolved or conflicting zone while replacing the complete schedule.
    Explicit UTC input preserves the robot's clock slots.
    """
    events: list[dict[str, Any]] = [dict(event) for event in data["events"]]
    for event in events:
        boundary = event.pop("boundary", None)
        if boundary is not None:
            try:
                boundary_id = _IDENTIFIER(boundary.get("id"))
            except vol.Invalid as err:
                raise _error("invalid_schedule_boundary") from err
            if "boundaryId" in event and event["boundaryId"] != boundary_id:
                raise _error("invalid_schedule_boundary")
            event["boundaryId"] = boundary_id
    if data["time_basis"] == "utc":
        return events
    offset = dt_util.now(ZoneInfo(hass.config.time_zone)).utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset else 0
    for event in events:
        hour, minute = map(int, event["startTime"].split(":"))
        day_shift, utc_minutes = divmod(hour * 60 + minute - offset_minutes, 24 * 60)
        event["day"] = (event["day"] + day_shift) % 7
        event["startTime"] = f"{utc_minutes // 60:02d}:{utc_minutes % 60:02d}"
    return events


def _merge_schedule_event(
    robot: RobotData, event: dict[str, Any], changes: dict[str, Any]
) -> dict[str, Any]:
    """Patch one freshly read Gen4 event without replacing omitted settings."""
    event = deepcopy(event)
    for key in ("enabled", "days", "time"):
        if key in changes:
            event[key] = deepcopy(changes[key])
    cleaning_fields = {
        "map_id",
        "cleaning_type",
        "zone_ids",
        "mode",
        "navigation",
        "nogo_enabled",
    }
    if not cleaning_fields.intersection(changes):
        return event
    _cleaning_settings(robot, changes)
    command = event.get("command")
    if (
        not isinstance(command, dict)
        or command.get("ability") != "cleaning.start"
        or not isinstance(runs := command.get("runs"), list)
        or not runs
        or any(
            not isinstance(run, dict)
            or not isinstance(run.get("settings"), dict)
            or (run.get("map") is not None and not isinstance(run["map"], dict))
            for run in runs
        )
    ):
        raise _error("action_unavailable")

    map_id = changes.get("map_id")
    cleaning_type = changes.get("cleaning_type")
    if cleaning_type == "house" and (map_id or changes.get("zone_ids")):
        raise _error("invalid_option")
    if map_id is not None:
        target: dict[str, Any] | None = {
            "rank_id": map_id,
            "track_id": None,
            "nogo_enabled": True,
        }
    elif cleaning_type is not None:
        target = cleaning_target(robot, cleaning_type, require_dock=False).get("map")
        if (
            target is None
            and cleaning_type == "auto"
            and any(isinstance(run.get("map"), dict) for run in runs)
        ):
            # Automatic found no promoted floorplan. Do not silently turn a
            # mapped or zoned event into whole-home cleaning; that must be an
            # explicit choice (cleaning_type: house).
            raise _error("map_unavailable")
    else:
        target = None

    if map_id is not None or cleaning_type is not None:
        if target is None:
            # Choosing whole-home cleaning deliberately removes the zone target.
            runs = [{**runs[0], "map": None}]
        else:
            if "zone_ids" not in changes and any(
                isinstance(previous := run.get("map"), dict)
                and previous.get("track_id") is not None
                and previous.get("rank_id") != target["rank_id"]
                for run in runs
            ):
                # Zone identifiers belong to their original floorplan. Never
                # silently drop them or apply them to a different floorplan.
                raise _error("schedule_zones_required")
            for run in runs:
                previous = run.get("map")
                run["map"] = {
                    **target,
                    **(previous if isinstance(previous, dict) else {}),
                    "rank_id": target["rank_id"],
                }

    if "zone_ids" in changes:
        maps = [run.get("map") for run in runs]
        if all(item is None for item in maps) and not changes["zone_ids"]:
            runs = [{**runs[0], "map": None}]
        elif (
            any(not isinstance(item, dict) for item in maps)
            or len({item.get("rank_id") for item in maps}) != 1
            or not isinstance(maps[0].get("rank_id"), str)
        ):
            raise _error("map_required")
        else:
            selected = []
            for zone in changes["zone_ids"] or [None]:
                source = next(
                    (run for run in runs if run["map"].get("track_id") == zone),
                    runs[0],
                )
                selected.append(
                    {**deepcopy(source), "map": {**source["map"], "track_id": zone}}
                )
            runs = selected

    for run in runs:
        if "mode" in changes:
            run["settings"]["mode"] = changes["mode"]
        if "navigation" in changes:
            run["settings"]["navigation_mode"] = changes["navigation"]
        if "nogo_enabled" in changes and isinstance(run.get("map"), dict):
            run["map"]["nogo_enabled"] = changes["nogo_enabled"]
    command["runs"] = runs
    return event


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register globally so action validation also works while entries are offline."""
    schemas: dict[str, dict[Any, Any]] = {
        "clean": {
            vol.Optional("map_id"): _IDENTIFIER,
            vol.Optional("cleaning_type", default="auto"): vol.In(
                ["auto", "map", "house"]
            ),
            vol.Optional("zone_ids", default=[]): vol.All(
                [_IDENTIFIER], vol.Length(max=20)
            ),
            vol.Optional("mode", default="eco"): _MODE,
            vol.Optional("navigation", default="normal"): vol.In(
                ["normal", "extra_care", "deep", "gentle"]
            ),
            vol.Optional("nogo_enabled", default=True): bool,
        },
        "set_schedule": {
            vol.Required("events"): vol.All([_GEN3_EVENT], vol.Length(max=100)),
            vol.Optional("time_basis", default="local"): vol.In(["local", "utc"]),
        },
        "update_schedule_event": {
            vol.Optional("event_id"): _IDENTIFIER,
            vol.Optional("enabled"): bool,
            vol.Optional("days"): vol.All(
                [vol.In(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])],
                vol.Length(min=1, max=7),
            ),
            vol.Optional("time"): _TIME,
            vol.Optional("map_id"): _IDENTIFIER,
            vol.Optional("cleaning_type"): vol.In(["auto", "map", "house"]),
            vol.Optional("zone_ids"): vol.All([_IDENTIFIER], vol.Length(max=20)),
            vol.Optional("mode"): _MODE,
            vol.Optional("navigation"): vol.In(["normal", "gentle"]),
            vol.Optional("nogo_enabled"): bool,
        },
        "delete_schedule_event": {vol.Required("event_id"): _IDENTIFIER},
        "set_boundaries": {
            vol.Required("map_id"): _IDENTIFIER,
            vol.Required("boundaries"): vol.All([_BOUNDARY], vol.Length(max=100)),
        },
        "rename_robot": {vol.Required("name"): _NAME},
        "update_map": {
            vol.Required("map_id"): _IDENTIFIER,
            vol.Required("name"): _NAME,
            vol.Required("enabled"): bool,
        },
        "map_action": {
            vol.Required("map_id"): _IDENTIFIER,
            vol.Required("action"): vol.In(["promote", "use", "unuse", "demote"]),
        },
        "get_data": {
            vol.Required("resource"): vol.In(
                [
                    "state",
                    "info",
                    "stats",
                    "preferences",
                    "schedule",
                    "maps",
                    "runs",
                    "map",
                    "run",
                    "boundaries",
                    "log_upload_state",
                ]
            ),
            vol.Optional("map_id"): _IDENTIFIER,
            vol.Optional("run_id"): _IDENTIFIER,
        },
        "factory_reset": {vol.Required("confirm"): bool},
    }

    async def handle(call: ServiceCall) -> ServiceResponse:
        """Dispatch a validated action and return its data or command response.

        Check map ownership before writes and translate protocol failures without
        exposing raw server response text.
        """
        coordinator, robot = _resolve(hass, call.data["device_id"])
        data = dict(call.data)
        serial = robot.serial
        action = call.service
        try:
            result: Any
            if action in {"set_boundaries", "update_map", "map_action"}:
                # Older servers do not scope every DB update by robot. Verify
                # ownership using the per-robot map file before sending a write.
                await coordinator.client.async_map(serial, data["map_id"])
            if action == "clean":
                result = await coordinator.async_command(
                    serial,
                    lambda current: _clean_command(current, data, use_default_map=True),
                    require_map_recovery=data["cleaning_type"] != "house",
                    refresh_maps=data["cleaning_type"] != "house",
                )
            elif action == "set_schedule":
                _require_generation(robot, GEN3)
                result = await coordinator.async_command(
                    serial,
                    {
                        "cmd": "setSchedule",
                        "params": {"events": _gen3_schedule_events(hass, data)},
                    },
                )
            elif action == "update_schedule_event":
                _require_generation(robot, GEN4)
                if "event_id" in data:
                    refresh_maps = (
                        "map_id" in data or "cleaning_type" in data
                    ) and data.get("cleaning_type") != "house"

                    def update_event(event: dict[str, Any]) -> dict[str, Any]:
                        """Patch the current event under the map recovery guard."""
                        if (
                            data.get("cleaning_type") in {"auto", "map"}
                            or "map_id" in data
                        ):
                            coordinator.validate_map_recovery(serial)
                        event = _merge_schedule_event(robot, event, data)
                        command = event.get("command", {})
                        runs = (
                            command.get("runs") if isinstance(command, dict) else None
                        )
                        if isinstance(runs, list) and any(
                            isinstance(run, dict) and isinstance(run.get("map"), dict)
                            for run in runs
                        ):
                            coordinator.validate_map_recovery(serial)
                        return event

                    result = await coordinator.async_update_schedule_event(
                        serial,
                        data["event_id"],
                        update_event,
                        refresh_maps=refresh_maps,
                    )
                    return {"response": result}
                if not {"enabled", "days", "time"}.issubset(data):
                    raise _error("invalid_schedule_event")
                cleaning_type = data.get("cleaning_type", "auto")

                def create_event(current: RobotData) -> dict[str, Any]:
                    """Select an event's map and no-go defaults under the lock."""
                    command = _clean_command(
                        current, data, use_default_map=True, require_dock=False
                    )
                    return {
                        "ability": "scheduling.update",
                        "event": {
                            "enabled": data["enabled"],
                            "days": data["days"],
                            "time": data["time"],
                            "command": {
                                **command,
                                "auto_localization": False,
                                "force_floorplan": False,
                            },
                        },
                    }

                result = await coordinator.async_command(
                    serial,
                    create_event,
                    require_map_recovery=cleaning_type != "house",
                    refresh_maps=cleaning_type != "house",
                )
            elif action == "delete_schedule_event":
                _require_generation(robot, GEN4)
                result = await coordinator.async_command(
                    serial, {"ability": "scheduling.delete", "id": data["event_id"]}
                )
            elif action == "set_boundaries":
                for boundary in data["boundaries"]:
                    if boundary["type"] == "polygon" and len(boundary["vertices"]) < 3:
                        raise _error("invalid_boundary")
                    if robot.generation == GEN4 and "function" not in boundary:
                        raise _error("invalid_boundary")
                    if robot.generation == GEN4 and "relevancy" in boundary:
                        raise _error("invalid_boundary")
                    if (
                        robot.generation == GEN3
                        and boundary["type"] == "polygon"
                        and "relevancy" not in boundary
                    ):
                        # The Vacuula app computes this as the vertex average
                        # (components/map/utils.ts calcRelevancy).
                        vertices = boundary["vertices"]
                        boundary["relevancy"] = [
                            sum(point[0] for point in vertices) / len(vertices),
                            sum(point[1] for point in vertices) / len(vertices),
                        ]
                if robot.generation == GEN3:
                    result = await coordinator.async_command(
                        serial,
                        {
                            "cmd": "setMapBoundaries",
                            "params": {
                                "mapId": data["map_id"],
                                "boundaries": data["boundaries"],
                            },
                        },
                    )
                else:
                    result = await coordinator.async_set_map_boundaries(
                        serial, data["map_id"], data["boundaries"]
                    )
            elif action == "rename_robot":
                await coordinator.client.async_update_robot(serial, data["name"])
                robot.name = data["name"]
                result = {}
                dr.async_get(hass).async_update_device(
                    data["device_id"], name=data["name"]
                )
                coordinator.async_update_listeners()
            elif action == "update_map":
                await coordinator.async_update_map(
                    serial, data["map_id"], data["name"], data["enabled"]
                )
                result = {}
            elif action == "map_action":
                _require_generation(robot, GEN4)
                result = await coordinator.async_map_operation(
                    serial, data["map_id"], data["action"], None
                )
            elif action == "factory_reset":
                _require_generation(robot, GEN4)
                if data["confirm"] is not True:
                    raise _error("confirmation_required")
                result = await coordinator.async_command(
                    serial, {"ability": "settings.factory_reset"}
                )
            else:
                return {"data": await _get_data(coordinator, robot, data)}
            if action == "map_action" and data["action"] != "promote":
                # The server applied the action; a failed follow-up read must
                # not report it as failed. The next poll catches up.
                with suppress(HomeAssistantError):
                    await coordinator.async_refresh_maps(serial)
            return {"response": result}
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err

    for action, fields in schemas.items():
        async_register_admin_service(
            hass,
            DOMAIN,
            action,
            handle,
            schema=vol.Schema({**_TARGET, **fields}),
            supports_response=SupportsResponse.ONLY
            if action == "get_data"
            else SupportsResponse.OPTIONAL,
        )


async def _get_data(
    coordinator: VacuulaCoordinator, robot: RobotData, data: dict[str, Any]
) -> Any:
    """Read the requested robot or server resource without unrelated refreshes."""
    resource = data["resource"]
    serial = robot.serial
    if resource == "maps":
        return await coordinator.client.async_maps(serial)
    if resource == "runs":
        return await coordinator.client.async_runs(serial)
    if resource in {"map", "boundaries"}:
        if "map_id" not in data:
            raise _error("map_required")
        if resource == "map":
            return await coordinator.client.async_map(serial, data["map_id"])
        if robot.generation == GEN3:
            return response_data(
                await coordinator.async_command(
                    serial,
                    {"cmd": "getMapBoundaries", "params": {"mapId": data["map_id"]}},
                    refresh=False,
                )
            )
        return await coordinator.client.async_map_boundaries(serial, data["map_id"])
    if resource == "run":
        if "run_id" not in data:
            raise _error("run_required")
        return await coordinator.client.async_run(serial, data["run_id"])
    commands = {
        GEN3: {
            "state": "getRobotState",
            "info": "getGeneralInfo",
            "stats": "getLocalStats",
            "preferences": "getPreferences",
            "schedule": "getSchedule",
            "log_upload_state": "getLogsUploadState",
        },
        GEN4: {
            "state": "state.show",
            "info": "info.robot",
            "stats": "cleaning.show",
            "preferences": "settings.cleaning_options",
            "schedule": "scheduling.show",
        },
    }
    if resource not in commands[robot.generation]:
        raise _error("unsupported_action")
    return response_data(
        await coordinator.async_command(
            serial,
            {
                "cmd" if robot.generation == GEN3 else "ability": commands[
                    robot.generation
                ][resource]
            },
            refresh=False,
        )
    )
