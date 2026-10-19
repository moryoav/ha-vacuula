"""Connection, charging, command readiness and robot problem telemetry."""

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities, value_at
from .telemetry import key_and_label, scalars, source_data

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VacuulaBinaryDescription(BinarySensorEntityDescription):
    source: str = "state"
    gen3_path: tuple[str, ...] = ()
    gen4_path: tuple[str, ...] = ()


DESCRIPTIONS = (
    VacuulaBinaryDescription(
        key="connected",
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        gen3_path=("details", "isCharging"),
        gen4_path=("details", "is_charging"),
    ),
    VacuulaBinaryDescription(
        key="docked",
        translation_key="docked",
        gen3_path=("details", "isDocked"),
        gen4_path=("details", "is_docked"),
    ),
    VacuulaBinaryDescription(
        key="dock_seen",
        translation_key="dock_seen",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        gen3_path=("details", "dockHasBeenSeen"),
    ),
    VacuulaBinaryDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaBinaryDescription(
        key="dustbin_missing",
        translation_key="dustbin_missing",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VacuulaBinaryDescription(
        key="dustbin_full",
        translation_key="dustbin_full",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    VacuulaBinaryDescription(
        key="quickboost",
        translation_key="quickboost",
        gen4_path=("details", "is_quickboost"),
    ),
    VacuulaBinaryDescription(
        key="started_on_base",
        translation_key="started_on_base",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        gen4_path=("autonomy_states", "started_on_base"),
    ),
    VacuulaBinaryDescription(
        key="extracting",
        translation_key="extracting",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        gen4_path=("cleaning_center", "is_extracting"),
    ),
    *(
        VacuulaBinaryDescription(
            key=f"can_{name}",
            translation_key=f"can_{name}",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            gen3_path=("availableCommands", gen3),
            gen4_path=("available_commands", gen4),
        )
        for name, gen3, gen4 in (
            ("start", "start", "start"),
            ("stop", "stop", "cancel"),
            ("pause", "pause", "pause"),
            ("resume", "resume", "resume"),
            ("return_to_base", "goToBase", "return_to_base"),
        )
    ),
)


def binary_value(
    robot: RobotData, description: VacuulaBinaryDescription
) -> bool | None:
    """Read explicit firmware booleans; absent telemetry stays unknown."""
    key = description.key
    if key == "connected":
        return robot.connected
    if description.source != "state":
        path = (
            description.gen3_path if robot.generation == GEN3 else description.gen4_path
        )
        value = value_at(source_data(robot, description.source), *path)
        return value if isinstance(value, bool) else None
    if not robot.state:
        return None
    if key == "problem":
        if ("error" if robot.generation == GEN3 else "errors") not in robot.state:
            return None
        error = (
            robot.state.get("error")
            if robot.generation == GEN3
            else robot.state.get("errors")
        )
        if robot.generation == GEN3:
            return bool(error) if error is None or isinstance(error, str) else None
        return (
            bool(error)
            if error is None
            or isinstance(error, list)
            and all(isinstance(item, dict) for item in error)
            else None
        )
    if key == "dustbin_missing":
        if "error" not in robot.state:
            return None
        if robot.state["error"] is not None and not isinstance(
            robot.state["error"], str
        ):
            return None
        return (
            robot.state.get("error") == "dustbin_missing"
            if robot.generation == GEN3
            else None
        )
    if key == "dustbin_full":
        if "alert" not in robot.state:
            return None
        if robot.state["alert"] is not None and not isinstance(
            robot.state["alert"], str
        ):
            return None
        return (
            robot.state.get("alert") == "dustbin_full"
            if robot.generation == GEN3
            else None
        )
    path = description.gen3_path if robot.generation == GEN3 else description.gen4_path
    value = value_at(robot.state, *path) if path else None
    return value if isinstance(value, bool) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaBinarySensor]:
    descriptions = [
        description
        for description in DESCRIPTIONS
        if binary_value(robot, description) is not None
    ]
    represented = {
        (desc.source, desc.gen3_path if robot.generation == GEN3 else desc.gen4_path)
        for desc in DESCRIPTIONS
    }
    for source in ("state", "info", "stats", "active_run", "last_run", "last_subrun"):
        for path, value in scalars(source_data(robot, source)):
            if not isinstance(value, bool) or (source, path) in represented:
                continue
            key, label = key_and_label(source, path)
            descriptions.append(
                VacuulaBinaryDescription(
                    key=key,
                    translation_key="telemetry",
                    translation_placeholders={"label": label},
                    source=source,
                    gen3_path=path,
                    gen4_path=path,
                    entity_registry_enabled_default=False,
                    entity_category=EntityCategory.DIAGNOSTIC,
                )
            )
    return [
        VacuulaBinarySensor(coordinator, robot.serial, description)
        for description in descriptions
    ]


class VacuulaBinarySensor(VacuulaEntity, BinarySensorEntity):
    entity_description: VacuulaBinaryDescription

    def __init__(
        self,
        coordinator: VacuulaCoordinator,
        serial: str,
        description: VacuulaBinaryDescription,
    ) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.key == "connected":
            return self.coordinator.last_update_success
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        return binary_value(self.robot, self.entity_description)
