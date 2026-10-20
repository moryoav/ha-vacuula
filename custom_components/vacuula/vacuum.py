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
    coordinator: VacuulaCoordinator,
    serial: str,
    *,
    spot: bool = False,
    category: str | None = None,
) -> dict[str, Any]:
    """Build a future cleaning action without changing the robot's live mode."""
    robot = coordinator.robots[serial]
    options = coordinator.cleaning_options.get(serial, {})
    mode = cleaning_mode(coordinator, serial)
    navigation = options.get("navigation_mode", "normal")
    category = "spot" if spot else category or options.get("category", "auto")
    if category in {"auto", "map"}:
        coordinator.validate_map_recovery(serial)
    target = cleaning_target(robot, category)
    if robot.generation == GEN3:
        params = {
            **target,
            "mode": {"eco": 1, "turbo": 2}[mode],
            "navigationMode": {"normal": 1, "extra_care": 2, "deep": 3}.get(
                navigation, 1
            ),
        }
        if target["category"] == 3:
            params.update(
                spotHeight=options.get("spot_height", 200),
                spotWidth=options.get("spot_width", 200),
            )
        return {"cmd": "startCleaning", "params": params}
    return {
        "ability": "cleaning.start",
        "runs": [
            {
                **target,
                "settings": {
                    "mode": mode,
                    "navigation_mode": navigation
                    if navigation in ("normal", "gentle")
                    else "normal",
                },
            }
        ],
    }


def cleaning_target(
    robot: RobotData, category: str = "auto", *, require_dock: bool = True
) -> dict[str, Any]:
    """Use an active saved map when possible, following the app's fallback.

    Gen3 lets the robot match its dock against all enabled maps. Gen4 requires
    the one promoted floorplan ID. Automatic cleaning falls back to whole-home
    when no map is active or the robot is off its dock. Explicit mapped cleaning
    requires a usable map. Future schedules do not depend on current docking.
    """
    mapless = {"category": 2} if robot.generation == GEN3 else {"map": None}
    if category == "house":
        return mapless
    if category == "spot" and robot.generation == GEN3:
        return {"category": 3}
    if category not in {"auto", "map"}:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_option"
        )
    active_key = "enabled" if robot.generation == GEN3 else "promoted"
    maps = [item for item in robot.maps if item.get(active_key) is True]
    if not maps and category == "auto":
        return mapless
    if not maps or any(
        not isinstance(item.get("id"), str) or not item["id"] for item in maps
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="map_unavailable"
        )
    if robot.generation != GEN3 and len(maps) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="ambiguous_map"
        )
    if (
        require_dock
        and value_at(
            robot.state,
            "details",
            "isDocked" if robot.generation == GEN3 else "is_docked",
        )
        is not True
    ):
        if category == "auto":
            return mapless
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="map_cleaning_requires_dock"
        )
    if robot.generation == GEN3:
        return {"category": 4}
    return {"map": {"rank_id": maps[0]["id"], "track_id": None, "nogo_enabled": True}}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register a native vacuum for each listed robot and discover later arrivals."""
    async_add_robot_entities(
        entry,
        async_add_entities,
        lambda coordinator, robot: [VacuulaVacuum(coordinator, robot.serial)],
    )


class VacuulaVacuum(VacuulaEntity, StateVacuumEntity):
    """A Neato connected through the local Vacuula server."""

    _attr_name = None

    def __init__(self, coordinator: VacuulaCoordinator, serial: str) -> None:
        """Bind native vacuum controls to the robot's stable entity identity."""
        super().__init__(coordinator, serial, "vacuum")

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Advertise native controls and gate locate and spot cleaning by capability."""
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
        """Project confirmed robot state and action into Home Assistant activities."""
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
        if state in (2, "busy"):
            # HA has no exploration activity. It is an active navigation run,
            # whereas map upload, logs and software updates are stationary work.
            if action == 12 or (
                action == 9
                and value_at(self.robot.state, "cleaning", "category") in (1, 2, 3, 4)
            ):
                return VacuumActivity.CLEANING
            return (
                VacuumActivity.DOCKED
                if self.detail("isDocked", "is_docked") is True
                else VacuumActivity.IDLE
            )
        return None

    @property
    def fan_speed(self) -> str:
        """Return the saved cleaning mode, falling back to the current robot mode."""
        return cleaning_mode(self.coordinator, self.serial)

    @property
    def fan_speed_list(self) -> list[str]:
        """Offer cleaning modes supported by the generation and hardware model."""
        return fan_modes(self.robot)

    async def _command(
        self,
        gen3_cmd: str,
        gen4_ability: str,
        gen3_flag: str | None = None,
        gen4_flag: str | None = None,
    ) -> None:
        """Choose the protocol command and recheck firmware readiness under the lock."""

        def build_command(robot: RobotData) -> dict[str, Any]:
            """Reject newly unavailable actions before building their payload."""
            if (
                gen3_flag is not None
                and self.command_allowed(gen3_flag, gen4_flag or gen3_flag) is False
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="action_unavailable"
                )
            return (
                {"cmd": gen3_cmd}
                if robot.generation == GEN3
                else {"ability": gen4_ability}
            )

        await self.coordinator.async_command(self.serial, build_command)

    async def async_start(self) -> None:
        """Resume a paused run or start cleaning with the configured map policy."""
        if self.command_allowed("resume", "resume") is True:
            await self._command("resumeCleaning", "cleaning.resume", "resume")
            return
        if self.command_allowed("start", "start") is False:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )

        def build_command(robot: RobotData) -> dict[str, Any]:
            # Another queued control may have started a run while this action
            # waited. Do not send a second start using its previous readiness.
            """Recheck start readiness and targets after queued map changes."""
            if self.command_allowed("start", "start") is False:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="action_unavailable"
                )
            return start_payload(self.coordinator, self.serial, category=category)

        category = self.coordinator.cleaning_options.get(self.serial, {}).get(
            "category", "auto"
        )
        await self.coordinator.async_command(
            self.serial,
            build_command,
            require_map_recovery=category in {"auto", "map"},
            refresh_maps=category not in {"house", "spot"},
        )

    async def async_stop(self, **kwargs: Any) -> None:
        """Cancel the current run when firmware advertises cancellation is allowed."""
        await self._command("stopCleaning", "cleaning.cancel", "stop", "cancel")

    async def async_pause(self) -> None:
        """Pause the current run while preserving the robot's ability to resume it."""
        await self._command("pauseCleaning", "cleaning.pause", "pause")

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Use the coordinator's serialized pause-then-home sequence."""
        await self.coordinator.async_return_to_base(self.serial)

    async def async_locate(self, **kwargs: Any) -> None:
        """Ask the robot to play its location sound using its protocol command."""
        await self._command("findMe", "utilities.find_me")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        """Start supported Gen3 spot cleaning with saved dimensions and mode."""
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

        def build_command(robot: RobotData) -> dict[str, Any]:
            """Recheck start readiness under the lock before building a spot run."""
            if self.command_allowed("start", "start") is False:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="action_unavailable"
                )
            return start_payload(self.coordinator, self.serial, spot=True)

        await self.coordinator.async_command(self.serial, build_command)

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Save a validated mode for future runs without changing active cleaning."""
        if fan_speed not in self.fan_speed_list:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_option"
            )
        await self.coordinator.async_set_cleaning_option(self.serial, "mode", fan_speed)
