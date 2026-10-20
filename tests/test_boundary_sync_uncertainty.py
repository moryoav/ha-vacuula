"""Keep changed server boundaries guarded until the robot receives the new map."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect, MapRequestRejected
from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import start_payload

BOUNDARIES = [
    {
        "id": "new-no-go",
        "name": "Keep clear",
        "color": "#ff0000",
        "type": "polygon",
        "vertices": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
        "enabled": True,
        "function": "nogo",
    }
]


@pytest.fixture
def uncertain_boundary_setup(hass):
    """Model committed server edits separately from the robot's uploaded boundaries."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_server_boot_id = AsyncMock(return_value=None)
    client.async_maps = AsyncMock(return_value=[{"id": "map1", "promoted": True}])
    client.async_command = AsyncMock(return_value={"body": {}})
    remote = {"saved": [], "uploaded": [], "failure": "boundary_response"}

    async def map_boundaries(serial, map_id):
        """Return the current persisted set when recovery verifies the server write."""
        assert serial == "robot" and map_id == "map1"
        return deepcopy(remote["saved"])

    async def map_action(serial, map_id, action, payload=None):
        """Commit boundaries before simulating a lost reply or rejected promotion."""
        assert serial == "robot" and map_id == "map1"
        if action == "boundaries":
            if remote["failure"] == "boundary_rejected":
                raise MapRequestRejected(status=400)
            remote["saved"] = deepcopy(payload)
            if remote["failure"] == "boundary_response":
                raise CannotConnect("Response lost after committed boundary write")
        elif action == "promote":
            if remote["failure"] == "promote_rejected":
                raise MapRequestRejected(status=400)
            remote["uploaded"] = deepcopy(remote["saved"])
        return {}

    client.async_map_action = AsyncMock(side_effect=map_action)
    client.async_map_boundaries = AsyncMock(side_effect=map_boundaries)
    coordinator = VacuulaCoordinator(hass, entry, client)
    robot = RobotData("robot", "Neato", GEN4, True)
    robot.state = {"details": {"is_docked": True}}
    coordinator.robots[robot.serial] = robot
    return coordinator, remote


async def test_rejected_unchanged_boundary_write_clears_new_guard(
    uncertain_boundary_setup,
):
    """Clear a new guard after a completed rejection and unchanged fresh read."""
    coordinator, remote = uncertain_boundary_setup
    remote["saved"] = deepcopy(BOUNDARIES)
    remote["uploaded"] = deepcopy(BOUNDARIES)
    remote["failure"] = "boundary_rejected"
    replacement = [{**deepcopy(BOUNDARIES[0]), "id": "replacement-no-go"}]
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", replacement)
    assert remote["saved"] == remote["uploaded"] == BOUNDARIES
    assert coordinator.client.async_map_boundaries.await_count == 2
    assert not coordinator.has_pending_map_recovery("robot")


async def test_rejected_unchanged_save_preserves_previous_boundary_uncertainty(
    uncertain_boundary_setup,
):
    """An unchanged rejected save cannot resolve an earlier unapplied boundary set."""
    coordinator, remote = uncertain_boundary_setup
    remote["failure"] = "promote_rejected"
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.has_pending_map_recovery("robot")
    assert remote["saved"] == BOUNDARIES
    assert remote["uploaded"] == []

    remote["failure"] = "boundary_rejected"
    replacement = [{**deepcopy(BOUNDARIES[0]), "id": "replacement-no-go"}]
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", replacement)
    assert remote["saved"] == BOUNDARIES
    assert remote["uploaded"] == []
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("failure", ["boundary_response", "promote_rejected"])
async def test_noop_boundary_save_does_not_create_permanent_recovery(
    uncertain_boundary_setup, failure
):
    """An unchanged save needs no boundary-sync guard when the old map stays active."""
    coordinator, remote = uncertain_boundary_setup
    remote["saved"] = deepcopy(BOUNDARIES)
    remote["uploaded"] = deepcopy(BOUNDARIES)
    remote["failure"] = failure
    if failure == "promote_rejected":
        with pytest.raises(HomeAssistantError):
            await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    else:
        # A no-op save is skipped, so its hypothetical lost response cannot occur.
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert all(
        request.args[2] == "promote"
        for request in coordinator.client.async_map_action.await_args_list
    )
    assert remote["saved"] == remote["uploaded"] == BOUNDARIES
    assert not coordinator.has_pending_map_recovery("robot")
    await coordinator.async_refresh_maps("robot")
    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"]["rank_id"] == "map1"


@pytest.mark.parametrize("failure", ["boundary_response", "promote_rejected"])
async def test_unapplied_boundary_write_blocks_automatic_cleaning(
    uncertain_boundary_setup, failure
):
    """An unchanged promoted flag cannot confirm new no-go regions were uploaded."""
    coordinator, remote = uncertain_boundary_setup
    remote["failure"] = failure
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert remote["saved"] == BOUNDARIES
    assert remote["uploaded"] == []
    assert coordinator.has_pending_map_recovery("robot")

    # Reading the old active flag again must not authorize an unsynchronized map.
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError) as error:
        start_payload(coordinator, "robot")
    assert error.value.translation_key == "map_promotion_required"


@pytest.mark.parametrize("failure", ["boundary_response", "promote_rejected"])
async def test_boundary_sync_uncertainty_survives_reload(
    uncertain_boundary_setup, failure
):
    """Retain the distinction from ordinary promotion recovery across HA reloads."""
    coordinator, remote = uncertain_boundary_setup
    remote["failure"] = failure
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)

    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    replacement.robots = coordinator.robots
    await replacement.async_refresh_maps("robot")
    assert replacement.has_pending_map_recovery("robot")


@pytest.mark.parametrize("failure", ["boundary_response", "promote_rejected"])
async def test_successful_promotion_resolves_boundary_sync_uncertainty(
    uncertain_boundary_setup, failure
):
    """Clear the guard after a successful upload and active-map confirmation."""
    coordinator, remote = uncertain_boundary_setup
    remote["failure"] = failure
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    remote["failure"] = None
    # The mock server restart terminates the lost-response handler before retrying.
    await coordinator.async_map_operation(
        "robot",
        "map1",
        "promote",
        confirm_server_restarted=failure == "boundary_response",
    )
    assert remote["uploaded"] == BOUNDARIES
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("failure", ["boundary_response", "promote_rejected"])
async def test_external_promotion_does_not_unfence_an_unknown_handler(
    uncertain_boundary_setup, failure
):
    """Resolve completed uploads externally but keep unknown handlers fenced."""
    coordinator, remote = uncertain_boundary_setup
    remote["failure"] = failure
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.has_pending_map_recovery("robot")
    previous_writes = coordinator.client.async_map_action.await_count

    # The web app demotes the current map before uploading its saved boundaries.
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    replacement.robots = coordinator.robots
    assert replacement.has_pending_map_recovery("robot")

    # An external upload cannot prove a disconnected older handler has finished.
    remote["uploaded"] = deepcopy(remote["saved"])
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": True}]
    await replacement.async_refresh_maps("robot")
    assert remote["uploaded"] == BOUNDARIES
    assert replacement.has_pending_map_recovery("robot") is (
        failure == "boundary_response"
    )
    assert coordinator.client.async_map_action.await_count == previous_writes


@pytest.mark.parametrize("resolution", ["demote", "map_removed", "robot_removed"])
async def test_unknown_boundary_handler_requires_restart_before_abandonment(
    uncertain_boundary_setup, resolution
):
    """Require restart for demotion and retain unknown work when its map disappears."""
    coordinator, _remote = uncertain_boundary_setup
    with pytest.raises((CannotConnect, HomeAssistantError)):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.has_pending_map_recovery("robot")
    if resolution == "demote":
        await coordinator.async_map_operation(
            "robot", "map1", "demote", confirm_server_restarted=True
        )
    elif resolution == "map_removed":
        coordinator.client.async_maps.return_value = []
        await coordinator.async_refresh_maps("robot")
    else:
        await coordinator._async_snapshot({})
    assert coordinator.has_pending_map_recovery("robot") is (
        resolution == "map_removed"
    )
