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

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    CannotConnect,
    CommandError,
    MapRequestNotSent,
    MapRequestRejected,
    UnsupportedCommand,
    VacuulaClient,
    VacuulaError,
)
from .const import DOMAIN, GEN3, GEN4
from .data import (
    RobotData,
    begin_return_trip,
    clear_return_trip,
    response_data,
    track_return_trip,
)

_LOGGER = logging.getLogger(__name__)

# Commands after which the robot drives off. The D5 sends one state event as it
# leaves the dock, while pause is still unavailable and charging is reported,
# and no event when those flags change a few seconds later.
MOTION_COMMANDS = {
    "startCleaning",
    "resumeCleaning",
    "startPersistentMapExploration",
    "sendToBase",
    "cleaning.start",
    "cleaning.resume",
    "navigation.return_to_base",
}
FOLLOW_UP_DELAY = 10


class VacuulaCoordinator(DataUpdateCoordinator[dict[str, RobotData]]):
    """Manage server discovery, independent robots and SSE subscriptions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[VacuulaCoordinator],
        client: VacuulaClient,
    ) -> None:
        """Restore cleaning defaults and map recovery guards, then initialize caches."""
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
        # A promoted flag alone cannot prove that newly saved boundaries were
        # uploaded. Remember whether a later demotion has been observed, so an
        # external re-promotion can resolve that distinct uncertainty too.
        self._pending_boundary_sync: dict[str, bool] = {
            serial: observed_demoted
            for serial, observed_demoted in entry.options.get(
                "pending_boundary_sync", {}
            ).items()
            if serial in self._pending_promotions and isinstance(observed_demoted, bool)
        }
        for serial, map_id in self._pending_promotions.items():
            self._create_promotion_issue(serial, map_id)
        self._map_operations: dict[str, dict[str, Any]] = deepcopy(
            dict(entry.options.get("pending_map_operations", {}))
        )
        for serial, operation in self._map_operations.items():
            self._pending_promotions.setdefault(serial, operation["map_id"])
            self._create_promotion_issue(serial, self._pending_promotions[serial])
        self._locks: dict[str, asyncio.Lock] = {}
        self._streams: dict[str, asyncio.Task[None]] = {}
        self._server_stream: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_details: dict[str, float] = {}
        self._last_resources: dict[str, float] = {}
        self._unsupported: set[tuple[str, str]] = set()
        self._robot_failures: set[str] = set()
        self._remove_poll: Callable[[], None] | None = None
        self._follow_ups: dict[str, Callable[[], None]] = {}
        # State replies and pushes use separate connections. Keep only deltas
        # received during each locked read so its older reply cannot erase them.
        self._state_read_updates: dict[str, dict[str, Any]] = {}
        # Server snapshots also travel over independent HTTP and SSE channels.
        self._server_snapshot_revision = 0
        # Unique IDs already added, per platform module and robot serial. Kept
        # here so a removed device or record can be added again when it returns.
        self.known_entities: dict[str, dict[str, set[str]]] = {}

    def _update_capabilities(self, robot: RobotData) -> None:
        """Merge Gen3 service capabilities advertised in the latest robot state."""
        if isinstance(services := robot.state.get("availableServices"), dict):
            robot.capabilities.update(services)

    def _lock(self, serial: str) -> asyncio.Lock:
        """Return the shared per-robot lock for reads and command transactions."""
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
    def _async_update_device_metadata(self, robot: RobotData) -> None:
        """Follow confirmed firmware and model changes without replacing user names."""
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier(
            (DOMAIN, robot.serial), self.entry.entry_id
        ):
            meta = robot.state.get("meta")
            meta = meta if isinstance(meta, dict) else {}
            values = {
                "model": robot.info.get("model") or meta.get("modelName"),
                "sw_version": robot.info.get("firmware") or meta.get("firmware"),
            }
            changes = {
                key: value
                for key, value in values.items()
                if isinstance(value, str) and value and getattr(device, key) != value
            }
            if changes:
                registry.async_update_device(
                    device.id,
                    model=changes.get("model", device.model),
                    sw_version=changes.get("sw_version", device.sw_version),
                )

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
        """Poll the server and refresh listed robots with throttled details."""
        try:
            self.server_info = await self.client.async_server_info()
            snapshot_revision = self._server_snapshot_revision
            snapshot = await self.client.async_robots()
        except VacuulaError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="server_unavailable"
            ) from err
        if snapshot_revision == self._server_snapshot_revision:
            await self._async_snapshot(snapshot)
        for serial, robot in list(self.robots.items()):
            if not robot.listed:
                continue
            if robot.connected:
                await self._async_refresh_robot(serial)
            await self._async_refresh_resources(serial)
        return self.robots

    async def _async_snapshot(self, snapshot: dict[str, dict[str, Any]]) -> None:
        """Reconcile robot metadata, connection changes and event subscriptions."""
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
        """Create at most one event task for a connected robot unless stopping."""
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
        """Request the polling fallback refresh while the integration is running."""
        if not self._stopping:
            await self.async_request_refresh()

    async def async_stop(self) -> None:
        """Cancel all streams, including streams sleeping before a reconnect."""
        self._stopping = True
        if self._remove_poll:
            self._remove_poll()
            self._remove_poll = None
        for cancel in self._follow_ups.values():
            cancel()
        self._follow_ups.clear()
        tasks = list(self._streams.values())
        if self._server_stream:
            tasks.append(self._server_stream)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._streams.clear()
        self._server_stream = None

    async def _async_server_stream(self) -> None:
        """Apply server snapshots and reconnect with bounded exponential backoff."""
        delay = 1
        while not self._stopping:
            try:
                async with aclosing(self.client.async_server_events()) as stream:
                    async for snapshot in stream:
                        self._server_snapshot_revision += 1
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
        """Subscribe before reading state, then reconnect after stream failures."""
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
        """Merge recognized partial state events without discarding cached fields."""
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
            if (updates := self._state_read_updates.get(serial)) is not None:
                for key, value in state.items():
                    if isinstance(value, dict) and isinstance(updates.get(key), dict):
                        updates[key].update(deepcopy(value))
                    else:
                        updates[key] = deepcopy(value)
            # Events can contain partial state, so preserve unrelated cached fields.
            for key, value in state.items():
                if isinstance(value, dict) and isinstance(robot.state.get(key), dict):
                    robot.state[key].update(value)
                else:
                    robot.state[key] = value
            self._update_capabilities(robot)
            track_return_trip(robot)
            self._async_update_device_metadata(robot)
            self.async_set_updated_data(self.robots)
            if robot.state.get("state") in (2, "busy"):
                # Runs started by the app, a schedule or the robot itself also
                # leave the dock with stale flags and no later event.
                self._schedule_state_follow_up(serial)

    async def _async_optional(
        self, robot: RobotData, command: str
    ) -> dict[str, Any] | None:
        """Read optional telemetry, caching only explicitly unsupported commands."""
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
        """Read state under the robot lock, retaining pushes received during I/O."""
        updates: dict[str, Any] = {}
        self._state_read_updates[robot.serial] = updates
        try:
            command = (
                {"cmd": "getRobotState"}
                if robot.generation == GEN3
                else {"ability": "state.show"}
            )
            snapshot = response_data(
                await self.client.async_command(robot.serial, command)
            )
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(snapshot.get(key), dict):
                    snapshot[key].update(value)
                else:
                    snapshot[key] = value
            robot.state = snapshot
            self._update_capabilities(robot)
            track_return_trip(robot)
            self._async_update_device_metadata(robot)
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
        finally:
            self._state_read_updates.pop(robot.serial, None)
        return True

    async def _async_refresh_robot(self, serial: str, *, full: bool = False) -> bool:
        """Read locked robot state and throttle optional details to five minutes."""
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
                        self._async_update_device_metadata(robot)
                    elif attribute == "preferences" and robot.generation == GEN4:
                        # The language list comes from a separate read below;
                        # keep it when only that read fails.
                        robot.preferences.update(value)
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
        """Refresh stored maps and history, bypassing throttling during map recovery."""
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

        Return whether attempted state and map reads succeeded; throttled or
        skipped reads count as successful and history failures are ignored.
        Listeners are notified without marking the coordinator healthy: only
        the server poll and event stream decide availability, so a failed read
        during an outage cannot restore it.
        """
        state_read = True
        if self.robots[serial].connected:
            state_read = await self._async_refresh_robot(serial, full=full)
        resources_read = await self._async_refresh_resources(serial, full=full)
        self.async_update_listeners()
        return state_read and resources_read

    async def async_map_operation(
        self,
        serial: str,
        map_id: str,
        action: str,
        payload: Any = None,
        *,
        confirm_server_restarted: bool = False,
    ) -> Any:
        """Serialize map operations that can upload data or command the robot."""
        async with self._lock(serial):
            await self._async_require_map_operation_settled(
                serial, confirm_server_restarted=confirm_server_restarted
            )
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
                if (
                    action == "unuse"
                    and self.has_pending_map_recovery(serial)
                    and self._pending_promotions.get(serial) != map_id
                ):
                    # Keep one uncertain floorplan per robot. Hiding another
                    # map could conceal an externally activated floorplan too.
                    self.validate_map_recovery(serial)
            if action != "promote":
                await self._async_read_maps_locked(serial)
                visible = any(
                    item.get("id") == map_id for item in self.robots[serial].maps
                )
                if (action == "use" and visible) or (action == "unuse" and not visible):
                    return None
                result = await self._async_map_request_locked(
                    serial, map_id, action, payload
                )
                if (
                    action in {"demote", "delete"}
                    and self._pending_promotions.get(serial) == map_id
                ):
                    self._set_pending_promotion(serial, None)
                return result
            previous_pending = self._pending_promotions.get(serial)
            self._set_pending_promotion(serial, map_id)
            try:
                result = await self._async_map_request_locked(
                    serial, map_id, action, payload
                )
            except MapRequestNotSent as err:
                self._set_pending_promotion(serial, previous_pending)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
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
        """Reject commands when server metadata no longer shows the robot connected."""
        if serial not in self.robots or not self.robots[serial].connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="offline"
            )

    def _create_promotion_issue(self, serial: str, map_id: str) -> None:
        """Create a persistent repair notice naming the robot and uncertain map."""
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

    def _persist_map_recovery(self) -> None:
        """Save upload guards and boundary uncertainty together across restarts."""
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={
                **self.entry.options,
                "pending_map_promotions": dict(self._pending_promotions),
                "pending_boundary_sync": dict(self._pending_boundary_sync),
                "pending_map_operations": deepcopy(self._map_operations),
            },
        )

    def _set_pending_promotion(
        self, serial: str, map_id: str | None, *, boundary_change: bool = False
    ) -> None:
        """Persist or clear the upload guard and synchronize its repair notice.

        While promotion is pending, hide cached maps so they cannot authorize
        cleaning with a floorplan whose upload has not been confirmed.
        """
        pending = dict(self._pending_promotions)
        if map_id is None:
            pending.pop(serial, None)
            self._pending_boundary_sync.pop(serial, None)
            self._map_operations.pop(serial, None)
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{self.entry.entry_id}_{serial}_map_promotion"
            )
        else:
            if serial in self._pending_boundary_sync and not boundary_change:
                # Replacing the guard before a different map is uploaded would
                # forget unsynchronized zones if that attempt is rejected or
                # interrupted before demotion. Keep the original map guarded
                # until a replacement is confirmed active.
                map_id = pending[serial]
            if boundary_change:
                self._pending_boundary_sync[serial] = False
            elif pending.get(serial) != map_id:
                self._pending_boundary_sync.pop(serial, None)
            pending[serial] = map_id
            self._create_promotion_issue(serial, map_id)
            self.robots[serial].maps = []
            self.robots[serial].capabilities["maps_available"] = False
        self._pending_promotions = pending
        self._persist_map_recovery()
        self.async_update_listeners()

    def validate_map_recovery(self, serial: str) -> None:
        """Require authoritative map status after an uncertain floorplan upload."""
        if serial in self._pending_promotions or serial in self._map_operations:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="map_promotion_required"
            )

    def has_pending_map_recovery(self, serial: str) -> bool:
        """Return whether the latest floorplan upload outcome is unverified."""
        return serial in self._pending_promotions or serial in self._map_operations

    async def _async_require_map_operation_settled(
        self, serial: str, *, confirm_server_restarted: bool = False
    ) -> None:
        """Refuse overlapping writes until an earlier server handler has finished."""
        if serial not in self._map_operations:
            return
        await self._async_read_maps_locked(serial)
        operation = self._map_operations.get(serial)
        if operation is None or operation["action"] == "recovery":
            return
        if confirm_server_restarted:
            self._resolve_restarted_map_operation(serial, operation)
            return
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="map_operation_pending"
        )

    async def _async_map_request_locked(
        self,
        serial: str,
        map_id: str,
        action: str,
        payload: Any = None,
        *,
        require_connection: bool = False,
    ) -> Any:
        """Persist a write fence before dispatch and retain ambiguous outcomes.

        A disconnected HTTP caller does not stop Vacuula's threaded handler.
        A completed response ends this fence; a read of unchanged data does not.
        """
        if require_connection or action == "promote":
            self._require_robot_connection(serial)
        previous = self._map_operations.get(serial)
        operation: dict[str, Any] = {
            "action": action,
            "map_id": map_id,
            "boot_id": None,
            "needs_upload": action in {"promote", "demote", "delete", "unuse"}
            or require_connection
            or serial in self._pending_promotions,
        }
        self._map_operations[serial] = operation
        self._persist_map_recovery()
        try:
            result = await self.client.async_map_action(serial, map_id, action, payload)
        except MapRequestRejected, MapRequestNotSent:
            self._restore_completed_map_operation(serial, previous)
            raise
        except BaseException:
            if serial not in self._pending_promotions:
                self._set_pending_promotion(serial, map_id)
            # Probe only after the uncertain request. A process identity read
            # before dispatch might belong to a server that restarted before
            # receiving this POST, and must never unfence its new handler.
            with suppress(VacuulaError, TimeoutError):
                async with asyncio.timeout(5):
                    operation["boot_id"] = await self.client.async_server_boot_id()
                    self._persist_map_recovery()
            raise
        self._restore_completed_map_operation(serial, previous)
        return result

    def _restore_completed_map_operation(
        self, serial: str, previous: dict[str, Any] | None
    ) -> None:
        """Remove a completed request while retaining an older confirmation guard."""
        if previous is not None and previous.get("action") == "recovery":
            self._map_operations[serial] = previous
        else:
            self._map_operations.pop(serial, None)
        self._persist_map_recovery()

    async def _async_reconcile_map_operation(self, serial: str) -> None:
        """Keep unknown work fenced until a later Vacuula process is observed.

        Values alone cannot identify the writer: a web-app edit or promotion
        could match the requested outcome before our older handler finishes.
        """
        operation = self._map_operations.get(serial)
        if operation is None or operation["action"] == "recovery":
            return
        boot_id = None
        with suppress(VacuulaError, TimeoutError):
            async with asyncio.timeout(5):
                boot_id = await self.client.async_server_boot_id()
        if operation.get("boot_id") is None:
            if boot_id is not None:
                operation["boot_id"] = boot_id
                self._persist_map_recovery()
        elif boot_id is not None and boot_id != operation["boot_id"]:
            if operation["action"] == "boundaries" and (
                not operation.get("needs_upload", True)
                or operation["map_id"] != self._pending_promotions.get(serial)
            ):
                # The first map snapshot may precede the restart. An external
                # promotion can activate this map before its late boundary
                # save, leaving the robot with older zones than the database.
                self.robots[serial].maps = await self.client.async_maps(serial)
            self._resolve_restarted_map_operation(serial, operation)

    def _resolve_restarted_map_operation(
        self, serial: str, operation: dict[str, Any]
    ) -> None:
        """End an old handler fence while retaining any required map confirmation."""
        newly_active_boundaries = operation["action"] == "boundaries" and any(
            item.get("id") == operation["map_id"] and item.get("promoted") is True
            for item in self.robots[serial].maps
        )
        # An old unuse handler can hide a map that the app promoted after our
        # preflight. The API's map list cannot reveal that hidden active map.
        needs_upload = (
            operation.get("needs_upload", True)
            or operation["action"] == "unuse"
            or newly_active_boundaries
        )
        if not needs_upload:
            self._set_pending_promotion(serial, None)
            return
        if newly_active_boundaries:
            # A different map may have replaced an older guarded floorplan
            # while this save was pending. Only confirmation of the currently
            # active target's zones can resolve that newer uncertainty.
            self._pending_promotions[serial] = operation["map_id"]
            self._pending_boundary_sync[serial] = False
            self._create_promotion_issue(serial, operation["map_id"])
        self._map_operations[serial] = {
            "action": "recovery",
            "map_id": self._pending_promotions[serial],
        }
        self._persist_map_recovery()

    def _reconcile_map_recovery(self, serial: str) -> None:
        """Clear the upload guard once the server shows a resolved floorplan.

        After this integration's own upload fails, Vacuula reports the demoted
        floorplan exactly like a deliberate demotion (promoted=False). Keep the
        guard in that state so Automatic does not quietly clean without no-go
        zones. Clear it when a floorplan is active again (promoted here or in
        the Vacuula app) or the uploaded map is no longer in use. Boundary
        changes additionally require an acknowledged upload or an observed
        demotion followed by promotion: an unchanged promoted flag can still
        describe the old zones. Demoting it with the map action also clears it.
        """
        if (map_id := self._pending_promotions.get(serial)) is None:
            return
        if serial in self._map_operations:
            return
        maps = self.robots[serial].maps
        floor_map = next((item for item in maps if item.get("id") == map_id), None)
        active_maps = [item for item in maps if item.get("promoted") is True]
        if floor_map is None or any(item.get("id") != map_id for item in active_maps):
            self._set_pending_promotion(serial, None)
        elif serial not in self._pending_boundary_sync:
            if active_maps:
                self._set_pending_promotion(serial, None)
        elif active_maps:
            if self._pending_boundary_sync[serial]:
                self._set_pending_promotion(serial, None)
        elif floor_map.get("promoted") is False:
            if not self._pending_boundary_sync[serial]:
                self._pending_boundary_sync[serial] = True
                self._persist_map_recovery()

    async def _async_read_maps_locked(self, serial: str) -> None:
        """Refresh authoritative maps and reconcile recovery under the robot lock."""
        robot = self.robots[serial]
        try:
            robot.maps = await self.client.async_maps(serial)
            robot.capabilities["maps_available"] = True
            await self._async_reconcile_map_operation(serial)
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
        """Verify the promoted map and refresh schedules that firmware may disable."""
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
        self._set_pending_promotion(serial, None)

    async def async_set_map_boundaries(
        self, serial: str, map_id: str, boundaries: list[dict[str, Any]]
    ) -> Any:
        """Save Gen4 boundaries and replace an active robot map in one transaction."""
        async with self._lock(serial):
            await self._async_require_map_operation_settled(serial)
            await self._async_read_maps_locked(serial)
            maps = self.robots[serial].maps
            floor_map = next((item for item in maps if item.get("id") == map_id), None)
            if floor_map is None:
                # The map exists (ownership was checked) but is not in use, so
                # the Vacuula app would not offer it for boundary editing either.
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="map_not_in_use"
                )
            if (
                self.has_pending_map_recovery(serial)
                and self._pending_promotions.get(serial) != map_id
            ):
                # A second dirty map would require multiple independent upload
                # guards. Recover the first map, or promote a replacement, first.
                self.validate_map_recovery(serial)
            active = floor_map.get("promoted") is True
            if active:
                self._require_robot_connection(serial)
                already_pending = self.has_pending_map_recovery(serial)
                previous_boundaries = None
                with suppress(VacuulaError):
                    previous_boundaries = await self.client.async_map_boundaries(
                        serial, map_id
                    )
                self._require_robot_connection(serial)
                # The save itself can commit before its HTTP response is lost.
                # Persist first, and do not mistake the old promoted flag for
                # confirmation that the changed boundaries reached the robot.
                self._set_pending_promotion(
                    serial,
                    map_id,
                    boundary_change=(
                        previous_boundaries is None
                        or boundaries != previous_boundaries
                        or serial in self._pending_boundary_sync
                    ),
                )
            if active:
                saved = False
                try:
                    result = None
                    if previous_boundaries != boundaries:
                        result = await self._async_map_request_locked(
                            serial,
                            map_id,
                            "boundaries",
                            boundaries,
                            require_connection=True,
                        )
                    saved = True
                    await self._async_map_request_locked(serial, map_id, "promote")
                except VacuulaError as err:
                    if not saved and isinstance(err, MapRequestNotSent):
                        if not already_pending:
                            self._set_pending_promotion(serial, None)
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        ) from err
                    if (
                        not saved
                        and isinstance(err, MapRequestRejected)
                        and not already_pending
                        and previous_boundaries is not None
                    ):
                        # A fresh unchanged snapshot proves this failed save
                        # introduced no unsynchronized boundary edits. Never
                        # use that fact to clear an earlier uncertain upload.
                        with suppress(VacuulaError):
                            if (
                                await self.client.async_map_boundaries(serial, map_id)
                                == previous_boundaries
                            ):
                                self._set_pending_promotion(serial, None)
                    with suppress(VacuulaError):
                        await self._async_read_maps_locked(serial)
                    await self._async_read_schedule_locked(serial)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="map_promotion_failed",
                    ) from err
                await self._async_confirm_promotion_locked(serial, map_id)
            else:
                previous_boundaries = await self.client.async_map_boundaries(
                    serial, map_id
                )
                if previous_boundaries == boundaries:
                    return None
                result = await self._async_map_request_locked(
                    serial,
                    map_id,
                    "boundaries",
                    boundaries,
                )
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
                        translation_domain=DOMAIN, translation_key="event_not_found"
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
        refresh_maps: bool | Callable[[RobotData], bool] = False,
        refresh_state: bool = False,
    ) -> dict[str, Any]:
        """Select and execute once under the same lock as map changes."""
        async with self._lock(serial):
            self._require_robot_connection(serial)
            try:
                if refresh_state:
                    if not await self._async_read_state_locked(self.robots[serial]):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                    self._require_robot_connection(serial)
                    self.async_update_listeners()
                if (
                    refresh_maps(self.robots[serial])
                    if callable(refresh_maps)
                    else refresh_maps
                ):
                    await self._async_read_maps_locked(serial)
                    self._require_robot_connection(serial)
                if require_map_recovery:
                    self.validate_map_recovery(serial)
                if callable(command):
                    command = await self._async_build_command_locked(serial, command)
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
                robot = self.robots[serial]
                native_command = command.get("cmd")
                clear_home_on_ack = robot.generation == GEN3 and (
                    native_command
                    in {
                        "startCleaning",
                        "startPersistentMapExploration",
                    }
                    or (
                        native_command == "resumeCleaning"
                        and robot.return_phase == "pending"
                    )
                )
                result = await self.client.async_command(serial, command)
                if clear_home_on_ack:
                    # An accepted new task supersedes home intent; rejected
                    # requests leave it intact. Stop waits for actual terminal
                    # telemetry, and resuming an observed paused trip keeps its
                    # home context.
                    clear_return_trip(robot)
                if refresh:
                    await self._async_command_readback_locked(
                        self.robots[serial], command
                    )
            except VacuulaError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
        if (command.get("cmd") or command.get("ability")) in MOTION_COMMANDS:
            self._schedule_state_follow_up(serial)
        if refresh:
            self.async_update_listeners()
        return result

    async def _async_build_command_locked(
        self, serial: str, build: Callable[[RobotData], dict[str, Any]]
    ) -> dict[str, Any]:
        """Build a deferred command, confirming a readiness refusal first.

        Readiness flags can change without a state event, for example when
        pause becomes available a few seconds after the robot leaves the dock.
        A refusal based on cached flags is therefore checked once against a
        fresh state read before it is reported.
        """
        robot = self.robots[serial]
        try:
            return build(robot)
        except ServiceValidationError as err:
            if err.translation_key != "action_unavailable":
                raise
            if not await self._async_read_state_locked(robot):
                raise
            self.async_update_listeners()
            return build(robot)

    @callback
    def _schedule_state_follow_up(self, serial: str) -> None:
        """Read state again a few seconds after the robot starts driving."""
        if (cancel := self._follow_ups.pop(serial, None)) is not None:
            cancel()
        if self._stopping or self.entry.state is not ConfigEntryState.LOADED:
            return

        @callback
        def run(_now: Any) -> None:
            """Start the follow-up read unless the integration is unloading."""
            self._follow_ups.pop(serial, None)
            if self._stopping or not (
                serial in self.robots and self.robots[serial].connected
            ):
                return
            self.entry.async_create_background_task(
                self.hass,
                self._async_state_follow_up(serial),
                f"Vacuula state follow-up {serial}",
            )

        self._follow_ups[serial] = async_call_later(self.hass, FOLLOW_UP_DELAY, run)

    async def _async_state_follow_up(self, serial: str) -> None:
        """Refresh state under the robot lock and notify entities."""
        async with self._lock(serial):
            if await self._async_read_state_locked(self.robots[serial]):
                self.async_update_listeners()

    async def async_return_to_base(self, serial: str) -> None:
        """Pause an active run before docking, without interleaved robot commands."""
        if serial not in self.robots or not self.robots[serial].connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="offline"
            )
        robot = self.robots[serial]

        def command_allowed(flag: str) -> bool | None:
            """Read a native flag, retaining unknown availability as None."""
            key = (
                "availableCommands"
                if robot.generation == GEN3
                else "available_commands"
            )
            commands = robot.state.get(key, {})
            value = commands.get(flag) if isinstance(commands, dict) else None
            return value if isinstance(value, bool) else None

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
            home_started = False
            previous_return = (robot.return_phase, robot.return_deadline)
            try:
                # Positive flags become stale too: the app may have paused or
                # cancelled the run since the last event. Choose the complete
                # pause/home sequence from a fresh state under the same lock.
                if not await self._async_read_state_locked(robot):
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="command_failed"
                    )
                self._require_robot_connection(serial)
                if robot.return_phase in {"pending", "moving"}:
                    # A repeated user request must not pause an observed trip
                    # or send a second Home while its first departure is pending.
                    return
                if command_allowed("pause") is True:
                    await self.client.async_command(serial, pause)
                    await asyncio.sleep(3.5)
                    if not await self._async_read_state_locked(robot):
                        raise HomeAssistantError(
                            translation_domain=DOMAIN, translation_key="command_failed"
                        )
                self._require_robot_connection(serial)
                if command_allowed(home_flag) is not True:
                    # On the live D5, sendToBase in this state was acknowledged
                    # but ended the run where the robot stood instead of
                    # docking it, so the robot's own flag is followed.
                    details = robot.state.get("details")
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="base_not_found"
                        if robot.generation == GEN3
                        and command_allowed(home_flag) is False
                        and isinstance(details, dict)
                        and details.get("isDocked") is False
                        and details.get("dockHasBeenSeen") is False
                        else "action_unavailable",
                    )
                # Correlate even an event preceding the HTTP acknowledgement,
                # but do not report motion from the acknowledgement alone.
                previous_return = (robot.return_phase, robot.return_deadline)
                begin_return_trip(robot)
                home_started = True
                await self.client.async_command(serial, home)
                await self._async_read_state_locked(robot)
            except VacuulaError as err:
                # A lost acknowledgement must not relabel already observed
                # home motion as cleaning. An explicit rejection can discard
                # pending intent, while uncertain delivery remains bounded.
                if isinstance(err, CommandError) and robot.return_phase == "pending":
                    clear_return_trip(robot)
                    if (
                        previous_return[0] == "paused"
                        and monotonic() < previous_return[1]
                    ):
                        robot.return_phase, robot.return_deadline = previous_return
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="command_failed"
                ) from err
            finally:
                if home_started:
                    # Also reconcile a timed out or cancelled HTTP request;
                    # this reads state once and never repeats the home command.
                    self._schedule_state_follow_up(serial)
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
