"""Push updates with a bounded polling fallback for Vacuula."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import aclosing, suppress
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import CannotConnect, UnsupportedCommand, VacuulaClient, VacuulaError
from .const import DOMAIN, GEN3, GEN4
from .data import RobotData, response_data

_LOGGER = logging.getLogger(__name__)


class VacuulaCoordinator(DataUpdateCoordinator[dict[str, RobotData]]):
    """Manage server discovery, independent robots and SSE subscriptions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[VacuulaCoordinator],
        client: VacuulaClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.entry = entry
        self.client = client
        self.robots: dict[str, RobotData] = {}
        self.server_info: dict[str, Any] = {}
        self.cleaning_options: dict[str, dict[str, Any]] = {
            serial: dict(options)
            for serial, options in entry.options.get("cleaning_options", {}).items()
            if isinstance(options, dict)
        }
        self._pending_promotions: dict[str, str] = dict(
            entry.options.get("pending_map_promotions", {})
        )
        for serial in self._pending_promotions:
            self._create_promotion_issue(serial)
        self._locks: dict[str, asyncio.Lock] = {}
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._server_stream: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_details: dict[str, float] = {}
        self._last_resources: dict[str, float] = {}
        self._unsupported: set[tuple[str, str]] = set()
        self._robot_failures: set[str] = set()
        self._remove_poll: Callable[[], None] | None = None

    def _update_capabilities(self, robot: RobotData) -> None:
        if isinstance(services := robot.state.get("availableServices"), dict):
            robot.capabilities.update(services)

    def _lock(self, serial: str) -> asyncio.Lock:
        return self._locks.setdefault(serial, asyncio.Lock())

    async def _async_update_data(self) -> dict[str, RobotData]:
        try:
            self.server_info = await self.client.async_server_info()
            snapshot = await self.client.async_robots()
        except VacuulaError as err:
            raise UpdateFailed("Vacuula server is unavailable") from err
        await self._async_snapshot(snapshot)
        for serial, robot in list(self.robots.items()):
            if robot.connected:
                await self._async_refresh_robot(serial)
            await self._async_refresh_resources(serial)
        return self.robots

    async def _async_snapshot(self, snapshot: dict[str, dict[str, Any]]) -> None:
        robot_ids = sorted(snapshot)
        if self.entry.data.get("robot_ids") != robot_ids:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, "robot_ids": robot_ids}
            )
        for serial, robot in list(self.robots.items()):
            if serial not in snapshot:
                robot.connected = False
                robot.maps = []
                robot.runs = []
                if task := self._streams.pop(serial, None):
                    task.cancel()
        for serial, metadata in snapshot.items():
            generation = metadata.get("type")
            if generation not in (GEN3, GEN4):
                continue
            was_connected = self.robots.get(serial)
            reconnecting = was_connected is None or not was_connected.connected
            robot = self.robots.setdefault(
                serial,
                RobotData(serial, str(metadata.get("name") or "Neato"), generation),
            )
            robot.name = str(metadata.get("name") or robot.name)
            if label := metadata.get("label_serial"):
                robot.info["label_serial"] = label
            robot.connected = metadata.get("connected") is True
            if not robot.connected:
                if task := self._streams.pop(serial, None):
                    task.cancel()
            if reconnecting and robot.connected:
                self._last_details.pop(serial, None)
            if self._server_stream is not None:
                self._ensure_stream(serial)

    def _ensure_stream(self, serial: str) -> None:
        if self._stopping or not self.robots[serial].connected:
            return
        if (existing := self._streams.get(serial)) and not existing.done():
            return
        self._streams[serial] = self.entry.async_create_background_task(
            self.hass,
            self._async_robot_stream(serial),
            f"Vacuula robot events {serial}",
        )

    async def async_start(self) -> None:
        """Start streams after the initial refresh has succeeded."""
        self._server_stream = self.entry.async_create_background_task(
            self.hass, self._async_server_stream(), "Vacuula server events"
        )
        for serial in self.robots:
            self._ensure_stream(serial)
        self._remove_poll = async_track_time_interval(
            self.hass, self._async_poll, timedelta(seconds=60)
        )

    async def _async_poll(self, now: Any) -> None:
        if not self._stopping:
            await self.async_request_refresh()

    async def async_stop(self) -> None:
        """Cancel all streams, including streams sleeping before a reconnect."""
        self._stopping = True
        if self._remove_poll:
            self._remove_poll()
            self._remove_poll = None
        tasks = list(self._streams.values())
        if self._server_stream:
            tasks.append(self._server_stream)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
        self._server_stream = None

    async def _async_server_stream(self) -> None:
        delay = 1
        while not self._stopping:
            try:
                async with aclosing(self.client.async_server_events()) as stream:
                    async for snapshot in stream:
                        await self._async_snapshot(snapshot)
                        self.async_set_updated_data(self.robots)
                        delay = 1
                raise CannotConnect("Event stream closed")
            except VacuulaError:
                # The coordinator's polling path owns outage/recovery logging.
                await self.async_request_refresh()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _async_robot_stream(self, serial: str) -> None:
        delay = 1
        while not self._stopping and self.robots[serial].connected:
            try:
                async with aclosing(self.client.async_robot_events(serial)) as stream:
                    async for event in stream:
                        if event.get("_vacuula_stream_opened"):
                            # Read state after subscribing to avoid an event gap.
                            await self.async_refresh_robot(serial)
                        else:
                            self._handle_event(serial, event)
                        delay = 1
                raise CannotConnect("Event stream closed")
            except VacuulaError:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def _handle_event(self, serial: str, event: dict[str, Any]) -> None:
        robot = self.robots[serial]
        payload = event.get("data", {})
        if not isinstance(payload, dict):
            return
        state: dict[str, Any] | None = None
        if robot.generation == GEN3:
            if payload.get("type") == "state" or str(payload.get("eventId")) == "77":
                if isinstance(body := payload.get("data"), dict):
                    state = body
        elif isinstance(target := payload.get("to"), dict):
            if target.get("service") == "peer.state":
                if isinstance(body := payload.get("body"), dict):
                    state = body
            elif target.get("service") == "robots.error_codes":
                if isinstance(body := payload.get("body"), dict):
                    robot.state["errors"] = [body]
                    self.async_set_updated_data(self.robots)
        if state is not None:
            # Events can contain partial state, so preserve unrelated cached fields.
            for key, value in state.items():
                if isinstance(value, dict) and isinstance(robot.state.get(key), dict):
                    robot.state[key].update(value)
                else:
                    robot.state[key] = value
            self._update_capabilities(robot)
            self.async_set_updated_data(self.robots)

    async def _async_optional(
        self, robot: RobotData, command: str
    ) -> dict[str, Any] | None:
        if (robot.serial, command) in self._unsupported:
            return None
        payload = {"cmd" if robot.generation == GEN3 else "ability": command}
        try:
            return response_data(await self.client.async_command(robot.serial, payload))
        except VacuulaError as err:
            if isinstance(err, UnsupportedCommand):
                self._unsupported.add((robot.serial, command))
            return None

    async def _async_read_state_locked(self, robot: RobotData) -> bool:
        """Read state while the caller owns the robot lock."""
        try:
            command = (
                {"cmd": "getRobotState"}
                if robot.generation == GEN3
                else {"ability": "state.show"}
            )
            robot.state = response_data(
                await self.client.async_command(robot.serial, command)
            )
            self._update_capabilities(robot)
            if robot.serial in self._robot_failures:
                _LOGGER.info("Vacuula robot connection recovered")
                self._robot_failures.remove(robot.serial)
        except VacuulaError:
            robot.connected = False
            if robot.serial not in self._robot_failures:
                _LOGGER.info("Vacuula robot is unavailable")
                self._robot_failures.add(robot.serial)
            return False
        return True

    async def _async_refresh_robot(self, serial: str, *, full: bool = False) -> None:
        robot = self.robots[serial]
        async with self._lock(serial):
            if not await self._async_read_state_locked(robot):
                return
            if (
                not full
                and serial in self._last_details
                and monotonic() - self._last_details[serial] < 300
            ):
                return
            if robot.generation == GEN3:
                queries = {
                    "info": "getGeneralInfo",
                    "stats": "getLocalStats",
                    "preferences": "getPreferences",
                    "schedule": "getSchedule",
                }
            else:
                queries = {
                    "info": "info.robot",
                    "stats": "cleaning.show",
                    "preferences": "settings.cleaning_options",
                    "schedule": "scheduling.show",
                }
            for attribute, query in queries.items():
                if (value := await self._async_optional(robot, query)) is not None:
                    if attribute == "info":
                        robot.info.update(value)
                    else:
                        setattr(robot, attribute, value)
            if robot.generation == GEN4:
                if language := await self._async_optional(
                    robot, "settings.robot_language"
                ):
                    robot.preferences.update(language)
            self._last_details[serial] = monotonic()

    async def _async_refresh_resources(
        self, serial: str, *, full: bool = False
    ) -> None:
        if (
            not full
            and serial in self._last_resources
            and monotonic() - self._last_resources[serial] < 300
        ):
            return
        robot = self.robots[serial]
        for attribute, getter in (
            ("maps", self.client.async_maps),
            ("runs", self.client.async_runs),
        ):
            with suppress(VacuulaError):
                setattr(robot, attribute, await getter(serial))
                robot.capabilities[f"{attribute}_available"] = True
        self._last_resources[serial] = monotonic()

    async def async_refresh_robot(self, serial: str, *, full: bool = False) -> None:
        """Read the latest state and notify entity listeners."""
        if self.robots[serial].connected:
            await self._async_refresh_robot(serial, full=full)
        await self._async_refresh_resources(serial, full=full)
        self.async_set_updated_data(self.robots)

    async def async_map_operation(
        self, serial: str, map_id: str, action: str, payload: Any = None
    ) -> Any:
        """Serialize map operations that can upload data or command the robot."""
        async with self._lock(serial):
            if action != "promote":
                return await self.client.async_map_action(
                    serial, map_id, action, payload
                )
            self._set_pending_promotion(serial, map_id)
            try:
                result = await self.client.async_map_action(
                    serial, map_id, action, payload
                )
            except VacuulaError as err:
                with suppress(VacuulaError):
                    await self._async_read_maps_locked(serial)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="map_promotion_failed"
                ) from err
            await self._async_confirm_promotion_locked(serial, map_id)
            return result

    def _create_promotion_issue(self, serial: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{self.entry.entry_id}_{serial}_map_promotion",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="map_promotion_required",
        )

    def _set_pending_promotion(self, serial: str, map_id: str | None) -> None:
        pending = dict(self._pending_promotions)
        if map_id is None:
            pending.pop(serial, None)
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{self.entry.entry_id}_{serial}_map_promotion"
            )
        else:
            pending[serial] = map_id
            self._create_promotion_issue(serial)
            self.robots[serial].maps = []
            self.robots[serial].capabilities["maps_available"] = False
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, "pending_map_promotions": pending},
        )
        self._pending_promotions = pending
        self.async_set_updated_data(self.robots)

    def validate_map_recovery(self, serial: str) -> None:
        """Do not silently bypass no-go zones after a known failed map upload."""
        if serial in self._pending_promotions:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="map_promotion_required"
            )

    async def _async_read_maps_locked(self, serial: str) -> None:
        robot = self.robots[serial]
        try:
            robot.maps = await self.client.async_maps(serial)
            robot.capabilities["maps_available"] = True
        except VacuulaError:
            if serial in self._pending_promotions:
                robot.maps = []
                robot.capabilities["maps_available"] = False
            raise
        finally:
            self.async_set_updated_data(self.robots)

    async def _async_confirm_promotion_locked(self, serial: str, map_id: str) -> None:
        try:
            await self._async_read_maps_locked(serial)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="map_promotion_failed"
            ) from err
        if not any(
            item.get("id") == map_id and item.get("promoted") is True
            for item in self.robots[serial].maps
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="map_promotion_failed"
            )
        self._set_pending_promotion(serial, None)

    async def async_set_map_boundaries(
        self, serial: str, map_id: str, boundaries: list[dict[str, Any]]
    ) -> Any:
        """Save Gen4 boundaries and replace an active robot map in one transaction."""
        async with self._lock(serial):
            maps = await self.client.async_maps(serial)
            floor_map = next((item for item in maps if item.get("id") == map_id), None)
            if floor_map is None:
                raise VacuulaError("Map does not belong to the robot")
            result = await self.client.async_map_action(
                serial, map_id, "boundaries", boundaries
            )
            if floor_map.get("promoted") is True:
                # The server demotes first. Persist before awaiting promotion so
                # cancellation or a restart cannot conceal missing no-go zones.
                self._set_pending_promotion(serial, map_id)
                try:
                    await self.client.async_map_action(serial, map_id, "promote")
                except VacuulaError as err:
                    with suppress(VacuulaError):
                        await self._async_read_maps_locked(serial)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="map_promotion_failed",
                    ) from err
                await self._async_confirm_promotion_locked(serial, map_id)
            else:
                await self._async_read_maps_locked(serial)
            return result

    async def async_refresh_maps(self, serial: str) -> None:
        """Read saved maps without issuing unrelated robot or history commands."""
        try:
            async with self._lock(serial):
                maps = await self.client.async_maps(serial)
                self.robots[serial].maps = maps
                self.robots[serial].capabilities["maps_available"] = True
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err
        self.async_set_updated_data(self.robots)

    async def async_update_map(
        self, serial: str, map_id: str, name: str, enabled: bool
    ) -> None:
        """Update confirmed server map settings without requiring a robot connection."""
        try:
            async with self._lock(serial):
                await self.client.async_update_map(serial, map_id, name, enabled)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err
        await self.async_refresh_maps(serial)

    async def _async_command_readback_locked(
        self, robot: RobotData, command: dict[str, Any]
    ) -> None:
        """Confirm state and only the setting changed by this command."""
        if not await self._async_read_state_locked(robot):
            return
        name = command.get("cmd", command.get("ability"))
        targets = {
            "setPreferences": ("preferences", "getPreferences"),
            "setSchedule": ("schedule", "getSchedule"),
            "enableSchedule": ("schedule", "getSchedule"),
            "disableSchedule": ("schedule", "getSchedule"),
            "settings.set_cleaning_options": (
                "preferences",
                "settings.cleaning_options",
            ),
            "settings.set_robot_language": ("preferences", "settings.robot_language"),
            "scheduling.update": ("schedule", "scheduling.show"),
            "scheduling.delete": ("schedule", "scheduling.show"),
        }
        if name not in targets:
            return
        attribute, query = targets[name]
        if (value := await self._async_optional(robot, query)) is not None:
            if robot.generation == GEN4 and attribute == "preferences":
                robot.preferences.update(value)
            else:
                setattr(robot, attribute, value)

    async def async_command(
        self,
        serial: str,
        command: dict[str, Any],
        *,
        refresh: bool = True,
        require_map_recovery: bool = False,
    ) -> dict[str, Any]:
        """Execute exactly once, serialize commands and confirm state afterwards."""
        if serial not in self.robots or not self.robots[serial].connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="offline"
            )
        async with self._lock(serial):
            if require_map_recovery:
                self.validate_map_recovery(serial)
            try:
                if command.get("cmd") == "setPreferences":
                    # Read and merge under the same lock as the write so two
                    # settings actions cannot revert each other's changes.
                    preferences = response_data(
                        await self.client.async_command(
                            serial, {"cmd": "getPreferences"}
                        )
                    )
                    changes = command.get("params", {})
                    if not preferences or not set(changes).issubset(preferences):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                    command = {**command, "params": {**preferences, **changes}}
                elif command.get("ability") in {
                    "settings.set_robot_language",
                    "settings.set_cleaning_options",
                }:
                    ability = command["ability"]
                    read_ability = {
                        "settings.set_robot_language": "settings.robot_language",
                        "settings.set_cleaning_options": "settings.cleaning_options",
                    }[ability]
                    settings = response_data(
                        await self.client.async_command(
                            serial, {"ability": read_ability}
                        )
                    )
                    changes = {
                        key: value for key, value in command.items() if key != "ability"
                    }
                    if not settings or not set(changes).issubset(settings):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                    command = {**settings, **changes, "ability": ability}
                result = await self.client.async_command(serial, command)
                if refresh:
                    await self._async_command_readback_locked(
                        self.robots[serial], command
                    )
            except VacuulaError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
        if refresh:
            self.async_set_updated_data(self.robots)
        return result

    async def async_return_to_base(self, serial: str) -> None:
        """Pause an active run before docking, without interleaved robot commands."""
        if serial not in self.robots or not self.robots[serial].connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="offline"
            )
        robot = self.robots[serial]

        def command_allowed(flag: str) -> Any:
            key = (
                "availableCommands"
                if robot.generation == GEN3
                else "available_commands"
            )
            commands = robot.state.get(key, {})
            return commands.get(flag) if isinstance(commands, dict) else None

        home_flag = "goToBase" if robot.generation == GEN3 else "return_to_base"
        pause = (
            {"cmd": "pauseCleaning"}
            if robot.generation == GEN3
            else {"ability": "cleaning.pause"}
        )
        home = (
            {"cmd": "sendToBase"}
            if robot.generation == GEN3
            else {"ability": "navigation.return_to_base"}
        )
        async with self._lock(serial):
            try:
                if command_allowed("pause") is True:
                    await self.client.async_command(serial, pause)
                    await asyncio.sleep(3.5)
                    if not await self._async_read_state_locked(robot):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                if command_allowed(home_flag) is False:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="action_unavailable"
                    )
                await self.client.async_command(serial, home)
                await self._async_read_state_locked(robot)
            except VacuulaError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
        self.async_set_updated_data(self.robots)

    async def async_set_cleaning_option(
        self, serial: str, key: str, value: Any
    ) -> None:
        """Persist defaults used by future cleaning actions without starting a run."""
        if serial not in self.robots:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="unknown_robot"
            )
        options = deepcopy(self.cleaning_options)
        options.setdefault(serial, {})[key] = value
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, "cleaning_options": options},
        )
        self.cleaning_options = deepcopy(options)
        self.async_set_updated_data(self.robots)
