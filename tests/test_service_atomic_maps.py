"""Map-dependent service payloads follow the map active when they are sent."""

# Import the shared fixture for registration in this module.
# ruff: noqa: F811

import asyncio
from copy import deepcopy

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_schedule_merge import schedule_setup  # noqa: F401

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN


@pytest.mark.parametrize("action", ["clean", "new_event", "update_event"])
async def test_promoting_while_action_waits_cannot_send_stale_map_target(
    hass, schedule_setup, action
):
    """Queue a promotion behind the action's first map read using real locks."""
    coordinator, robot, device_id, _, _ = schedule_setup
    robot.state = {"details": {"is_docked": True}}
    remote_maps = [{"id": "old", "promoted": True}] if action == "update_event" else []
    initial_read = asyncio.Event()
    release_read = asyncio.Event()
    promotion_queued = asyncio.Event()
    read_count = 0
    writes = []
    original_command = coordinator.client.async_command.side_effect

    async def maps(serial):
        """Hold the first map snapshot to queue a competing promotion."""
        nonlocal read_count
        assert serial == robot.serial
        read_count += 1
        snapshot = deepcopy(remote_maps)
        if read_count == 1:
            initial_read.set()
            await release_read.wait()
        return snapshot

    async def command(serial, payload):
        """Record the sent target and the floorplan active during dispatch."""
        name = payload["ability"]
        if name in {"cleaning.start", "scheduling.update"}:
            cleaning = (
                payload if name == "cleaning.start" else payload["event"]["command"]
            )
            actual_target = cleaning["runs"][0]["map"]
            promoted = next(
                (item["id"] for item in remote_maps if item["promoted"]), None
            )
            writes.append((deepcopy(actual_target), promoted))
        return await original_command(serial, payload)

    async def promote():
        """Promote a new remote floorplan under the coordinator's robot lock."""
        promotion_queued.set()
        async with coordinator._lock(robot.serial):
            remote_maps[:] = [{"id": "new", "promoted": True}]
            await coordinator._async_read_maps_locked(robot.serial)

    coordinator.client.async_maps.side_effect = maps
    coordinator.client.async_command.side_effect = command
    payload = {"device_id": device_id}
    if action != "clean":
        payload.update(enabled=True, days=["mon"], time="12:00")
    if action == "update_event":
        payload.update(event_id="event1", cleaning_type="auto", zone_ids=[])
    service = "clean" if action == "clean" else "update_schedule_event"
    tasks = []
    try:
        async with asyncio.timeout(5):
            tasks.append(
                hass.async_create_task(
                    hass.services.async_call(DOMAIN, service, payload, blocking=True)
                )
            )
            await initial_read.wait()
            tasks.append(hass.async_create_task(promote()))
            await promotion_queued.wait()
            release_read.set()
            await asyncio.gather(*tasks)
    finally:
        release_read.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(writes) == 1
    sent, promoted = writes[0]
    assert (sent["rank_id"] if isinstance(sent, dict) else None) == promoted


async def test_existing_event_target_change_requires_successful_fresh_map_read(
    hass, schedule_setup
):
    """An unreadable map list cannot turn an existing event into a stale write."""
    coordinator, _, device_id, _, writes = schedule_setup
    coordinator.client.async_maps.side_effect = CannotConnect("unavailable")
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            DOMAIN,
            "update_schedule_event",
            {"device_id": device_id, "event_id": "event1", "cleaning_type": "auto"},
            blocking=True,
        )
    assert error.value.translation_key == "command_failed"
    assert writes == []
    coordinator.client.async_maps.assert_awaited_once()
    coordinator.client.async_command.assert_not_awaited()
