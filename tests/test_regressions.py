"""Exercise resource caching, independent polling and failure cleanup."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from test_client import make_client as protocol_client
from test_config_flow import discovery
from test_coordinator import make_client as coordinator_client
from test_discovery_address import offline

from custom_components.vacuula import async_remove_config_entry_device
from custom_components.vacuula.client import (
    CannotConnect,
    InvalidAuth,
    UnsupportedCommand,
    VacuulaError,
    _SSEParser,
)
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.image import IMAGE_MAX_AGE, VacuulaMapImage
from custom_components.vacuula.services import _resolve, async_setup_services


@pytest.fixture
def resource_coordinator(hass):
    """Create a coordinator backed by the shared generation-aware mock client."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    return VacuulaCoordinator(hass, entry, coordinator_client())


async def test_poll_is_independent_of_push_and_removed_on_unload(
    resource_coordinator, freezer
):
    """Repeated pushes cannot postpone the sixty-second fallback indefinitely."""
    coordinator = resource_coordinator
    await coordinator._async_update_data()
    coordinator.entry.mock_state(coordinator.hass, ConfigEntryState.SETUP_IN_PROGRESS)

    async def server_events():
        """Keep the server event stream open without yielding pushes."""
        await asyncio.Event().wait()
        yield {}

    async def robot_events(serial):
        """Keep a robot event stream open while fallback polling is exercised."""
        await asyncio.Event().wait()
        yield {}

    coordinator.client.async_server_events = server_events
    coordinator.client.async_robot_events = robot_events
    now = dt_util.utcnow()
    with patch.object(coordinator, "async_request_refresh", new=AsyncMock()) as refresh:
        await coordinator.async_start()
        try:
            for elapsed in (15, 30, 45, 60):
                coordinator._handle_event(
                    "TEST-robot",
                    {"data": {"type": "state", "data": {"state": elapsed}}},
                )
                freezer.move_to(now + timedelta(seconds=elapsed))
                async_fire_time_changed(coordinator.hass, dt_util.utcnow())
                await coordinator.hass.async_block_till_done()
                assert refresh.await_count == (1 if elapsed == 60 else 0)
        finally:
            await coordinator.async_stop()
        freezer.move_to(now + timedelta(seconds=121))
        async_fire_time_changed(coordinator.hass, dt_util.utcnow())
        await coordinator.hass.async_block_till_done()
        await coordinator._async_poll(dt_util.utcnow())
        assert refresh.await_count == 1


async def test_nullable_capabilities_keep_known_services(resource_coordinator):
    """Retain known services after null or malformed capability replies."""
    coordinator = resource_coordinator
    coordinator.client.async_command.return_value = {
        "result": "ok",
        "data": {},
        "availableServices": {"findMe": True},
    }
    await coordinator._async_update_data()
    robot = coordinator.robots["TEST-robot"]
    coordinator._handle_event(
        robot.serial,
        {"data": {"type": "state", "data": {"availableServices": None}}},
    )
    assert robot.capabilities["findMe"] is True
    assert robot.state["availableServices"] is None
    coordinator.client.async_command.return_value = {
        "result": "ok",
        "data": {},
        "availableServices": ["wrong-shape"],
    }
    await coordinator.async_refresh_robot(robot.serial)
    assert robot.connected
    assert robot.capabilities["findMe"] is True


async def test_snapshot_cancels_offline_stream_and_restarts_on_reconnect(
    resource_coordinator,
):
    """Cancel disconnected streams and start a new stream only after reconnection."""
    coordinator = resource_coordinator
    coordinator.entry.mock_state(coordinator.hass, ConfigEntryState.SETUP_IN_PROGRESS)
    await coordinator._async_snapshot({"TEST-robot": {"type": GEN3, "connected": True}})

    async def events(serial):
        """Wait indefinitely so snapshot-driven stream cancellation can be observed."""
        await asyncio.Event().wait()
        yield {}

    coordinator.client.async_robot_events = events
    coordinator._server_stream = MagicMock()
    coordinator._ensure_stream("TEST-robot")
    first_task = coordinator._streams["TEST-robot"]
    coordinator._ensure_stream("TEST-robot")
    assert coordinator._streams["TEST-robot"] is first_task
    await coordinator._async_snapshot(
        {"TEST-robot": {"type": GEN3, "connected": False}}
    )
    await asyncio.sleep(0)
    assert first_task.cancelled()
    assert not coordinator._streams
    coordinator._ensure_stream("TEST-robot")
    assert not coordinator._streams
    await coordinator._async_snapshot({"TEST-robot": {"type": GEN3, "connected": True}})
    assert coordinator._streams["TEST-robot"] is not first_task
    coordinator._server_stream = None
    await coordinator.async_stop()
    coordinator._ensure_stream("TEST-robot")
    assert not coordinator._streams


@pytest.mark.parametrize(
    ("generation", "payload"),
    [
        (GEN3, {"type": "state", "data": []}),
        (GEN4, {"to": []}),
        (GEN4, {"to": {"service": "other"}, "body": {"state": "idle"}}),
        (GEN4, {"to": {"service": "peer.state"}, "body": []}),
        (GEN4, {"to": {"service": "robots.error_codes"}, "body": []}),
    ],
)
async def test_malformed_and_unrelated_events_preserve_state(
    resource_coordinator, generation, payload
):
    """Ignore malformed or unrelated push payloads without publishing changed state."""
    coordinator = resource_coordinator
    robot = RobotData("TEST-robot", "Neato", generation, True, state={"state": 1})
    coordinator.robots = {robot.serial: robot}
    with patch.object(coordinator, "async_set_updated_data") as updated:
        coordinator._handle_event(robot.serial, {"data": payload})
    assert robot.state == {"state": 1}
    updated.assert_not_called()


async def test_optional_missing_response_is_retried(resource_coordinator):
    """Retry optional reads after transient 404 replies rather than disabling them."""
    coordinator = resource_coordinator
    robot = RobotData("TEST-robot", "Neato", GEN3, True)
    coordinator.client.async_command.side_effect = VacuulaError("missing", status=404)
    assert await coordinator._async_optional(robot, "getLocalStats") is None
    assert await coordinator._async_optional(robot, "getLocalStats") is None
    assert coordinator.client.async_command.await_count == 2


async def test_optional_detail_failure_preserves_cached_metadata(resource_coordinator):
    """Retain cached robot metadata when optional detail commands are unsupported."""
    coordinator = resource_coordinator
    robot = RobotData("TEST-robot", "Neato", GEN3, True, info={"modelName": "D7"})
    coordinator.robots = {robot.serial: robot}

    async def command(serial, payload):
        """Answer state reads and reject every optional command as unsupported."""
        if payload == {"cmd": "getRobotState"}:
            return {"result": "ok", "state": 1}
        raise UnsupportedCommand("unsupported")

    coordinator.client.async_command.side_effect = command
    await coordinator.async_refresh_robot(robot.serial)
    assert robot.connected
    assert robot.state == {"state": 1}
    assert robot.info == {"modelName": "D7"}
    calls = coordinator.client.async_command.await_count
    await coordinator.async_refresh_robot(robot.serial, full=True)
    assert coordinator.client.async_command.await_count == calls + 1


async def test_gen4_absent_language_does_not_discard_cleaning_preferences(
    resource_coordinator,
):
    """Retain Gen4 cleaning options even when the language response is empty."""
    coordinator = resource_coordinator
    robot = RobotData("TEST-robot", "Neato", GEN4, True)
    coordinator.robots = {robot.serial: robot}

    async def command(serial, payload):
        """Expose only cleaning preferences and return empty bodies for other reads."""
        return {
            "code": 200,
            "body": {"mode": "eco"}
            if payload == {"ability": "settings.cleaning_options"}
            else {},
        }

    coordinator.client.async_command.side_effect = command
    await coordinator.async_refresh_robot(robot.serial)
    assert robot.preferences == {"mode": "eco"}


async def test_clean_robot_stream_closure_enters_bounded_retry(resource_coordinator):
    """Close an exhausted event stream before entering its bounded retry delay."""
    coordinator = resource_coordinator
    coordinator.robots = {"TEST-robot": RobotData("TEST-robot", "Neato", GEN3, True)}
    closed = False

    async def events(serial):
        """Yield one state push and record cleanup when the stream closes."""
        nonlocal closed
        try:
            yield {"data": {"type": "state", "data": {"state": 2}}}
        finally:
            closed = True

    async def retry(delay):
        """Check the first retry delay and prevent another attempt."""
        assert delay == 1
        coordinator._stopping = True

    coordinator.client.async_robot_events = events
    with patch("custom_components.vacuula.coordinator.asyncio.sleep", new=retry):
        await coordinator._async_robot_stream("TEST-robot")
    assert closed
    assert coordinator.robots["TEST-robot"].state == {"state": 2}


async def test_offline_resources_refresh_without_robot_command(resource_coordinator):
    """Refresh offline robots' server resources without robot commands."""
    coordinator = resource_coordinator
    coordinator.client.async_robots.return_value["TEST-robot"]["connected"] = False
    coordinator.client.async_maps.return_value = [{"id": "floor", "name": "Home"}]
    coordinator.client.async_runs.return_value = [{"id": "last-run"}]
    with patch("custom_components.vacuula.coordinator.monotonic", return_value=1):
        await coordinator._async_update_data()
        robot = coordinator.robots["TEST-robot"]
        assert not robot.connected
        assert robot.maps == [{"id": "floor", "name": "Home"}]
        assert robot.runs == [{"id": "last-run"}]
        assert robot.capabilities["maps_available"]
        await coordinator.async_refresh_robot(robot.serial)
        coordinator.client.async_maps.assert_awaited_once()
        coordinator.client.async_command.assert_not_awaited()
        coordinator.client.async_maps.side_effect = CannotConnect("offline")
        await coordinator.async_refresh_robot(robot.serial, full=True)
        assert robot.maps == [{"id": "floor", "name": "Home"}]
        assert robot.runs == [{"id": "last-run"}]
        assert coordinator.client.async_maps.await_count == 2
    with patch("custom_components.vacuula.coordinator.monotonic", return_value=301):
        coordinator.client.async_maps.side_effect = None
        coordinator.client.async_maps.return_value = []
        await coordinator.async_refresh_robot(robot.serial)
    assert robot.maps == []
    assert coordinator.client.async_maps.await_count == 3
    coordinator.client.async_command.assert_not_awaited()


async def test_map_operation_serializes_with_robot_command(resource_coordinator):
    """Hold robot commands until an in-flight map operation releases the shared lock."""
    coordinator = resource_coordinator
    await coordinator._async_update_data()
    coordinator.client.async_command.reset_mock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def map_action(*args):
        """Block the map request until the test releases its transaction."""
        entered.set()
        await release.wait()
        return {"result": "ok"}

    coordinator.client.async_map_action = AsyncMock(side_effect=map_action)
    map_task = asyncio.create_task(
        coordinator.async_map_operation("TEST-robot", "floor", "use")
    )
    await entered.wait()
    command_task = asyncio.create_task(
        coordinator.async_command("TEST-robot", {"cmd": "findMe"})
    )
    await asyncio.sleep(0)
    coordinator.client.async_command.assert_not_awaited()
    release.set()
    assert await map_task == {"result": "ok"}
    await command_task
    assert coordinator.client.async_command.await_args_list[0].args == (
        "TEST-robot",
        {"cmd": "findMe"},
    )


async def test_server_map_settings_update_and_refresh_while_robot_offline(
    resource_coordinator,
):
    """Allow server-side map changes and readback while the robot is disconnected."""
    coordinator = resource_coordinator
    coordinator.robots = {"TEST-robot": RobotData("TEST-robot", "Neato", GEN3, False)}
    coordinator.client.async_update_map = AsyncMock()
    await coordinator.async_update_map("TEST-robot", "floor", "Downstairs", False)
    coordinator.client.async_update_map.assert_awaited_once_with(
        "TEST-robot", "floor", "Downstairs", False
    )
    # The map list is read back under the same lock, without robot commands.
    coordinator.client.async_maps.assert_awaited_once_with("TEST-robot")
    coordinator.client.async_command.assert_not_awaited()


async def test_failed_map_settings_update_is_not_retried_or_confirmed(
    resource_coordinator,
):
    """Report a failed map write without retrying or performing a success refresh."""
    from homeassistant.exceptions import HomeAssistantError

    coordinator = resource_coordinator
    coordinator.robots = {"TEST-robot": RobotData("TEST-robot", "Neato", GEN3, False)}
    coordinator.client.async_update_map = AsyncMock(side_effect=CannotConnect("failed"))
    with patch.object(coordinator, "async_refresh_robot", new=AsyncMock()) as refresh:
        with pytest.raises(HomeAssistantError):
            await coordinator.async_update_map(
                "TEST-robot", "floor", "Downstairs", False
            )
    coordinator.client.async_update_map.assert_awaited_once()
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.NOT_LOADED,
    ],
)
async def test_rediscovery_updates_same_robots_without_duplicate(hass, state):
    """Update a known server's address and reload only entries requiring reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="http://old.local:81",
        data={"url": "http://old.local:81", "robot_ids": ["TEST-robot"]},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, state)
    with (
        patch("custom_components.vacuula.config_flow.VacuulaClient") as client_cls,
        patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload,
    ):
        client_cls.return_value.async_server_info = AsyncMock(return_value={})
        client_cls.return_value.async_robots = AsyncMock(
            return_value={"TEST-robot": {}}
        )
        # The old address no longer answers, so discovery replaces it.
        offline(client_cls, "http://old.local:81")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "zeroconf"}, data=discovery()
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {
        "url": "http://192.0.2.1:81",
        "robot_ids": ["TEST-robot"],
        "url_aliases": [
            "http://192.0.2.1:81",
            "http://old.local:81",
            "http://vacuula.local:81",
        ],
    }
    assert entry.unique_id == "http://192.0.2.1:81"
    if state is ConfigEntryState.NOT_LOADED:
        reload.assert_not_awaited()
    else:
        reload.assert_awaited_once_with(entry.entry_id)


async def test_reconfigure_rejects_robots_owned_by_other_entry(hass):
    """Reject reconfiguration to a server whose robot belongs to another entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"url": "http://old.local:81", "robot_ids": []}
    )
    entry.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://different.local:81", "robot_ids": ["other-robot"]},
    ).add_to_hass(hass)
    with patch("custom_components.vacuula.config_flow.VacuulaClient") as client_cls:
        client_cls.return_value.async_server_info = AsyncMock(return_value={})
        client_cls.return_value.async_robots = AsyncMock(
            return_value={"other-robot": {}}
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
            data={"url": "http://new.local:81"},
        )
    assert result["reason"] == "already_configured"
    assert entry.data["url"] == "http://old.local:81"


async def test_cached_image_follows_map_changes_not_token_refreshes(
    resource_coordinator,
):
    """Reuse a map image across token refreshes and reread it after map changes."""
    coordinator = resource_coordinator
    robot = RobotData("TEST-robot", "Neato", GEN3, True, maps=[{"id": "floor"}])
    coordinator.robots = {robot.serial: robot}
    coordinator.async_set_updated_data(coordinator.robots)
    coordinator.client.async_map = AsyncMock(
        return_value={"width": 1, "height": 1, "rle": [[0, 1]]}
    )
    entity = VacuulaMapImage(coordinator, robot.serial, "floor")
    entity.hass = coordinator.hass
    clock = patch("custom_components.vacuula.image.monotonic", return_value=1000)
    with clock as now, patch.object(entity, "async_write_ha_state"):
        first_image = await entity.async_image()
        first_updated = entity.image_last_updated
        # Viewers fetch again after each five-minute token rotation.
        now.return_value = 1000 + 30 * 60
        entity._handle_coordinator_update()
        assert await entity.async_image() == first_image
        assert coordinator.client.async_map.await_count == 1
        assert entity.image_last_updated == first_updated

        # A changed map record makes the next fetch read the map again. An
        # unchanged picture keeps its timestamp, so no new image is reported.
        robot.maps = [{"id": "floor", "name": "Upstairs"}]
        entity._handle_coordinator_update()
        assert await entity.async_image() == first_image
        assert coordinator.client.async_map.await_count == 2
        assert entity.image_last_updated == first_updated

        # Grid changes without a record change are picked up after an hour.
        coordinator.client.async_map.return_value["rle"] = [[1, 1]]
        now.return_value = 1000 + 30 * 60 + IMAGE_MAX_AGE
        changed = await entity.async_image()
        assert changed != first_image
        assert coordinator.client.async_map.await_count == 3
        assert entity.image_last_updated != first_updated


@pytest.mark.parametrize(
    ("generation", "response", "command"),
    [
        (GEN3, {"result": "ok", "state": 1}, {"cmd": "getRobotState"}),
        (GEN4, {"code": 200, "body": {"state": "idle"}}, {"ability": "state.show"}),
    ],
)
async def test_robot_state_uses_generation_and_unwraps(generation, response, command):
    """Send the matching generation's state command and unwrap its response body."""
    client, session, _ = protocol_client(response)
    assert await client.async_robot_state("robot", generation) == (
        {"state": 1} if generation == GEN3 else {"state": "idle"}
    )
    assert session.request.call_args.kwargs["json"] == command


async def test_unknown_generation_never_sends_command():
    """Reject an unsupported robot generation before making a network request."""
    client, session, _ = protocol_client()
    with pytest.raises(UnsupportedCommand):
        await client.async_robot_state("robot", "future")
    session.request.assert_not_called()


async def test_boundaries_alias_keeps_exact_resource_path():
    """Read boundaries through the expected map resource path."""
    client, session, _ = protocol_client([{"id": "zone"}])
    assert await client.async_boundaries("robot", "map") == [{"id": "zone"}]
    assert session.request.call_args.args[1].raw_path.endswith("/maps/map/boundaries")


async def test_sse_close_releases_http_context_before_returning():
    """Release the HTTP context when the event generator closes."""
    client, _, response = protocol_client(
        raw=b'data: {"serial":"robot","data":{}}\n\n',
        headers={"Content-Type": "text/event-stream"},
    )
    stream = client.async_robot_events("robot")
    assert await anext(stream) == {"_vacuula_stream_opened": True}
    await stream.aclose()
    response.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("exception", [aiohttp.ClientConnectionError(), TimeoutError()])
async def test_sse_transport_error_is_safe(exception):
    """Translate event connection failures into the public connection error type."""
    client, session, response = protocol_client()
    response.__aenter__.side_effect = exception
    with pytest.raises(CannotConnect, match="event connection failed"):
        await anext(client.async_server_events())
    session.request.assert_called_once()


@pytest.mark.parametrize("status", [401, 403])
async def test_sse_authentication_error_is_distinct(status):
    """Distinguish authentication failures from other event connection errors."""
    client, _, _ = protocol_client(status=status)
    with pytest.raises(InvalidAuth):
        await anext(client.async_server_events())


def test_sse_ignores_unknown_fields_and_rejects_invalid_event_encoding():
    """Ignore unsupported SSE fields and reject invalid UTF-8 event names."""
    parser = _SSEParser()
    assert parser.feed(b"id: 2\nretry: 100\ndata: {}\n\n") == [("message", b"{}")]
    with pytest.raises(VacuulaError, match="event encoding"):
        parser.feed(b"event: \xff\n")


async def test_unsupported_http_status_has_specific_error():
    """Translate HTTP 501 into an unsupported-command error without leaking the body."""
    client, _, _ = protocol_client(raw=b"PRIVATE", status=501)
    with pytest.raises(UnsupportedCommand):
        await client.async_server_info()


async def test_device_removal_refuses_when_server_unreachable(resource_coordinator):
    """Keep the device when an unreachable server cannot confirm robot removal."""
    coordinator = resource_coordinator
    coordinator.entry.runtime_data = coordinator
    coordinator.client.async_robots.side_effect = CannotConnect("offline")
    device = MagicMock(identifiers={(DOMAIN, "TEST-robot")})
    assert not await async_remove_config_entry_device(
        coordinator.hass, coordinator.entry, device
    )


async def test_device_from_other_integration_is_rejected(hass):
    """Reject an advanced-action target owned by a different integration."""
    entry = MockConfigEntry(domain="other", data={})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("other", "robot")}
    )
    with pytest.raises(ServiceValidationError):
        _resolve(hass, device.id)


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        ("get_data", {"resource": "maps"}),
        ("factory_reset", {"confirm": True}),
    ],
)
async def test_advanced_service_rejects_non_admin_before_resolving_device(
    hass, hass_read_only_user, service, payload
):
    """Enforce administrator access before resolving advanced-action device targets."""
    async_setup_services(hass)
    with patch("custom_components.vacuula.services._resolve") as resolve:
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                service,
                {"device_id": "unknown", **payload},
                blocking=True,
                return_response=True,
                context=Context(user_id=hass_read_only_user.id),
            )
    resolve.assert_not_called()
