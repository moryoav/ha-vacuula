"""Future cleaning defaults and the robot's advertised language choices."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities, value_at
from .vacuum import cleaning_mode, fan_modes

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaSelect]:
    keys = ["cleaning_mode", "navigation_mode", "cleaning_category"]
    if robot.generation != GEN3 and robot.preferences.get("available_languages"):
        keys.append("robot_language")
    return [VacuulaSelect(coordinator, robot.serial, key) for key in keys]


class VacuulaSelect(VacuulaEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator, serial, key)
        self.key = key
        self._attr_translation_key = key

    @property
    def available(self) -> bool:
        if self.key == "robot_language":
            return super().available
        # Cleaning defaults are stored in Home Assistant and never sent to the
        # robot when changed, so they stay editable while it is offline.
        return self.coordinator.last_update_success and self.robot.listed

    @property
    def options(self) -> list[str]:
        if self.key == "cleaning_mode":
            return fan_modes(self.robot)
        if self.key == "navigation_mode":
            return (
                ["normal", "extra_care", "deep"]
                if self.robot.generation == GEN3
                else ["normal", "gentle"]
            )
        if self.key == "cleaning_category":
            options = ["auto", "house"]
            if (
                self.robot.generation != GEN3
                or self.robot.capabilities.get("maps")
                or self.robot.maps
            ):
                options.append("map")
            if self.robot.generation == GEN3 and self.robot.capabilities.get(
                "spotCleaning"
            ):
                options.append("spot")
            return options
        return [
            item
            for item in self.robot.preferences.get("available_languages", [])
            if isinstance(item, str)
        ]

    @property
    def current_option(self) -> str | None:
        defaults = self.coordinator.cleaning_options.get(self.serial, {})
        if self.key == "cleaning_mode":
            return cleaning_mode(self.coordinator, self.serial)
        if self.key == "navigation_mode":
            if configured := defaults.get("navigation_mode"):
                return configured if configured in self.options else None
            if self.robot.generation == GEN3:
                return {1: "normal", 2: "extra_care", 3: "deep"}.get(
                    value_at(self.robot.state, "cleaning", "navigationMode"), "normal"
                )
            return "normal"
        if self.key == "cleaning_category":
            value = defaults.get("category", "auto")
        else:
            value = self.robot.preferences.get("language")
        return value if isinstance(value, str) and value in self.options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_option"
            )
        if self.key == "robot_language":
            await self.coordinator.async_command(
                self.serial,
                {"ability": "settings.set_robot_language", "language": option},
            )
            return
        key = {"cleaning_mode": "mode", "cleaning_category": "category"}.get(
            self.key, self.key
        )
        await self.coordinator.async_set_cleaning_option(self.serial, key, option)
