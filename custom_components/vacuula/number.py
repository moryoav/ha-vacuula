"""Supported reminder intervals and future spot-clean dimensions."""

from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities, number

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VacuulaNumberDescription(NumberEntityDescription):
    preference: str | None = None
    divisor: int = 1
    default: float | None = None


DESCRIPTIONS = (
    VacuulaNumberDescription(
        key="bin_reminder",
        translation_key="bin_reminder",
        preference="dirtbinAlertReminderInterval",
        native_min_value=30,
        native_max_value=150,
        native_step=30,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    VacuulaNumberDescription(
        key="filter_reminder",
        translation_key="filter_reminder",
        preference="filterChangeReminderInterval",
        divisor=1440,
        native_min_value=30,
        native_max_value=90,
        native_step=15,
        native_unit_of_measurement=UnitOfTime.DAYS,
    ),
    VacuulaNumberDescription(
        key="brush_reminder",
        translation_key="brush_reminder",
        preference="brushChangeReminderInterval",
        divisor=1440,
        native_min_value=120,
        native_max_value=240,
        native_step=30,
        native_unit_of_measurement=UnitOfTime.DAYS,
    ),
    *(
        VacuulaNumberDescription(
            key=key,
            translation_key=key,
            default=200,
            native_min_value=100,
            native_max_value=400,
            native_step=1,
            native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        )
        for key in ("spot_height", "spot_width")
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaNumber]:
    if robot.generation != GEN3:
        return []
    return [
        VacuulaNumber(coordinator, robot.serial, desc)
        for desc in DESCRIPTIONS
        if (
            desc.preference is not None
            and number(robot.preferences.get(desc.preference)) is not None
        )
        or (desc.preference is None and robot.capabilities.get("spotCleaning"))
    ]


class VacuulaNumber(VacuulaEntity, NumberEntity):
    entity_description: VacuulaNumberDescription
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: VacuulaCoordinator,
        serial: str,
        description: VacuulaNumberDescription,
    ) -> None:
        super().__init__(coordinator, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        desc = self.entity_description
        if desc.preference is None:
            value = number(
                self.coordinator.cleaning_options.get(self.serial, {}).get(
                    desc.key, desc.default
                )
            )
        else:
            value = number(self.robot.preferences.get(desc.preference))
        return float(value) / desc.divisor if value is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        desc = self.entity_description
        minimum = cast(float, desc.native_min_value)
        maximum = cast(float, desc.native_max_value)
        step = cast(float, desc.native_step)
        if (
            number(value) is None
            or not minimum <= value <= maximum
            or abs((value - minimum) / step - round((value - minimum) / step)) > 1e-8
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_option"
            )
        if desc.preference is None:
            await self.coordinator.async_set_cleaning_option(
                self.serial, desc.key, int(value)
            )
        else:
            command: dict[str, Any] = {
                "cmd": "setPreferences",
                "params": {desc.preference: int(value * desc.divisor)},
            }
            await self.coordinator.async_command(self.serial, command)
