"""Robot settings that have a confirmed boolean API."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, GEN3
from .coordinator import VacuulaCoordinator
from .data import RobotData
from .entity import VacuulaEntity, async_add_robot_entities, value_at

PARALLEL_UPDATES = 0

KEYS = ("robot_sounds", "schedule", "force_floorplan")


def switch_value(robot: RobotData, key: str) -> bool | None:
    value: Any = None
    if robot.generation == GEN3:
        if key == "robot_sounds":
            value = robot.preferences.get("robotSounds")
        elif key == "schedule":
            value = value_at(robot.state, "details", "isScheduleEnabled")
            if value is None:
                value = robot.schedule.get("enabled", robot.schedule.get("isEnabled"))
    elif key == "force_floorplan":
        value = robot.preferences.get("force_floorplan")
    return value if isinstance(value, bool) else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[VacuulaCoordinator],
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[SwitchEntity]:
    entities: list[SwitchEntity] = [
        VacuulaSwitch(coordinator, robot.serial, key)
        for key in KEYS
        if switch_value(robot, key) is not None
    ]
    if robot.generation == GEN3:
        for floor_map in robot.maps:
            if (
                isinstance(floor_map.get("id"), str)
                and isinstance(floor_map.get("name"), str)
                and isinstance(floor_map.get("enabled"), bool)
            ):
                entities.append(
                    VacuulaMapSwitch(coordinator, robot.serial, floor_map["id"])
                )
    else:
        events = robot.schedule.get("events")
        if isinstance(events, list):
            for event in events:
                if (
                    isinstance(event, dict)
                    and isinstance(event.get("id"), str)
                    and isinstance(event.get("enabled"), bool)
                    and isinstance(event.get("command"), dict)
                ):
                    entities.append(
                        VacuulaScheduleEventSwitch(
                            coordinator, robot.serial, event["id"]
                        )
                    )
    return entities


class VacuulaSwitch(VacuulaEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
        super().__init__(coordinator, serial, key)
        self.key = key
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool | None:
        return switch_value(self.robot, self.key)

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None

    async def _set(self, enabled: bool) -> None:
        if self.key == "robot_sounds":
            command: dict[str, Any] = {
                "cmd": "setPreferences",
                "params": {"robotSounds": enabled},
            }
        elif self.key == "schedule":
            command = {"cmd": "enableSchedule" if enabled else "disableSchedule"}
        else:
            command = {
                "ability": "settings.set_cleaning_options",
                "force_floorplan": enabled,
            }
        await self.coordinator.async_command(self.serial, command)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class VacuulaMapSwitch(VacuulaEntity, SwitchEntity):
    """Include a saved Generation 3 map without changing its stored name."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "map_enabled"

    def __init__(
        self, coordinator: VacuulaCoordinator, serial: str, map_id: str
    ) -> None:
        super().__init__(coordinator, serial, f"map_{map_id}_enabled")
        self.map_id = map_id
        self._update_name()

    @property
    def floor_map(self) -> dict[str, Any] | None:
        return next(
            (item for item in self.robot.maps if item.get("id") == self.map_id), None
        )

    def _update_name(self) -> None:
        floor_map = self.floor_map
        self._attr_translation_placeholders = {
            "map_name": str(floor_map.get("name", self.map_id))
            if floor_map is not None
            else self.map_id
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_name()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        floor_map = self.floor_map
        value = floor_map.get("enabled") if floor_map is not None else None
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        floor_map = self.floor_map
        return (
            self.coordinator.last_update_success
            and self.is_on is not None
            and floor_map is not None
            and isinstance(floor_map.get("name"), str)
        )

    async def _set(self, enabled: bool) -> None:
        floor_map = self.floor_map
        if floor_map is None or not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        await self.coordinator.async_update_map(
            self.serial, self.map_id, floor_map["name"], enabled
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class VacuulaScheduleEventSwitch(VacuulaEntity, SwitchEntity):
    """Enable one existing Generation 4 event, preserving its full command."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "schedule_event_enabled"

    def __init__(
        self, coordinator: VacuulaCoordinator, serial: str, event_id: str
    ) -> None:
        super().__init__(coordinator, serial, f"schedule_event_{event_id}_enabled")
        self.event_id = event_id
        self._update_name()

    @property
    def event(self) -> dict[str, Any] | None:
        events = self.robot.schedule.get("events")
        if not isinstance(events, list):
            return None
        return next(
            (
                item
                for item in events
                if isinstance(item, dict) and item.get("id") == self.event_id
            ),
            None,
        )

    def _update_name(self) -> None:
        event = self.event
        self._attr_translation_placeholders = {
            "event_name": str(event.get("time", self.event_id))
            if event is not None
            else self.event_id
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_name()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        event = self.event
        value = event.get("enabled") if event is not None else None
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        event = self.event
        return (
            super().available
            and self.is_on is not None
            and event is not None
            and isinstance(event.get("command"), dict)
        )

    async def _set(self, enabled: bool) -> None:
        event = self.event
        if event is None or not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        await self.coordinator.async_command(
            self.serial,
            {"ability": "scheduling.update", "event": {**event, "enabled": enabled}},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
