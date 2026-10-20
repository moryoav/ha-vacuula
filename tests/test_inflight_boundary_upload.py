"""Protect newer boundary edits from an older upload still running on the server."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import start_payload


@pytest.mark.parametrize("reload_entry", [False, True])
async def test_old_cancelled_upload_cannot_confirm_later_boundary_changes(
    hass, reload_entry
):
    """Reject newer edits or retain their guard when an older upload completes late.

    Vacuula's HTTP handler continues waiting for the robot after its client
    disconnects. Its earlier boundary snapshot can become active after another
    Home Assistant request sees the temporarily demoted map and edits it again.
    """
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local"})
    entry.add_to_hass(hass)
    remote = {"promoted": True, "saved": [], "uploaded": []}
    promotion_started = asyncio.Event()
    release_promotion = asyncio.Event()
    server_tasks = []
    boundary_writes = []
    first_zones = [
        {
            "id": "first",
            "name": "First no-go",
            "color": "#ff0000",
            "type": "polygon",
            "function": "nogo",
            "enabled": True,
            "vertices": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
        }
    ]
    second_zones = [
        {
            **deepcopy(first_zones[0]),
            "id": "second",
            "name": "Second no-go",
            "vertices": [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4]],
        }
    ]
    client = MagicMock()

    async def maps(serial):
        """Read the server flag independently from the interrupted HTTP request."""
        return [{"id": "map1", "promoted": remote["promoted"]}]

    async def boundaries(serial, map_id):
        """Read the database's current complete boundary set."""
        return deepcopy(remote["saved"])

    async def action(serial, map_id, name, payload=None):
        """Keep an upload alive after cancellation of the client's waiting request."""
        if name == "boundaries":
            boundary_writes.append(deepcopy(payload))
            remote["saved"] = deepcopy(payload)
            return {}
        assert name == "promote"
        upload_snapshot = deepcopy(remote["saved"])
        remote["promoted"] = False

        async def finish_upload():
            """Finish the robot upload using the server's already captured zones."""
            promotion_started.set()
            await release_promotion.wait()
            remote["uploaded"] = upload_snapshot
            remote["promoted"] = True
            return {}

        server_task = asyncio.create_task(finish_upload())
        server_tasks.append(server_task)
        return await asyncio.shield(server_task)

    client.async_maps = AsyncMock(side_effect=maps)
    client.async_map_boundaries = AsyncMock(side_effect=boundaries)
    client.async_server_boot_id = AsyncMock(return_value='"111111111111"')
    client.async_map_action = AsyncMock(side_effect=action)
    client.async_command = AsyncMock(return_value={"body": {}})
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots["robot"] = RobotData(
        "robot",
        "Neato",
        GEN4,
        True,
        state={"details": {"is_docked": True}},
    )
    first_request = asyncio.create_task(
        coordinator.async_set_map_boundaries("robot", "map1", first_zones)
    )
    try:
        async with asyncio.timeout(5):
            await promotion_started.wait()
            first_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_request
            assert coordinator.has_pending_map_recovery("robot")

            if reload_entry:
                replacement = VacuulaCoordinator(hass, entry, client)
                replacement.robots = deepcopy(coordinator.robots)
                coordinator = replacement

            # The first server handler still owns an upload of first_zones.
            try:
                await coordinator.async_set_map_boundaries(
                    "robot", "map1", second_zones
                )
            except HomeAssistantError:
                # Refusing to overlap uploads is also a safe resolution.
                assert boundary_writes == [first_zones]
            else:
                assert remote["saved"] == second_zones
            assert coordinator.has_pending_map_recovery("robot")

            release_promotion.set()
            await asyncio.gather(*server_tasks)
            assert remote["uploaded"] == first_zones
            await coordinator.async_refresh_maps("robot")
            if remote["saved"] != remote["uploaded"]:
                assert coordinator.has_pending_map_recovery("robot")
                with pytest.raises(ServiceValidationError) as error:
                    start_payload(coordinator, "robot")
                assert error.value.translation_key == "map_promotion_required"
    finally:
        first_request.cancel()
        for task in server_tasks:
            task.cancel()
        await asyncio.gather(first_request, *server_tasks, return_exceptions=True)
