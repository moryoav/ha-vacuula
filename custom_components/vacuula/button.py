"""Convenient buttons for standard controls and maintenance actions."""

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities
from .vacuum import VacuulaVacuum

PARALLEL_UPDATES = 0

# Return to base is not a plain command: it uses the coordinator's
# pause-then-home sequence, like the vacuum entity.
COMMANDS = {
    "stop": ("stopCleaning", "cleaning.cancel", "stop", "cancel"),
    "pause": ("pauseCleaning", "cleaning.pause", "pause", "pause"),
    "resume": ("resumeCleaning", "cleaning.resume", "resume", "resume"),
    "locate": ("findMe", "utilities.find_me", None, None),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register control buttons and discover capabilities reported after setup."""
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaButton]:
    """Offer standard controls and maintenance actions supported by this robot."""
    keys = ["start", "stop", "pause", "resume", "return_to_base", "refresh"]
    if robot.generation != GEN3 or robot.capabilities.get("findMe"):
        keys.append("locate")
    if robot.generation == GEN3:
        keys.append("dismiss_alert")
        if robot.capabilities.get("spotCleaning"):
            keys.append("clean_spot")
        if robot.capabilities.get("maps"):
            keys.append("explore")
        if robot.capabilities.get("logCopy"):
            keys.append("upload_logs")
    return [VacuulaButton(coordinator, robot.serial, key) for key in keys]


class VacuulaButton(VacuulaEntity, ButtonEntity):
    """Expose one robot action with firmware-aware availability checks."""

    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
        """Assign the action identity, translated name and configuration category."""
        super().__init__(coordinator, serial, f"button_{key}")
        self.key = key
        self._attr_translation_key = key
        if key in ("refresh", "dismiss_alert", "upload_logs"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "upload_logs":
            self._attr_entity_registry_enabled_default = False
        elif key == "explore":
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def available(self) -> bool:
        """Keep supported actions callable so stale readiness can be checked live."""
        if self.key == "refresh":
            return self.coordinator.last_update_success
        return super().available

    async def async_press(self) -> None:
        """Run the selected control through validated coordinator actions."""
        if not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        if self.key == "refresh":
            if not await self.coordinator.async_refresh_robot(self.serial, full=True):
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                )
        elif self.key == "start":
            await VacuulaVacuum(self.coordinator, self.serial).async_start()
        elif self.key == "clean_spot":
            await VacuulaVacuum(self.coordinator, self.serial).async_clean_spot()
        elif self.key == "return_to_base":
            await VacuulaVacuum(self.coordinator, self.serial).async_return_to_base()
        elif self.key == "upload_logs":
            await self.coordinator.async_command(
                self.serial, {"cmd": "uploadLogs", "params": {"recentCount": 1}}
            )
        elif self.key in COMMANDS:
            await VacuulaVacuum(self.coordinator, self.serial)._command(
                *COMMANDS[self.key]
            )
        elif self.key == "explore":

            def build_command(robot: RobotData) -> dict[str, Any]:
                """Recheck exploration readiness under the robot command lock."""
                if (
                    self.detail("isDocked", "is_docked") is not True
                    or self.command_allowed("start", "start") is False
                ):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="action_unavailable"
                    )
                return {"cmd": "startPersistentMapExploration"}

            await self.coordinator.async_command(self.serial, build_command)
        else:
            await self.coordinator.async_command(
                self.serial, {"cmd": "dismissCurrentAlert"}
            )
