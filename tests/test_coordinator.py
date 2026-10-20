"""Exercise robot failure isolation, push events, refreshes and action errors."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect, UnsupportedCommand
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData, response_data


def make_client():
    client = MagicMock()
    client.async_server_info = AsyncMock(return_value={"version": "1.0.0"})
    client.async_robots = AsyncMock(
        return_value={
            "TEST-robot": {
                "type": GEN3,
                "name": "Neato",
                "connected": True,
            }
        }
    )
    client.async_command = AsyncMock(return_value={"result": "ok", "data": {}})
    client.async_maps = AsyncMock(return_value=[])
    client.async_runs = AsyncMock(return_value=[])
    return client


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    return VacuulaCoordinator(hass, entry, make_client())


def test_unwrap():
    assert response_data({"result": "ok", "data": {}, "state": 4}) == {"state": 4}
    assert response_data({"body": {"state": "idle"}, "code": 200}) == {"state": "idle"}
    assert response_data({"data": {"a": 1}}) == {"a": 1}
    assert response_data({"data": []}) == {}


async def test_refresh(coordinator):
    result = await coordinator._async_update_data()
    robot = result["TEST-robot"]
    assert robot.connected
    assert robot.capabilities["maps_available"]
    assert coordinator.client.async_command.await_count == 5
    await coordinator._async_update_data()
    assert coordinator.client.async_command.await_count == 6
    coordinator.client.async_robots.return_value = {}
    await coordinator._async_update_data()
    assert not robot.connected


async def test_server_failure(coordinator):
    coordinator.client.async_server_info.side_effect = CannotConnect("offline")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_robot_failure_recovery(coordinator, caplog):
    coordinator.client.async_command.side_effect = CannotConnect("offline")
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    # Vacuula still lists the robot as connected; one failed read (such as its
    # 504 timeout) must not make the robot and its controls unavailable.
    assert coordinator.robots["TEST-robot"].connected
    assert caplog.text.count("robot is not answering") == 1
    coordinator.client.async_command.side_effect = None
    await coordinator._async_update_data()
    assert coordinator.robots["TEST-robot"].connected
    assert "connection recovered" in caplog.text


async def test_optional_failures(coordinator):
    robot = RobotData("TEST-robot", "Neato", GEN3, True)
    error = UnsupportedCommand("unsupported")
    error.status = 400
    coordinator.client.async_command.side_effect = error
    assert await coordinator._async_optional(robot, "getLocalStats") is None
    assert await coordinator._async_optional(robot, "getLocalStats") is None
    assert coordinator.client.async_command.await_count == 1
    coordinator.client.async_command.side_effect = CannotConnect("temporary")
    await coordinator._async_optional(robot, "getGeneralInfo")
    await coordinator._async_optional(robot, "getGeneralInfo")
    assert coordinator.client.async_command.await_count == 3


async def test_gen4_refresh(coordinator):
    coordinator.client.async_robots.return_value = {
        "robot4": {
            "type": GEN4,
            "name": "D10",
            "connected": True,
            "label_serial": "AAAA N2",
        },
        "unknown": {"type": "future.robot"},
    }
    coordinator.client.async_command.return_value = {
        "code": 200,
        "body": {"language": "en"},
    }
    await coordinator._async_update_data()
    robot = coordinator.robots["robot4"]
    assert robot.preferences["language"] == "en"
    assert robot.info["label_serial"] == "AAAA N2"
    assert "unknown" not in coordinator.robots


async def test_commands(coordinator):
    await coordinator._async_update_data()
    robot = coordinator.robots["TEST-robot"]
    robot.preferences = {"robotSounds": True, "brushChangeReminderInterval": 100}
    coordinator.client.async_command.side_effect = lambda serial, payload: (
        {"result": "ok", "data": dict(robot.preferences)}
        if payload.get("cmd") == "getPreferences"
        else {"result": "ok", "data": {}}
    )
    await coordinator.async_command(
        "TEST-robot", {"cmd": "setPreferences", "params": {"robotSounds": False}}
    )
    write = next(
        record.args[1]
        for record in coordinator.client.async_command.await_args_list
        if record.args[1].get("cmd") == "setPreferences"
    )
    assert write["params"] == {
        "robotSounds": False,
        "brushChangeReminderInterval": 100,
    }
    coordinator.client.async_command.side_effect = CannotConnect("offline")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command("TEST-robot", {"cmd": "findMe"})
    robot.connected = False
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command("TEST-robot", {"cmd": "findMe"})
    with pytest.raises(ServiceValidationError):
        await coordinator.async_set_cleaning_option("missing", "mode", "eco")
    await coordinator.async_set_cleaning_option("TEST-robot", "mode", "eco")
    assert coordinator.entry.options["cleaning_options"]["TEST-robot"]["mode"] == "eco"


async def test_push_events(coordinator):
    await coordinator._async_update_data()
    robot = coordinator.robots["TEST-robot"]
    robot.state = {"details": {"charge": 50, "isDocked": True}}
    coordinator._handle_event(
        "TEST-robot", {"data": {"type": "state", "data": {"details": {"charge": 55}}}}
    )
    assert robot.state["details"] == {"charge": 55, "isDocked": True}
    coordinator._handle_event(
        "TEST-robot", {"data": {"eventId": "77", "data": {"state": 2}}}
    )
    assert robot.state["state"] == 2
    coordinator._handle_event("TEST-robot", {"data": []})
    coordinator._handle_event("TEST-robot", {"data": {"type": "other"}})
    robot.generation = GEN4
    coordinator._handle_event(
        "TEST-robot",
        {"data": {"to": {"service": "peer.state"}, "body": {"state": "idle"}}},
    )
    assert robot.state["state"] == "idle"
    # Like the Vacuula app, error-code events are ignored: they also carry
    # informational notices, and peer.state carries the full errors list.
    coordinator._handle_event(
        "TEST-robot",
        {"data": {"to": {"service": "robots.error_codes"}, "body": {"code": 1}}},
    )
    assert "errors" not in robot.state


async def test_stream_lifecycle(coordinator):
    async def robot_stream(serial):
        yield {"_vacuula_stream_opened": True}
        yield {"data": {"type": "state", "data": {"state": 1}}}
        await asyncio.Event().wait()

    async def server_stream():
        yield {"TEST-robot": {"type": GEN3, "name": "Updated", "connected": True}}
        await asyncio.Event().wait()

    coordinator.client.async_robot_events = robot_stream
    coordinator.client.async_server_events = server_stream
    coordinator.entry.mock_state(coordinator.hass, ConfigEntryState.SETUP_IN_PROGRESS)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start()
    for _ in range(5):
        await asyncio.sleep(0)
    assert coordinator.robots["TEST-robot"].name == "Updated"
    assert coordinator.robots["TEST-robot"].state["state"] == 1
    await coordinator._async_snapshot({})
    await coordinator.async_stop()
    assert coordinator._server_stream is None
    assert not coordinator._streams


async def test_reconnect_streams(coordinator):
    calls = 0

    async def server_stream():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CannotConnect("failed")
        coordinator._stopping = True
        if False:
            yield {}

    coordinator.client.async_server_events = server_stream
    with (
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()),
        patch("custom_components.vacuula.coordinator.asyncio.sleep", new=AsyncMock()),
    ):
        await coordinator._async_server_stream()
    assert calls == 2


async def test_robot_stream_error(coordinator):
    await coordinator._async_snapshot({"TEST-robot": {"type": GEN3, "connected": True}})

    async def events(serial):
        coordinator._stopping = True
        raise CannotConnect("failed")
        yield {}

    coordinator.client.async_robot_events = events
    with patch("custom_components.vacuula.coordinator.asyncio.sleep", new=AsyncMock()):
        await coordinator._async_robot_stream("TEST-robot")
