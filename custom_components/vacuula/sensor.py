"""Confirmed robot telemetry, battery details and compact latest-run sensors."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfLength,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import (
    VacuulaEntity,
    async_add_robot_entities,
    gen4_error_codes,
    number,
    value_at,
)
from .telemetry import key_and_label, latest_record, scalars, source_data

PARALLEL_UPDATES = 0

STATE_NAMES = {0: "invalid", 1: "idle", 2: "busy", 3: "paused", 4: "error"}
ACTION_NAMES = {
    0: "invalid",
    1: "house_cleaning",
    2: "spot_cleaning",
    3: "manual_cleaning",
    4: "docking",
    5: "user_menu_active",
    6: "suspended_cleaning",
    7: "updating",
    8: "copying_logs",
    9: "recovering_location",
    11: "map_cleaning",
    12: "exploring_map",
    13: "acquiring_persistent_map_ids",
    14: "creating_uploading_map",
    15: "suspended_exploration",
}
AUTONOMY_KEYS = (
    "active_cleaning_after_suspended",
    "active_cleaning_session",
    "cleaning_start",
    "docking",
    "docking_for_suspended",
    "docking_successful",
    "docking_successful_suspended",
    "docking_verify_base",
    "suspended_charging_start",
    "undocking",
    "undocking_after_suspended",
)


@dataclass(frozen=True, kw_only=True)
class VacuulaSensorDescription(SensorEntityDescription):
    source: str = "state"
    gen3_path: tuple[str, ...] = ()
    gen4_path: tuple[str, ...] = ()
    kind: str = "number"


DESCRIPTIONS = (
    VacuulaSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        gen3_path=("details", "charge"),
        gen4_path=("details", "charge"),
    ),
    VacuulaSensorDescription(
        key="robot_state",
        translation_key="robot_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(STATE_NAMES.values()),
        gen3_path=("state",),
        gen4_path=("state",),
        kind="state",
    ),
    VacuulaSensorDescription(
        key="robot_action",
        translation_key="robot_action",
        entity_category=EntityCategory.DIAGNOSTIC,
        gen3_path=("action",),
        gen4_path=("action",),
        kind="action",
    ),
    VacuulaSensorDescription(
        key="error",
        translation_key="error",
        entity_category=EntityCategory.DIAGNOSTIC,
        kind="error",
    ),
    VacuulaSensorDescription(
        key="alert",
        translation_key="alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        gen3_path=("alert",),
        kind="alert",
    ),
    VacuulaSensorDescription(
        key="active_cleaning_mode",
        translation_key="active_cleaning_mode",
        gen3_path=("cleaning", "mode"),
        kind="mode",
    ),
    VacuulaSensorDescription(
        key="active_navigation_mode",
        translation_key="active_navigation_mode",
        gen3_path=("cleaning", "navigationMode"),
        kind="navigation",
    ),
    VacuulaSensorDescription(
        key="active_cleaning_category",
        translation_key="active_cleaning_category",
        gen3_path=("cleaning", "category"),
        kind="category",
    ),
    VacuulaSensorDescription(
        key="active_cleaning_modifier",
        translation_key="active_cleaning_modifier",
        gen3_path=("cleaning", "modifier"),
        kind="modifier",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *(
        VacuulaSensorDescription(
            key=f"active_spot_{dimension}",
            translation_key=f"active_spot_{dimension}",
            gen3_path=("cleaning", f"spot{dimension.title()}"),
            native_unit_of_measurement=UnitOfLength.CENTIMETERS,
            entity_registry_enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for dimension in ("height", "width")
    ),
    VacuulaSensorDescription(
        key="time_to_empty",
        translation_key="time_to_empty",
        source="info",
        gen3_path=("battery", "timeToEmpty"),
        kind="battery_time",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="time_to_full_charge",
        entity_registry_enabled_default=False,
        translation_key="time_to_full_charge",
        source="info",
        gen3_path=("battery", "timeToFullCharge"),
        kind="battery_time",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="battery_total_charges",
        translation_key="battery_total_charges",
        source="info",
        gen3_path=("battery", "totalCharges"),
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="battery_manufacturing_date",
        translation_key="battery_manufacturing_date",
        source="info",
        gen3_path=("battery", "manufacturingDate"),
        device_class=SensorDeviceClass.DATE,
        kind="date",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="battery_vendor",
        translation_key="battery_vendor",
        source="info",
        gen3_path=("battery", "vendor"),
        kind="text",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="battery_authorization",
        translation_key="battery_authorization",
        source="info",
        gen3_path=("battery", "authorizationStatus"),
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="product_number",
        translation_key="product_number",
        source="info",
        gen3_path=("productNumber",),
        kind="text",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="base_type",
        translation_key="base_type",
        gen4_path=("details", "base_type"),
        kind="text",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="bag_status",
        translation_key="bag_status",
        gen4_path=("cleaning_center", "bag_status"),
        kind="text",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="base_error",
        translation_key="base_error",
        gen4_path=("cleaning_center", "base_error"),
        kind="text",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    *(
        VacuulaSensorDescription(
            key=f"autonomy_{key}",
            translation_key="autonomy",
            translation_placeholders={"label": key.replace("_", " ")},
            gen4_path=("autonomy_states", key),
            entity_registry_enabled_default=False,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for key in AUTONOMY_KEYS
    ),
    VacuulaSensorDescription(
        key="map_count",
        translation_key="map_count",
        kind="map_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="run_count",
        translation_key="run_count",
        kind="run_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="schedule_event_count",
        translation_key="schedule_event_count",
        kind="schedule_count",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="last_run_area",
        translation_key="last_run_area",
        kind="last_area",
        device_class=SensorDeviceClass.AREA,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
    ),
    VacuulaSensorDescription(
        key="last_run_duration",
        translation_key="last_run_duration",
        kind="last_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
    VacuulaSensorDescription(
        key="last_run_started",
        translation_key="last_run_started",
        kind="last_started",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    VacuulaSensorDescription(
        key="last_run_finished",
        translation_key="last_run_finished",
        kind="last_finished",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    VacuulaSensorDescription(
        key="last_run_status", translation_key="last_run_status", kind="last_status"
    ),
    VacuulaSensorDescription(
        key="last_run_started_by",
        translation_key="last_run_started_by",
        kind="last_started_by",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="last_run_battery_used",
        translation_key="last_run_battery_used",
        kind="last_battery",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaSensorDescription(
        key="last_run_pickups",
        translation_key="last_run_pickups",
        kind="last_pickups",
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def parse_timestamp(value: Any) -> datetime | None:
    """Accept only timestamps carrying an unambiguous timezone."""
    parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _text(value: Any) -> str | None:
    return value[:255] if isinstance(value, str) else None


def latest_run(robot: RobotData) -> dict[str, Any] | None:
    """Find the latest run even when the API changes its ordering."""
    return latest_record(robot)


def last_value(robot: RobotData, kind: str) -> Any:
    latest = latest_run(robot)
    if latest is None:
        return None
    run = latest.get("run", latest)
    gen3 = robot.generation == GEN3
    started = parse_timestamp(
        run.get("start_at") if gen3 else value_at(run, "timing", "start")
    )
    finished = parse_timestamp(
        run.get("end_at") if gen3 else value_at(run, "timing", "end")
    )
    if kind == "last_started":
        return started
    if kind == "last_finished":
        return finished
    if kind == "last_duration":
        return (
            (finished - started).total_seconds()
            if started is not None and finished is not None and finished >= started
            else None
        )
    if kind == "last_started_by":
        return _text(run.get("launched_from") if gen3 else latest.get("started_by"))
    if kind == "last_status":
        if gen3:
            return _text(run.get("status"))
        complete = latest.get("cleaning_complete")
        runs = latest.get("runs")
        return (
            "done"
            if complete is True
            else (
                _text(value_at(runs[-1], "state"))
                if isinstance(runs, list) and runs and isinstance(runs[-1], dict)
                else None
            )
        )
    if kind == "last_battery":
        if gen3:
            start = number(run.get("run_charge_at_start"))
            end = number(run.get("run_charge_at_end"))
            return (
                start - end
                if start is not None and end is not None and start >= end
                else None
            )
        return number(latest.get("battery_consumed"))
    if gen3:
        return number(
            run.get("cleaned_area" if kind == "last_area" else "picked_up_count")
        )
    runs = latest.get("runs")
    if not isinstance(runs, list) or any(not isinstance(item, dict) for item in runs):
        return None
    values = [
        number(
            value_at(item, "stats", "area" if kind == "last_area" else "pickup_count")
        )
        for item in runs
    ]
    return (
        sum(value for value in values if value is not None)
        if values and all(value is not None for value in values)
        else None
    )


def sensor_value(robot: RobotData, desc: VacuulaSensorDescription) -> Any:
    kind = desc.kind
    if kind.startswith("last_"):
        return last_value(robot, kind)
    if kind in ("map_count", "run_count"):
        available = robot.capabilities.get(
            "maps_available" if kind == "map_count" else "runs_available"
        )
        return (
            len(robot.maps if kind == "map_count" else robot.runs)
            if available
            else None
        )
    if kind == "schedule_count":
        events = robot.schedule.get("events")
        return len(events) if isinstance(events, list) else None
    if kind in ("error", "alert"):
        if robot.generation == GEN3:
            # Gen3 reports one error and one alert; null means none is active.
            value = robot.state.get(kind)
            return (
                (value or "none")[:255]
                if kind in robot.state and (value is None or isinstance(value, str))
                else None
            )
        if kind == "alert":
            # Gen4 reports alerts inside its errors list, shown by Error.
            return None
        codes = gen4_error_codes(robot)
        if codes is None:
            return None
        return ", ".join(codes)[:255] if codes else "none"
    path = desc.gen3_path if robot.generation == GEN3 else desc.gen4_path
    raw = value_at(source_data(robot, desc.source), *path) if path else None
    if kind in (
        "text",
        "action",
        "state",
        "mode",
        "navigation",
        "category",
        "modifier",
    ):
        if kind == "action" and robot.generation == GEN3:
            return ACTION_NAMES.get(raw) if type(raw) is int else None
        if kind == "state" and robot.generation == GEN3:
            return STATE_NAMES.get(raw) if type(raw) is int else None
        if kind == "state":
            return raw if isinstance(raw, str) and raw in STATE_NAMES.values() else None
        mapping = {
            "mode": {1: "eco", 2: "turbo"},
            "navigation": {1: "normal", 2: "extra_care", 3: "deep"},
            "category": {1: "manual", 2: "house", 3: "spot", 4: "map"},
            "modifier": {1: "normal", 2: "double"},
        }
        if kind in mapping:
            return mapping[kind].get(raw) if type(raw) is int else None
        return (
            str(raw)[:255]
            if isinstance(raw, (str, int, float)) and not isinstance(raw, bool)
            else None
        )
    if kind == "date":
        try:
            return date(*(int(part) for part in str(raw).split("-")))
        except ValueError, TypeError:
            return None
    value = number(raw)
    return None if kind == "battery_time" and value == 65535 else value


def scalar_stats(
    data: dict[str, Any], path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Expose scalar local statistics without copying arbitrary arrays into state."""
    for key, value in data.items():
        current = (*path, key)
        if isinstance(value, dict) and len(current) < 4:
            yield from scalar_stats(value, current)
        elif (
            value is not None
            and isinstance(value, (str, int, float))
            and not isinstance(value, bool)
        ):
            yield current, value


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaSensor]:
    descriptions = [
        desc for desc in DESCRIPTIONS if sensor_value(robot, desc) is not None
    ]
    represented = {
        (desc.source, desc.gen3_path if robot.generation == GEN3 else desc.gen4_path)
        for desc in DESCRIPTIONS
    }
    for source in ("state", "info", "stats", "active_run", "last_run", "last_subrun"):
        for path, value in scalars(source_data(robot, source)):
            if isinstance(value, bool) or (source, path) in represented:
                continue
            key, label = key_and_label(source, path)
            descriptions.append(
                VacuulaSensorDescription(
                    key=key,
                    translation_key="telemetry",
                    translation_placeholders={"label": label},
                    source=source,
                    gen3_path=path,
                    gen4_path=path,
                    kind="text" if isinstance(value, str) else "number",
                    entity_registry_enabled_default=False,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )
    return [VacuulaSensor(coordinator, robot.serial, desc) for desc in descriptions]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


class VacuulaSensor(VacuulaEntity, SensorEntity):
    entity_description: VacuulaSensorDescription

    def __init__(
        self,
        coordinator: VacuulaCoordinator,
        serial: str,
        description: VacuulaSensorDescription,
    ) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return sensor_value(self.robot, self.entity_description)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None
