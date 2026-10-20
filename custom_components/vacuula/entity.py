"""Shared robot identity, availability and late discovery."""

from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
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


def gen4_error_codes(robot: RobotData) -> list[str] | None:
    """Return Gen4 error codes, skipping informational notices.

    The robot's errors list also carries status notices such as charging on
    the base or returning to base. The Vacuula app labels their severity
    "invalid" as "Info"; they are not problems. None means not reported.
    """
    if "errors" not in robot.state:
        return None
    errors = robot.state.get("errors")
    if errors is None:
        return []
    if not isinstance(errors, list) or any(
        not isinstance(item, dict) for item in errors
    ):
        return None
    return [
        str(item.get("code", ""))
        for item in errors
        if item.get("severity") != "invalid"
    ]


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
    # Shared through the coordinator per platform module and robot, so a robot
    # whose device was removed, or a deleted record, can be added again later.
    known = coordinator.known_entities.setdefault(factory.__module__, {})

    @callback
    def discover() -> None:
        entities: list[Entity] = []
        for robot in coordinator.robots.values():
            if not robot.listed:
                continue
            added = known.setdefault(robot.serial, set())
            for entity in factory(coordinator, robot):
                if (
                    unique_id := entity.unique_id
                ) is not None and unique_id not in added:
                    added.add(unique_id)
                    entities.append(entity)
        if entities:
            async_add_entities(entities)

    discover()
    entry.async_on_unload(coordinator.async_add_listener(discover))


@callback
def async_remove_stale_entity(
    entity: CoordinatorEntity[VacuulaCoordinator], serial: str
) -> None:
    """Remove an entity whose server record was deleted; it can return later."""
    known = entity.coordinator.known_entities.get(type(entity).__module__, {})
    if entity.unique_id is not None:
        known.get(serial, set()).discard(entity.unique_id)
    if entity.registry_entry is not None:
        er.async_get(entity.hass).async_remove(entity.entity_id)


def set_name_placeholders(entity: Entity, placeholders: dict[str, str]) -> None:
    """Update translated name placeholders and drop HA's cached entity name.

    Home Assistant caches Entity.name and only invalidates it when _attr_name
    changes, so a renamed map or edited event would otherwise keep its old name
    until the integration is reloaded.
    """
    if getattr(entity, "_attr_translation_placeholders", None) == placeholders:
        return
    entity._attr_translation_placeholders = placeholders
    entity.__dict__.pop("name", None)


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
