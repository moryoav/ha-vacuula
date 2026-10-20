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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import CannotConnect, UnsupportedCommand, VacuulaClient, VacuulaError
from .const import DOMAIN, GEN3, GEN4
from .data import RobotData, gen3_paused_run_can_return_home, response_data

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
        for serial, map_id in self._pending_promotions.items():
            self._create_promotion_issue(serial, map_id)
        self._locks: dict[str, asyncio.Lock] = {}
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._server_stream: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_details: dict[str, float] = {}
        self._last_resources: dict[str, float] = {}
        self._unsupported: set[tuple[str, str]] = set()
        self._robot_failures: set[str] = set()
        self._remove_poll: Callable[[], None] | None = None
        # Unique IDs already added, per platform module and robot serial. Kept
        # here so a removed device or record can be added again when it returns.
        self.known_entities: dict[str, dict[str, set[str]]] = {}

    def _update_capabilities(self, robot: RobotData) -> None:
        if isinstance(services := robot.state.get("availableServices"), dict):
            robot.capabilities.update(services)

    def _lock(self, serial: str) -> asyncio.Lock:
        return self._locks.setdefault(serial, asyncio.Lock())

    @callback
    def _async_update_device_name(self, serial: str, name: str) -> None:
        """Follow a robot renamed in Vacuula; a user-chosen HA name still wins."""
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, serial), self.entry.entry_id
        ):
            registry.async_update_device(device.id, name=name)

    @callback
    def async_forget_robot(self, serial: str) -> None:
        """Forget a removed robot's entities, so they are added if it returns.

        The robot's cached data stays (unlisted) until the server lists it again,
        so entities that Home Assistant is still removing keep working.
        """
        if robot := self.robots.get(serial):
            robot.listed = False
            robot.connected = False
        if task := self._streams.pop(serial, None):
            task.cancel()
        self._last_details.pop(serial, None)
        self._last_resources.pop(serial, None)
        self._robot_failures.discard(serial)
        self._unsupported = {item for item in self._unsupported if item[0] != serial}
        for known in self.known_entities.values():
            known.pop(serial, None)
        if serial in self._pending_promotions:
            self._set_pending_promotion(serial, None)

    async def _async_update_data(self) -> dict[str, RobotData]:
        try:
            self.server_info = await self.client.async_server_info()
            snapshot = await self.client.async_robots()
        except VacuulaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="server_unavailable"
            ) from err
        await self._async_snapshot(snapshot)
        for serial, robot in list(self.robots.items()):
            if not robot.listed:
                continue
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
        for serial in list(self._pending_promotions):
            if serial not in snapshot:
                self._set_pending_promotion(serial, None)
        for serial, robot in list(self.robots.items()):
            if serial not in snapshot:
                robot.connected = False
                robot.listed = False
                robot.maps = []
                robot.runs = []
                # Unknown, not empty: its map entities must not look deleted.
                # The lists are read again once the server lists the robot.
                robot.capabilities.pop("maps_available", None)
                robot.capabilities.pop("runs_available", None)
                self._last_resources.pop(serial, None)
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
            if (name := str(metadata.get("name") or robot.name)) != robot.name:
                robot.name = name
                self._async_update_device_name(serial, name)
            if map_id := self._pending_promotions.get(serial):
                self._create_promotion_issue(serial, map_id)
            if label := metadata.get("label_serial"):
                robot.info["label_serial"] = label
            robot.listed = True
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
            # Like the Vacuula app, read errors only from full peer.state lists.
            # robots.error_codes events also carry informational notices.
            if target.get("service") == "peer.state":
                if isinstance(body := payload.get("body"), dict):
                    state = body
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
            # Vacuula's robot list decides whether the robot is connected. A
            # single slow or failed read (such as the server's 504 timeout)
            # must not make the robot unavailable and drop its controls.
            if robot.serial not in self._robot_failures:
                _LOGGER.info("Vacuula robot is not answering")
                self._robot_failures.add(robot.serial)
            return False
        return True

    async def _async_refresh_robot(self, serial: str, *, full: bool = False) -> bool:
        robot = self.robots[serial]
        async with self._lock(serial):
            if not await self._async_read_state_locked(robot):
                return False
            if (
                not full
                and serial in self._last_details
                and monotonic() - self._last_details[serial] < 300
            ):
                return True
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
            return True

    async def _async_refresh_resources(
        self, serial: str, *, full: bool = False
    ) -> bool:
        if (
            not full
            and serial not in self._pending_promotions
            and serial in self._last_resources
            and monotonic() - self._last_resources[serial] < 300
        ):
            return True
        robot = self.robots[serial]
        maps_read = False
        # A map read must not race an in-progress promotion and mistake the
        # previous active map for the outcome of the upload.
        async with self._lock(serial):
            with suppress(VacuulaError):
                await self._async_read_maps_locked(serial)
                maps_read = True
        with suppress(VacuulaError):
            robot.runs = await self.client.async_runs(serial)
            robot.capabilities["runs_available"] = True
        self._last_resources[serial] = monotonic()
        return maps_read

    async def async_refresh_robot(self, serial: str, *, full: bool = False) -> bool:
        """Read the latest state and saved maps, and notify entity listeners.

        Return whether the reads succeeded. Listeners are notified without
        marking the coordinator healthy: only the server poll and event stream
        decide availability, so a failed read during an outage cannot restore it.
        """
        state_read = True
        if self.robots[serial].connected:
            state_read = await self._async_refresh_robot(serial, full=full)
        resources_read = await self._async_refresh_resources(serial, full=full)
        self.async_update_listeners()
        return state_read and resources_read

    async def async_map_operation(
        self, serial: str, map_id: str, action: str, payload: Any = None
    ) -> Any:
        """Serialize map operations that can upload data or command the robot."""
        async with self._lock(serial):
            if action == "promote":
                self._require_robot_connection(serial)
            if action in {"promote", "unuse"}:
                # Match the Vacuula app: only a floorplan marked for use can be
                # promoted, and the active floorplan cannot be taken out of use.
                # Vacuula lists only floorplans in use, so either case would hide
                # the robot's active floorplan and its no-go zones from Automatic.
                await self._async_read_maps_locked(serial)
                floor_map = next(
                    (
                        item
                        for item in self.robots[serial].maps
                        if item.get("id") == map_id
                    ),
                    None,
                )
                if action == "promote" and floor_map is None:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="map_not_in_use"
                    )
                if (
                    action == "unuse"
                    and floor_map is not None
                    and floor_map.get("promoted") is True
                ):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="map_unuse_promoted"
                    )
            if action != "promote":
                result = await self.client.async_map_action(
                    serial, map_id, action, payload
                )
                if (
                    action in {"demote", "delete"}
                    and self._pending_promotions.get(serial) == map_id
                ):
                    self._set_pending_promotion(serial, None)
                return result
            self._set_pending_promotion(serial, map_id)
            try:
                result = await self.client.async_map_action(
                    serial, map_id, action, payload
                )
            except VacuulaError as err:
                with suppress(VacuulaError):
                    await self._async_read_maps_locked(serial)
                await self._async_read_schedule_locked(serial)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="map_promotion_failed"
                ) from err
            await self._async_confirm_promotion_locked(serial, map_id)
            return result

    def _require_robot_connection(self, serial: str) -> None:
        if serial not in self.robots or not self.robots[serial].connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="offline"
            )

    def _create_promotion_issue(self, serial: str, map_id: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{self.entry.entry_id}_{serial}_map_promotion",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="map_promotion_required",
            translation_placeholders={
                "robot": self.robots[serial].name if serial in self.robots else serial,
                "map_id": map_id,
            },
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
            self._create_promotion_issue(serial, map_id)
            self.robots[serial].maps = []
            self.robots[serial].capabilities["maps_available"] = False
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, "pending_map_promotions": pending},
        )
        self._pending_promotions = pending
        self.async_update_listeners()

    def validate_map_recovery(self, serial: str) -> None:
        """Require authoritative map status after an uncertain floorplan upload."""
        if serial in self._pending_promotions:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="map_promotion_required"
            )

    def has_pending_map_recovery(self, serial: str) -> bool:
        """Return whether the latest floorplan upload outcome is unverified."""
        return serial in self._pending_promotions

    def _reconcile_map_recovery(self, serial: str) -> None:
        """Clear the upload guard once the server shows a resolved floorplan.

        After this integration's own upload fails, Vacuula reports the demoted
        floorplan exactly like a deliberate demotion (promoted=False). Keep the
        guard in that state so Automatic does not quietly clean without no-go
        zones. Clear it when a floorplan is active again (promoted here or in
        the Vacuula app) or the uploaded map is no longer in use. Demoting it
        with the map action also clears it.
        """
        if (map_id := self._pending_promotions.get(serial)) is None:
            return
        maps = self.robots[serial].maps
        floor_map = next((item for item in maps if item.get("id") == map_id), None)
        if floor_map is None or any(item.get("promoted") is True for item in maps):
            self._set_pending_promotion(serial, None)

    async def _async_read_maps_locked(self, serial: str) -> None:
        robot = self.robots[serial]
        try:
            robot.maps = await self.client.async_maps(serial)
            robot.capabilities["maps_available"] = True
            self._reconcile_map_recovery(serial)
        except VacuulaError:
            if serial in self._pending_promotions:
                robot.maps = []
                robot.capabilities["maps_available"] = False
            raise
        finally:
            # Notify entities without marking the coordinator healthy: a failed
            # read during a server outage must not make stale entities available.
            self.async_update_listeners()

    async def _async_read_schedule_locked(self, serial: str) -> None:
        """Re-read a Gen4 schedule; the firmware may disable events on map changes."""
        robot = self.robots[serial]
        if robot.generation == GEN4 and robot.connected:
            if (
                schedule := await self._async_optional(robot, "scheduling.show")
            ) is not None:
                robot.schedule = schedule
                self.async_update_listeners()

    async def _async_confirm_promotion_locked(self, serial: str, map_id: str) -> None:
        try:
            await self._async_read_maps_locked(serial)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="map_promotion_failed"
            ) from err
        finally:
            await self._async_read_schedule_locked(serial)
        if not any(
            item.get("id") == map_id and item.get("promoted") is True
            for item in self.robots[serial].maps
        ):
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="map_promotion_failed"
            )

    async def async_set_map_boundaries(
        self, serial: str, map_id: str, boundaries: list[dict[str, Any]]
    ) -> Any:
        """Save Gen4 boundaries and replace an active robot map in one transaction."""
        async with self._lock(serial):
            await self._async_read_maps_locked(serial)
            maps = self.robots[serial].maps
            floor_map = next((item for item in maps if item.get("id") == map_id), None)
            if floor_map is None:
                # The map exists (ownership was checked) but is not in use, so
                # the Vacuula app would not offer it for boundary editing either.
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="map_not_in_use"
                )
            if floor_map.get("promoted") is True:
                self._require_robot_connection(serial)
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
                    await self._async_read_schedule_locked(serial)
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
                await self._async_read_maps_locked(serial)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err

    async def async_update_map(
        self, serial: str, map_id: str, name: str, enabled: bool
    ) -> None:
        """Update confirmed server map settings without requiring a robot connection."""
        try:
            async with self._lock(serial):
                await self.client.async_update_map(serial, map_id, name, enabled)
                # The server applied the change; a failed follow-up read must
                # not report the action as failed. The next poll catches up.
                with suppress(VacuulaError):
                    await self._async_read_maps_locked(serial)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err

    async def async_set_map_enabled(
        self, serial: str, map_id: str, enabled: bool
    ) -> None:
        """Change a Gen3 map's enabled flag, keeping the name stored right now.

        Vacuula writes the name and the flag together, so read the current map
        list first instead of sending a cached name that could undo a rename
        made in the Vacuula app.
        """
        try:
            async with self._lock(serial):
                await self._async_read_maps_locked(serial)
                floor_map = next(
                    (
                        item
                        for item in self.robots[serial].maps
                        if item.get("id") == map_id
                    ),
                    None,
                )
                if floor_map is None or not isinstance(
                    name := floor_map.get("name"), str
                ):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="action_unavailable"
                    )
                await self.client.async_update_map(serial, map_id, name, enabled)
                with suppress(VacuulaError):
                    await self._async_read_maps_locked(serial)
        except VacuulaError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err

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

    async def async_update_schedule_event(
        self,
        serial: str,
        event_id: str,
        update: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        refresh_maps: bool = False,
    ) -> dict[str, Any]:
        """Read, merge and write an existing event under the robot command lock."""
        async with self._lock(serial):
            if serial not in self.robots or not self.robots[serial].connected:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="offline"
                )
            try:
                if refresh_maps:
                    await self._async_read_maps_locked(serial)
                elif self.has_pending_map_recovery(serial):
                    with suppress(VacuulaError):
                        await self._async_read_maps_locked(serial)
                schedule = response_data(
                    await self.client.async_command(
                        serial, {"ability": "scheduling.show"}
                    )
                )
                self.robots[serial].schedule = schedule
                events = schedule.get("events")
                matches = (
                    [
                        event
                        for event in events
                        if isinstance(event, dict) and event.get("id") == event_id
                    ]
                    if isinstance(events, list)
                    else []
                )
                if len(matches) != 1:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="action_unavailable"
                    )
                command = {
                    "ability": "scheduling.update",
                    "event": update(deepcopy(matches[0])),
                }
                result = await self.client.async_command(serial, command)
                await self._async_command_readback_locked(self.robots[serial], command)
            except VacuulaError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
        self.async_update_listeners()
        return result

    async def async_command(
        self,
        serial: str,
        command: dict[str, Any] | Callable[[RobotData], dict[str, Any]],
        *,
        refresh: bool = True,
        require_map_recovery: bool = False,
        refresh_maps: bool = False,
    ) -> dict[str, Any]:
        """Select and execute once under the same lock as map changes."""
        async with self._lock(serial):
            self._require_robot_connection(serial)
            try:
                if refresh_maps:
                    await self._async_read_maps_locked(serial)
                    self._require_robot_connection(serial)
                if require_map_recovery:
                    self.validate_map_recovery(serial)
                if callable(command):
                    command = command(self.robots[serial])
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
            self.async_update_listeners()
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
            self._require_robot_connection(serial)
            try:
                if command_allowed("pause") is True:
                    await self.client.async_command(serial, pause)
                    await asyncio.sleep(3.5)
                    if not await self._async_read_state_locked(robot):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                elif command_allowed(
                    home_flag
                ) is False and gen3_paused_run_can_return_home(robot):
                    # A cached paused run can have ended while this request was
                    # queued. Confirm it before overriding the firmware flag.
                    if not await self._async_read_state_locked(robot):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                if command_allowed(home_flag) is False and not (
                    gen3_paused_run_can_return_home(robot)
                ):
                    raise ServiceValidationError(
                        translation_domain=DOMAIN, translation_key="action_unavailable"
                    )
                self._require_robot_connection(serial)
                await self.client.async_command(serial, home)
                await self._async_read_state_locked(robot)
            except VacuulaError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
            finally:
                # Show a completed pause even when docking was then refused.
                self.async_update_listeners()

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
        self.async_update_listeners()
