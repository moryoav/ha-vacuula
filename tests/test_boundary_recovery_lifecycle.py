"""Preserve unsynchronized no-go zones across later map recovery attempts."""

import asyncio
from copy import deepcopy

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from test_boundary_sync_uncertainty import (
    BOUNDARIES,
    uncertain_boundary_setup,  # noqa: F401
)

from custom_components.vacuula.client import CannotConnect, CommandError
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.vacuum import start_payload


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_alternate_promotion_preserves_old_boundary_guard(
    uncertain_boundary_setup,  # noqa: F811
    cancelled,
):
    """A failed switch must not authorize the old map with unapplied new zones."""
    coordinator, remote = uncertain_boundary_setup
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert remote["saved"] == BOUNDARIES
    assert remote["uploaded"] == []
    assert coordinator.has_pending_map_recovery("robot")

    # Both maps are in use, but the original map remains the robot's active one.
    coordinator.client.async_maps.return_value = [
        {"id": "map1", "promoted": True},
        {"id": "map2", "promoted": False},
    ]
    rejection = (
        asyncio.CancelledError()
        if cancelled
        else CommandError("Robot not connected", status=400)
    )
    coordinator.client.async_map_action.side_effect = rejection
    with pytest.raises(asyncio.CancelledError if cancelled else HomeAssistantError):
        await coordinator.async_map_operation("robot", "map2", "promote")

    # A restart and fresh read still see map1 promoted with its old uploaded zones.
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    replacement.robots = deepcopy(coordinator.robots)
    await replacement.async_refresh_maps("robot")
    assert replacement.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError) as error:
        start_payload(replacement, "robot")
    assert error.value.translation_key == "map_promotion_required"


@pytest.mark.parametrize("read_failed", [False, True])
async def test_successful_alternate_promotion_resolves_old_guard_when_confirmed(
    uncertain_boundary_setup,  # noqa: F811
    read_failed,
):
    """Resolve the old guard once a newly uploaded different map is confirmed."""
    coordinator, _remote = uncertain_boundary_setup
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.has_pending_map_recovery("robot")

    original_active = [
        {"id": "map1", "promoted": True},
        {"id": "map2", "promoted": False},
    ]
    replacement_active = [
        {"id": "map1", "promoted": False},
        {"id": "map2", "promoted": True},
    ]
    coordinator.client.async_maps.side_effect = [
        original_active,
        CannotConnect("Confirmation read failed")
        if read_failed
        else replacement_active,
        replacement_active,
    ]
    coordinator.client.async_map_action.side_effect = None
    coordinator.client.async_map_action.return_value = {}
    if read_failed:
        with pytest.raises(HomeAssistantError):
            await coordinator.async_map_operation("robot", "map2", "promote")
        assert coordinator.has_pending_map_recovery("robot")
        replacement = VacuulaCoordinator(
            coordinator.hass, coordinator.entry, coordinator.client
        )
        replacement.robots = deepcopy(coordinator.robots)
        await replacement.async_refresh_maps("robot")
        coordinator = replacement
    else:
        await coordinator.async_map_operation("robot", "map2", "promote")

    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"] == {
        "rank_id": "map2",
        "track_id": None,
        "nogo_enabled": True,
    }
