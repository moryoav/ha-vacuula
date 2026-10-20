"""Preserve newer server events when an older robot-list HTTP reply completes."""

import asyncio
from copy import deepcopy

import pytest
from test_coordinator import coordinator  # noqa: F401

from custom_components.vacuula.const import GEN4
from custom_components.vacuula.data import RobotData


@pytest.mark.parametrize("connected", [False, True])
async def test_server_event_supersedes_inflight_robot_list(
    coordinator,  # noqa: F811
    connected,
):
    """Do not restore old connection flags or names after a newer push snapshot."""
    started = asyncio.Event()
    applied = asyncio.Event()
    old = {"TEST-robot": {"type": GEN4, "name": "Old", "connected": not connected}}
    latest = {"TEST-robot": {"type": GEN4, "name": "Renamed", "connected": connected}}

    async def robot_list():
        """Return a captured old list after its newer event has already been applied."""
        snapshot = deepcopy(old)
        started.set()
        await applied.wait()
        return snapshot

    async def server_events():
        """Deliver a full newer snapshot while the independent HTTP request waits."""
        await started.wait()
        yield latest
        applied.set()
        await asyncio.Event().wait()

    coordinator.client.async_robots.side_effect = robot_list
    coordinator.client.async_server_events = server_events
    stream = asyncio.create_task(coordinator._async_server_stream())
    try:
        await asyncio.wait_for(coordinator._async_update_data(), timeout=2)
        robot = coordinator.robots["TEST-robot"]
        assert robot.connected is connected
        assert robot.name == "Renamed"
        assert coordinator.entry.data["robot_ids"] == ["TEST-robot"]
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)


async def test_old_empty_poll_does_not_clear_newer_recovery_guard(
    coordinator,  # noqa: F811
):
    """An out-of-date empty list must not treat a currently listed robot as removed."""
    serial = "TEST-robot"
    coordinator.robots[serial] = RobotData(serial, "Neato", GEN4, True)
    coordinator._set_pending_promotion(serial, "floor", boundary_change=True)
    coordinator._map_operations[serial] = {"action": "recovery", "map_id": "floor"}
    coordinator.client.async_maps.return_value = [{"id": "floor", "promoted": False}]
    started = asyncio.Event()
    applied = asyncio.Event()

    async def robot_list():
        """Hold an empty response until the listed-robot event has been applied."""
        started.set()
        await applied.wait()
        return {}

    async def server_events():
        """Report the connected robot before the older empty poll returns."""
        await started.wait()
        yield {serial: {"type": GEN4, "name": "Neato", "connected": True}}
        applied.set()
        await asyncio.Event().wait()

    coordinator.client.async_robots.side_effect = robot_list
    coordinator.client.async_server_events = server_events
    stream = asyncio.create_task(coordinator._async_server_stream())
    try:
        await asyncio.wait_for(coordinator._async_update_data(), timeout=2)
        assert coordinator.has_pending_map_recovery(serial)
        assert coordinator.robots[serial].listed
        assert coordinator.entry.data["robot_ids"] == [serial]
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)


async def test_newer_server_removal_is_not_undone_by_old_nonempty_poll(
    coordinator,  # noqa: F811
):
    """Keep an authoritative removal instead of resurrecting the older robot record."""
    serial = "TEST-robot"
    coordinator.robots[serial] = RobotData(serial, "Neato", GEN4, True)
    coordinator._set_pending_promotion(serial, "floor", boundary_change=True)
    started = asyncio.Event()
    applied = asyncio.Event()

    async def robot_list():
        """Return the previous robot list after the server has reported its removal."""
        old = {serial: {"type": GEN4, "name": "Old", "connected": True}}
        started.set()
        await applied.wait()
        return old

    async def server_events():
        """Apply the real removal before releasing the previous HTTP snapshot."""
        await started.wait()
        yield {}
        applied.set()
        await asyncio.Event().wait()

    coordinator.client.async_robots.side_effect = robot_list
    coordinator.client.async_server_events = server_events
    stream = asyncio.create_task(coordinator._async_server_stream())
    try:
        await asyncio.wait_for(coordinator._async_update_data(), timeout=2)
        assert not coordinator.has_pending_map_recovery(serial)
        assert not coordinator.robots[serial].listed
        assert not coordinator.robots[serial].connected
        assert coordinator.entry.data["robot_ids"] == []
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
