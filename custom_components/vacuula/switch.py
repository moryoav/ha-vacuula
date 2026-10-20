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
from .data import RobotData, event_targets_map
from .entity import (
    VacuulaEntity,
    async_add_robot_entities,
    async_remove_stale_entity,
    async_remove_stale_records,
    set_name_placeholders,
    value_at,
)

PARALLEL_UPDATES = 0

KEYS = ("robot_sounds", "schedule", "force_floorplan")


def switch_value(robot: RobotData, key: str) -> bool | None:
    """Read an explicit settings boolean, including the Gen3 schedule fallback."""
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
    """Register settings switches and reconcile deleted map and schedule records."""
    coordinator = entry.runtime_data

    @callback
    def remove_deleted_records() -> None:
        """Prune disabled and enabled record entities after authoritative list reads."""
        for serial, robot in coordinator.robots.items():
            if not robot.listed:
                continue
            if robot.generation == GEN3:
                if robot.capabilities.get("maps_available") is not True:
                    continue
                prefix = f"{serial}_map_"
                current = {
                    f"{prefix}{floor_map['id']}_enabled"
                    for floor_map in robot.maps
                    if isinstance(floor_map.get("id"), str)
                }
            else:
                events = robot.schedule.get("events")
                if not isinstance(events, list):
                    continue
                prefix = f"{serial}_schedule_event_"
                current = {
                    f"{prefix}{event['id']}_enabled"
                    for event in events
                    if isinstance(event, dict) and isinstance(event.get("id"), str)
                }
            async_remove_stale_records(
                coordinator, serial, __name__, "switch", prefix, current
            )

    remove_deleted_records()
    entry.async_on_unload(coordinator.async_add_listener(remove_deleted_records))
    async_add_robot_entities(entry, async_add_entities, create_entities)


def create_entities(
    coordinator: VacuulaCoordinator, robot: RobotData
) -> list[SwitchEntity]:
    """Build confirmed settings, Gen3 map toggles and Gen4 schedule-event toggles."""
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
    """Toggle a confirmed robot preference or the Generation 3 schedule."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: VacuulaCoordinator, serial: str, key: str) -> None:
        """Bind the settings key to a stable robot entity and translated name."""
        super().__init__(coordinator, serial, key)
        self.key = key
        self._attr_translation_key = key

    @property
    def is_on(self) -> bool | None:
        """Return the confirmed settings flag, or None when it is not reported."""
        return switch_value(self.robot, self.key)

    @property
    def available(self) -> bool:
        """Require connection and a known settings value before allowing changes."""
        return super().available and self.is_on is not None

    async def _set(self, enabled: bool) -> None:
        """Send the generation-specific preference or schedule-enable command."""
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
        """Enable the represented robot setting through the coordinator."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the represented robot setting through the coordinator."""
        await self._set(False)


class VacuulaMapSwitch(VacuulaEntity, SwitchEntity):
    """Include a saved Generation 3 map without changing its stored name."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "map_enabled"

    def __init__(
        self, coordinator: VacuulaCoordinator, serial: str, map_id: str
    ) -> None:
        """Bind an enabled-map control to its stable map ID and current name."""
        super().__init__(coordinator, serial, f"map_{map_id}_enabled")
        self.map_id = map_id
        self._update_name()

    @property
    def floor_map(self) -> dict[str, Any] | None:
        """Find the current cached record for this map, or None after deletion."""
        return next(
            (item for item in self.robot.maps if item.get("id") == self.map_id), None
        )

    def _update_name(self) -> None:
        """Follow map renames, falling back to the map ID when no name is known.

        While the map list is unknown (a failed read or an unlisted robot) the
        last known name is kept.
        """
        floor_map = self.floor_map
        if floor_map is None and getattr(self, "_attr_translation_placeholders", None):
            return
        set_name_placeholders(
            self,
            {
                "map_name": str(floor_map.get("name", self.map_id))
                if floor_map is not None
                else self.map_id
            },
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remove a confirmed deleted map or refresh its translated name and state."""
        if (
            self.robot.listed
            and self.robot.capabilities.get("maps_available") is True
            and self.floor_map is None
        ):
            # The server's map list was read and no longer has this map.
            async_remove_stale_entity(self, self.serial)
            return
        self._update_name()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        """Return the saved map's explicit enabled flag, or None if unavailable."""
        floor_map = self.floor_map
        value = floor_map.get("enabled") if floor_map is not None else None
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        """Allow server-side map edits when its enabled flag and name are known."""
        floor_map = self.floor_map
        return (
            self.coordinator.last_update_success
            and self.is_on is not None
            and floor_map is not None
            and isinstance(floor_map.get("name"), str)
        )

    async def _set(self, enabled: bool) -> None:
        """Toggle the map under the coordinator lock while preserving its fresh name."""
        if self.floor_map is None or not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )
        # The coordinator re-reads the map name; the cached one may be stale.
        await self.coordinator.async_set_map_enabled(self.serial, self.map_id, enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Include this saved map among the robot's enabled Gen3 maps."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Exclude this saved map without deleting its stored data."""
        await self._set(False)


class VacuulaScheduleEventSwitch(VacuulaEntity, SwitchEntity):
    """Enable one existing Generation 4 event, preserving its full command."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "schedule_event_enabled"

    def __init__(
        self, coordinator: VacuulaCoordinator, serial: str, event_id: str
    ) -> None:
        """Bind an event toggle to its stable ID and current schedule label."""
        super().__init__(coordinator, serial, f"schedule_event_{event_id}_enabled")
        self.event_id = event_id
        self._update_name()

    @property
    def event(self) -> dict[str, Any] | None:
        """Find the current cached schedule event, or None for a missing record."""
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
        """Label the event with its days and time, falling back to the event ID."""
        event = self.event
        name = self.event_id
        if event is not None and isinstance(time := event.get("time"), str):
            days = event.get("days")
            # Include the days so events at the same time stay distinguishable.
            if isinstance(days, list) and all(isinstance(day, str) for day in days):
                name = f"{', '.join(days)} {time}"
            else:
                name = time
        set_name_placeholders(self, {"event_name": name})

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remove a deleted event or refresh its schedule label and state."""
        if (
            self.robot.listed
            and isinstance(self.robot.schedule.get("events"), list)
            and self.event is None
        ):
            # The robot's schedule was read and no longer has this event.
            async_remove_stale_entity(self, self.serial)
            return
        self._update_name()
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool | None:
        """Return the event's explicit enabled flag, or None for unknown data."""
        event = self.event
        value = event.get("enabled") if event is not None else None
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        """Require connection and a known event with an editable command."""
        event = self.event
        return (
            super().available
            and self.is_on is not None
            and event is not None
            and isinstance(event.get("command"), dict)
        )

    async def _set(self, enabled: bool) -> None:
        """Merge the enabled flag into a fresh event without replacing its settings."""
        event = self.event
        if event is None or not self.available:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="action_unavailable"
            )

        def update(current: dict[str, Any]) -> dict[str, Any]:
            """Set the flag; enabling a mapped event waits for map recovery."""
            if enabled and event_targets_map(current):
                self.coordinator.validate_map_recovery(self.serial)
            return {**current, "enabled": enabled}

        await self.coordinator.async_update_schedule_event(
            self.serial, self.event_id, update
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable this event while preserving its command, timing and zones."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable this event while retaining it and its existing settings."""
        await self._set(False)
