"""Standard vacuum actions backed by the app's exact command payloads."""

from typing import Any

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities, value_at

PARALLEL_UPDATES = 0


def fan_modes(robot: RobotData) -> list[str]:
    """Mirror the app's hardware restriction for the D10-only stronger modes."""
    modes = ["eco", "turbo"]
    label_serial = robot.info.get("label_serial", "")
    if (
        robot.generation != GEN3
        and isinstance(label_serial, str)
        and label_serial[4:6] == "N2"
    ):
        modes += ["max", "auto"]
    return modes


def cleaning_mode(coordinator: VacuulaCoordinator, serial: str) -> str:
    """Return the configured default, falling back to the actual mode."""
    robot = coordinator.robots[serial]
    if mode := coordinator.cleaning_options.get(serial, {}).get("mode"):
        if mode in fan_modes(robot):
            return str(mode)
    mode = (
        value_at(robot.state, "cleaning", "mode") if robot.generation == GEN3 else None
    )
    return {1: "eco", 2: "turbo"}.get(mode, "eco") if isinstance(mode, int) else "eco"


def start_payload(
    coordinator: VacuulaCoordinator, serial: str, *, spot: bool = False
) -> dict[str, Any]:
    """Build a future cleaning action without changing the robot's live mode."""
    robot = coordinator.robots[serial]
    options = coordinator.cleaning_options.get(serial, {})
    mode = cleaning_mode(coordinator, serial)
    navigation = options.get("navigation_mode", "normal")
    if robot.generation == GEN3:
        category = 3 if spot or options.get("category") == "spot" else 2
        params = {
            "category": category,
            "mode": {"eco": 1, "turbo": 2}[mode],
            "navigationMode": {"normal": 1, "extra_care": 2, "deep": 3}.get(
                navigation, 1
            ),
        }
        if category == 3:
            params.update(
                spotHeight=options.get("spot_height", 200),
                spotWidth=options.get("spot_width", 200),
            )
        return {"cmd": "startCleaning", "params": params}
    return {
        "ability": "cleaning.start",
        "runs": [
            {
                "map": None,
                "settings": {
                    "mode": mode,
                    "navigation_mode": navigation
                    if navigation in ("normal", "gentle")
                    else "normal",
                },
            }
        ],
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(
        entry,
        async_add_entities,
        lambda coordinator, robot: [VacuulaVacuum(coordinator, robot.serial)],
    )


class VacuulaVacuum(VacuulaEntity, StateVacuumEntity):
    """A Neato connected through the local Vacuula server."""

    _attr_name = None

    def __init__(self, coordinator: VacuulaCoordinator, serial: str) -> None:
        super().__init__(coordinator, serial, "vacuum")

    @property
    def supported_features(self) -> VacuumEntityFeature:
        features = (
            VacuumEntityFeature.STATE
            | VacuumEntityFeature.START
            | VacuumEntityFeature.STOP
            | VacuumEntityFeature.PAUSE
            | VacuumEntityFeature.RETURN_HOME
            | VacuumEntityFeature.FAN_SPEED
        )
        if self.robot.generation != GEN3 or self.robot.capabilities.get("findMe"):
            features |= VacuumEntityFeature.LOCATE
        if self.robot.generation == GEN3 and self.robot.capabilities.get(
            "spotCleaning"
        ):
            features |= VacuumEntityFeature.CLEAN_SPOT
        return features

    @property
    def activity(self) -> VacuumActivity | None:
        state = self.robot.state.get("state")
        action = self.robot.state.get("action")
        if state in (4, "error"):
            return VacuumActivity.ERROR
        if state in (3, "paused"):
            return VacuumActivity.PAUSED
        if state in (1, "idle"):
            return (
                VacuumActivity.DOCKED
                if self.detail("isDocked", "is_docked") is True
                else VacuumActivity.IDLE
            )
        if action in (4, "docking"):
            return VacuumActivity.RETURNING
        if action in (1, 2, 3, 11, "cleaning"):
            return VacuumActivity.CLEANING
        if action in (6, 15, "cleaning_standby"):
            return VacuumActivity.PAUSED
        return None

    @property
    def fan_speed(self) -> str:
        return cleaning_mode(self.coordinator, self.serial)

    @property
    def fan_speed_list(self) -> list[str]:
        return fan_modes(self.robot)

    async def _command(
        self,
        gen3_cmd: str,
        gen4_ability: str,
        gen3_flag: str | None = None,
        gen4_flag: str | None = None,
    ) -> None:
        if (
            gen3_flag is not None
            and self.command_allowed(gen3_flag, gen4_flag or gen3_flag) is False
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        await self.coordinator.async_command(
            self.serial,
            {"cmd": gen3_cmd}
            if self.robot.generation == GEN3
            else {"ability": gen4_ability},
        )

    async def async_start(self) -> None:
        if self.command_allowed("resume", "resume") is True:
            await self._command("resumeCleaning", "cleaning.resume", "resume")
            return
        if self.command_allowed("start", "start") is False:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        await self.coordinator.async_command(
            self.serial, start_payload(self.coordinator, self.serial)
        )

    async def async_stop(self, **kwargs: Any) -> None:
        await self._command("stopCleaning", "cleaning.cancel", "stop", "cancel")

    async def async_pause(self) -> None:
        await self._command("pauseCleaning", "cleaning.pause", "pause")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        await self._command(
            "sendToBase", "navigation.return_to_base", "goToBase", "return_to_base"
        )

    async def async_locate(self, **kwargs: Any) -> None:
        await self._command("findMe", "utilities.find_me")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        if self.robot.generation != GEN3 or not self.robot.capabilities.get(
            "spotCleaning"
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        if self.command_allowed("start", "start") is False:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        await self.coordinator.async_command(
            self.serial, start_payload(self.coordinator, self.serial, spot=True)
        )

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        if fan_speed not in self.fan_speed_list:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_option"
            )
        await self.coordinator.async_set_cleaning_option(self.serial, "mode", fan_speed)
