"""Verify saved Gen4 boundary changes reach the active robot map safely."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula import async_remove_entry
from custom_components.vacuula.client import CannotConnect, VacuulaError
from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData

BOUNDARIES = [
    {
        "id": "zone1",
        "name": "No-go area",
        "color": "#ffffff",
        "type": "polygon",
        "vertices": [[0, 0], [1, 0], [1, 1]],
        "enabled": True,
        "function": "nogo",
    }
]


@pytest.fixture
def boundary_coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_maps = AsyncMock(return_value=[{"id": "map1", "promoted": True}])
    client.async_map_action = AsyncMock(return_value={"saved": True})
    client.async_command = AsyncMock(return_value={"code": 200})
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots["robot"] = RobotData("robot", "Neato", GEN4, True)
    return coordinator


async def test_fresh_promoted_map_is_saved_before_upload(boundary_coordinator):
    """A stale inactive cache cannot suppress the required robot promotion."""
    coordinator = boundary_coordinator
    coordinator.robots["robot"].maps = [{"id": "map1", "promoted": False}]
    coordinator.client.async_map_action.side_effect = [
        {"saved": True},
        {"promoted": True},
    ]
    result = await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert result == {"saved": True}
    assert coordinator.client.mock_calls == [
        call.async_maps("robot"),
        call.async_map_action("robot", "map1", "boundaries", BOUNDARIES),
        call.async_map_action("robot", "map1", "promote"),
        call.async_maps("robot"),
    ]
    assert not coordinator.entry.options["pending_map_promotions"]


async def test_inactive_map_is_saved_without_becoming_active(boundary_coordinator):
    """A stale promoted cache cannot unexpectedly activate another map."""
    coordinator = boundary_coordinator
    coordinator.robots["robot"].maps = [{"id": "map1", "promoted": True}]
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    coordinator.client.async_map_action.assert_awaited_once_with(
        "robot", "map1", "boundaries", BOUNDARIES
    )


async def test_failed_save_does_not_promote_previous_boundaries(boundary_coordinator):
    coordinator = boundary_coordinator
    failure = CannotConnect("Boundary save failed")
    coordinator.client.async_map_action.side_effect = failure
    with pytest.raises(CannotConnect) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value is failure
    coordinator.client.async_map_action.assert_awaited_once_with(
        "robot", "map1", "boundaries", BOUNDARIES
    )


async def test_failed_promotion_cannot_be_reported_as_success(boundary_coordinator):
    coordinator = boundary_coordinator
    failure = VacuulaError("Robot rejected map promotion")
    coordinator.client.async_map_action.side_effect = [{"saved": True}, failure]
    coordinator.client.async_maps.side_effect = [
        [{"id": "map1", "promoted": True}],
        [{"id": "map1", "promoted": False}],
    ]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value.translation_key == "map_promotion_failed"
    assert caught.value.__cause__ is failure
    assert coordinator.robots["robot"].maps == [{"id": "map1", "promoted": False}]
    assert coordinator.entry.options["pending_map_promotions"] == {"robot": "map1"}
    with pytest.raises(ServiceValidationError) as blocked:
        coordinator.validate_map_recovery("robot")
    assert blocked.value.translation_key == "map_promotion_required"
    assert coordinator.client.async_map_action.await_args_list == [
        call("robot", "map1", "boundaries", BOUNDARIES),
        call("robot", "map1", "promote"),
    ]


async def test_map_missing_from_robot_list_is_never_written(boundary_coordinator):
    coordinator = boundary_coordinator
    coordinator.robots["robot"].maps = [{"id": "map1", "promoted": True}]
    coordinator.client.async_maps.return_value = [{"id": "other", "promoted": True}]
    with pytest.raises(VacuulaError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    coordinator.client.async_map_action.assert_not_awaited()


async def test_map_lookup_failure_prevents_any_write(boundary_coordinator):
    coordinator = boundary_coordinator
    failure = CannotConnect("Map lookup failed")
    coordinator.client.async_maps.side_effect = failure
    with pytest.raises(CannotConnect) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value is failure
    coordinator.client.async_map_action.assert_not_awaited()


@pytest.mark.parametrize("blocked_stage", ["maps", "boundaries", "promote"])
async def test_other_commands_cannot_interleave_with_map_transaction(
    boundary_coordinator, blocked_stage
):
    """Lookup, save and promotion all share the robot command lock."""
    coordinator = boundary_coordinator
    entered_stage = asyncio.Event()
    release_stage = asyncio.Event()
    command_attempted = asyncio.Event()
    operations = []

    async def stage(name):
        operations.append(name)
        if name == blocked_stage:
            entered_stage.set()
            await release_stage.wait()

    async def maps(serial):
        await stage("maps")
        return [{"id": "map1", "promoted": True}]

    async def map_action(serial, map_id, action, payload=None):
        await stage(action)
        return {"ok": True}

    async def command(serial, payload):
        operations.append("command")
        return {"code": 200}

    async def competing_command():
        command_attempted.set()
        return await coordinator.async_command(
            "robot", {"ability": "state.show"}, refresh=False
        )

    coordinator.client.async_maps.side_effect = maps
    coordinator.client.async_map_action.side_effect = map_action
    coordinator.client.async_command.side_effect = command
    transaction = asyncio.create_task(
        coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    )
    competitor = None
    try:
        async with asyncio.timeout(5):
            await entered_stage.wait()
            competitor = asyncio.create_task(competing_command())
            await command_attempted.wait()
            coordinator.client.async_command.assert_not_awaited()
            assert not competitor.done()
            release_stage.set()
            await asyncio.gather(transaction, competitor)
        assert operations == ["maps", "boundaries", "promote", "maps", "command"]
        coordinator.client.async_command.assert_awaited_once_with(
            "robot", {"ability": "state.show"}
        )
    finally:
        release_stage.set()
        pending = [transaction] if competitor is None else [transaction, competitor]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_pending_upload_survives_reload_and_clears_after_confirmed_recovery(
    boundary_coordinator,
):
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    replacement.robots = coordinator.robots
    with pytest.raises(ServiceValidationError):
        replacement.validate_map_recovery("robot")
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id)
    await replacement.async_map_operation("robot", "map1", "promote")
    replacement.validate_map_recovery("robot")
    assert not coordinator.entry.options["pending_map_promotions"]
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize("readback", [CannotConnect("Read failed"), []])
async def test_unverified_promotion_keeps_repair_and_guard(
    boundary_coordinator, readback
):
    coordinator = boundary_coordinator
    coordinator.client.async_maps.side_effect = [
        [{"id": "map1", "promoted": True}],
        readback,
    ]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value.translation_key == "map_promotion_failed"
    assert coordinator.robots["robot"].maps == []
    with pytest.raises(ServiceValidationError):
        coordinator.validate_map_recovery("robot")


async def test_failed_promotion_and_failed_readback_clear_stale_active_map(
    boundary_coordinator,
):
    coordinator = boundary_coordinator
    coordinator.client.async_maps.side_effect = [
        [{"id": "map1", "promoted": True}],
        CannotConnect("Read failed"),
    ]
    coordinator.client.async_map_action.side_effect = [{}, CannotConnect("Timed out")]
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.robots["robot"].maps == []
    assert coordinator.robots["robot"].capabilities["maps_available"] is False
    assert coordinator.client.async_map_action.await_count == 2


async def test_cancelled_promotion_leaves_persistent_recovery_guard(
    boundary_coordinator,
):
    coordinator = boundary_coordinator
    coordinator.client.async_map_action.side_effect = [{}, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert coordinator.entry.options["pending_map_promotions"] == {"robot": "map1"}
    assert coordinator.robots["robot"].maps == []
    assert coordinator.robots["robot"].capabilities["maps_available"] is False
    with pytest.raises(ServiceValidationError):
        coordinator.validate_map_recovery("robot")


async def test_failed_standalone_promotion_exposes_demoted_state(boundary_coordinator):
    coordinator = boundary_coordinator
    coordinator.client.async_map_action.side_effect = CannotConnect("Upload timed out")
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_map_operation("robot", "map1", "promote")
    assert caught.value.translation_key == "map_promotion_failed"
    assert coordinator.robots["robot"].maps == [{"id": "map1", "promoted": False}]
    with pytest.raises(ServiceValidationError):
        coordinator.validate_map_recovery("robot")
    coordinator.client.async_map_action.assert_awaited_once()


async def test_removing_server_cleans_up_its_persistent_repair(boundary_coordinator):
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id)
    await async_remove_entry(coordinator.hass, coordinator.entry)
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None
