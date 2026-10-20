"""Keep pre-dispatch connection failures distinct from ambiguous map writes."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_map_operation_fences import BOUNDARIES, map_fence  # noqa: F401

from custom_components.vacuula.client import CannotConnect, MapRequestNotSent


async def test_unsent_promotion_clears_its_new_guard_without_another_read(
    map_fence,  # noqa: F811
):
    """A failed connection cannot demote a map even if subsequent reads fail too."""
    coordinator, remote = map_fence
    coordinator.client.async_maps.side_effect = [
        remote["maps"],
        CannotConnect("Server unavailable"),
    ]
    coordinator.client.async_map_action.side_effect = MapRequestNotSent("Not connected")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_map_operation("robot", "map1", "promote")
    assert not coordinator.has_pending_map_recovery("robot")
    assert coordinator.client.async_maps.await_count == 1
    coordinator.client.async_server_boot_id.assert_not_awaited()


async def test_unsent_boundary_save_clears_only_its_new_guard(map_fence):  # noqa: F811
    """No HTTP request was sent, so no boundary edit or upload needs recovery."""
    coordinator, _remote = map_fence
    coordinator.client.async_map_action.side_effect = MapRequestNotSent("Not connected")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert not coordinator.has_pending_map_recovery("robot")
    coordinator.client.async_server_boot_id.assert_not_awaited()


async def test_unsent_promotion_after_saved_boundaries_keeps_upload_guard(
    map_fence,  # noqa: F811
):
    """A later failed connection cannot undo a boundary edit already saved by HTTP."""
    coordinator, remote = map_fence

    async def save_then_refuse_connection(serial, map_id, action, payload=None):
        """Commit the boundary POST, then fail to connect for its separate upload."""
        if action == "boundaries":
            remote["boundaries"] = payload
            return None
        raise MapRequestNotSent("Not connected")

    coordinator.client.async_map_action.side_effect = save_then_refuse_connection
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.has_pending_map_recovery("robot")
    assert not coordinator._map_operations
    coordinator.client.async_server_boot_id.assert_not_awaited()


async def test_unsent_recovery_promotion_preserves_its_original_guard(map_fence):  # noqa: F811
    """A connection refusal on a recovery attempt must retain earlier dirty zones."""
    coordinator, _remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator.client.async_map_action.side_effect = MapRequestNotSent("Not connected")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_map_operation("robot", "map1", "promote")
    assert coordinator._pending_promotions == {"robot": "map1"}
    assert coordinator._map_operations["robot"]["action"] == "recovery"


async def test_unsent_metadata_write_introduces_no_recovery(map_fence):  # noqa: F811
    """A failed initial connection for a hidden map cannot create delayed work."""
    coordinator, remote = map_fence
    remote["maps"] = []
    coordinator.client.async_map_action = AsyncMock(
        side_effect=MapRequestNotSent("Not connected")
    )
    with pytest.raises(MapRequestNotSent):
        await coordinator.async_map_operation("robot", "map1", "use")
    assert not coordinator.has_pending_map_recovery("robot")
    coordinator.client.async_server_boot_id.assert_not_awaited()
