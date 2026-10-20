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

COMMANDS = {
    "stop": ("stopCleaning", "cleaning.cancel", "stop", "cancel"),
    "pause": ("pauseCleaning", "cleaning.pause", "pause", "pause"),
    "resume": ("resumeCleaning", "cleaning.resume", "resume", "resume"),
    "return_to_base": (
        "sendToBase",
        "navigation.return_to_base",
        "goToBase",
        "return_to_base",
    ),
    "locate": ("findMe", "utilities.find_me", None, None),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[VacuulaButton]:
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
    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
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
        if self.key == "refresh":
            return self.coordinator.last_update_success
        if not super().available:
            return False
        if self.key == "return_to_base":
            return (
                self.command_allowed("goToBase", "return_to_base") is not False
                or self.command_allowed("pause", "pause") is True
            )
        if self.key in COMMANDS:
            _cmd, _ability, gen3_flag, gen4_flag = COMMANDS[self.key]
            return (
                gen3_flag is None
                or self.command_allowed(gen3_flag, gen4_flag or gen3_flag) is not False
            )
        if self.key in ("start", "clean_spot"):
            return self.command_allowed("start", "start") is not False or (
                self.key == "start" and self.command_allowed("resume", "resume") is True
            )
        if self.key == "explore":
            return (
                self.detail("isDocked", "is_docked") is True
                and self.command_allowed("start", "start") is not False
            )
        return True

    async def async_press(self) -> None:
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
        else:
            command: dict[str, Any]
            if self.key in COMMANDS:
                cmd, ability, _gen3, _gen4 = COMMANDS[self.key]
                command = (
                    {"cmd": cmd}
                    if self.robot.generation == GEN3
                    else {"ability": ability}
                )
            else:
                command = {
                    "cmd": "dismissCurrentAlert"
                    if self.key == "dismiss_alert"
                    else "startPersistentMapExploration"
                }
            await self.coordinator.async_command(self.serial, command)
