"""Shared robot identity, availability and late discovery."""

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_URL, DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData


def value_at(data: dict[str, Any], *path: str) -> Any:
    """Read an optional nested field without inventing a fallback value."""
    value: Any = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def number(value: Any) -> int | float | None:
    """Accept numeric telemetry, excluding booleans and invalid floats."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


@callback
def async_add_robot_entities(
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[VacuulaCoordinator, RobotData], Iterable[Entity]],
) -> None:
    """Discover entities again after pushes, including newly supported telemetry."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def discover() -> None:
        entities: list[Entity] = []
        for robot in coordinator.robots.values():
            for entity in factory(coordinator, robot):
                if (
                    unique_id := entity.unique_id
                ) is not None and unique_id not in known:
                    known.add(unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


class VacuulaEntity(CoordinatorEntity[VacuulaCoordinator]):
    """An entity belonging to one stable robot serial."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator)
        self.serial = serial
        self._attr_unique_id = f"{serial}_{key}"

    @property
    def robot(self) -> RobotData:
        """Return the current coordinator robot, including replacements."""
        return self.coordinator.robots[self.serial]

    @property
    def available(self) -> bool:
        return super().available and self.robot.connected

    @property
    def device_info(self) -> DeviceInfo:
        robot = self.robot
        return DeviceInfo(
            identifiers={(DOMAIN, self.serial)},
            manufacturer="Neato",
            model=robot.info.get("model") or value_at(robot.state, "meta", "modelName"),
            name=robot.name,
            serial_number=self.serial,
            sw_version=robot.info.get("firmware")
            or value_at(robot.state, "meta", "firmware"),
            configuration_url=self.coordinator.entry.data[CONF_URL].rstrip("/")
            + "/web",
        )

    def detail(self, gen3_key: str, gen4_key: str) -> Any:
        return value_at(
            self.robot.state,
            "details",
            gen3_key if self.robot.generation == GEN3 else gen4_key,
        )

    def command_allowed(self, gen3_key: str, gen4_key: str) -> bool | None:
        """Return None when firmware does not advertise availability."""
        key = (
            "availableCommands"
            if self.robot.generation == GEN3
            else "available_commands"
        )
        result = value_at(
            self.robot.state,
            key,
            gen3_key if self.robot.generation == GEN3 else gen4_key,
        )
        return result if isinstance(result, bool) else None
