"""Verify saved Gen4 boundary changes reach the active robot map safely."""

import asyncio
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula import async_remove_entry
from custom_components.vacuula.client import (
    CannotConnect,
    MapRequestRejected,
)
from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import start_payload

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
    """Build a connected Gen4 robot with mocked map storage and promotion."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_server_boot_id = AsyncMock(return_value=None)
    client.async_maps = AsyncMock(return_value=[{"id": "map1", "promoted": True}])
    client.async_map_boundaries = AsyncMock(return_value=[])
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
        call.async_map_boundaries("robot", "map1"),
        call.async_map_action("robot", "map1", "boundaries", BOUNDARIES),
        call.async_map_action("robot", "map1", "promote", None),
        call.async_maps("robot"),
        # The firmware can disable schedule events when the floorplan changes.
        call.async_command("robot", {"ability": "scheduling.show"}),
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
    """Preserve the save failure and never upload the old boundary set."""
    coordinator = boundary_coordinator
    failure = MapRequestRejected(status=400)
    coordinator.client.async_map_action.side_effect = failure
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value.__cause__ is failure
    assert caught.value.translation_key == "map_promotion_failed"
    assert not coordinator.has_pending_map_recovery("robot")
    coordinator.client.async_map_action.assert_awaited_once_with(
        "robot", "map1", "boundaries", BOUNDARIES
    )


async def test_failed_promotion_cannot_be_reported_as_success(boundary_coordinator):
    """A demoted floorplan after our own failed upload keeps the guard.

    Automatic must not quietly fall back to whole-home cleaning without the
    no-go zones the user just edited.
    """
    coordinator = boundary_coordinator
    failure = MapRequestRejected(status=502)
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
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id)
    assert coordinator.client.async_map_action.await_args_list == [
        call("robot", "map1", "boundaries", BOUNDARIES),
        call("robot", "map1", "promote", None),
    ]


async def test_map_missing_from_robot_list_is_never_written(boundary_coordinator):
    """Reject an absent map using the fresh list before any boundary write."""
    coordinator = boundary_coordinator
    coordinator.robots["robot"].maps = [{"id": "map1", "promoted": True}]
    coordinator.client.async_maps.return_value = [{"id": "other", "promoted": True}]
    with pytest.raises(ServiceValidationError) as error:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert error.value.translation_key == "map_not_in_use"
    coordinator.client.async_map_action.assert_not_awaited()


async def test_map_lookup_failure_prevents_any_write(boundary_coordinator):
    """Propagate a failed map lookup without saving or promoting boundaries."""
    coordinator = boundary_coordinator
    failure = CannotConnect("Map lookup failed")
    coordinator.client.async_maps.side_effect = failure
    with pytest.raises(CannotConnect) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value is failure
    coordinator.client.async_map_action.assert_not_awaited()


@pytest.mark.parametrize(
    "blocked_stage", ["maps", "boundary_read", "boundaries", "promote"]
)
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
        """Record a transaction stage and hold the selected stage for a competitor."""
        operations.append(name)
        if name == blocked_stage:
            entered_stage.set()
            await release_stage.wait()

    async def maps(serial):
        """Simulate the map lookup while recording its transaction position."""
        await stage("maps")
        return [{"id": "map1", "promoted": True}]

    async def map_action(serial, map_id, action, payload=None):
        """Record and optionally block a boundary save or promotion request."""
        await stage(action)
        return {"ok": True}

    async def map_boundaries(serial, map_id):
        """Hold the authoritative boundary snapshot inside the map transaction."""
        await stage("boundary_read")
        return []

    async def command(serial, payload):
        """Record robot-command execution after the map transaction stages."""
        operations.append("command")
        return {"code": 200}

    async def competing_command():
        """Signal the competing read attempt before waiting for the robot lock."""
        command_attempted.set()
        return await coordinator.async_command(
            "robot", {"ability": "state.show"}, refresh=False
        )

    coordinator.client.async_maps.side_effect = maps
    coordinator.client.async_map_boundaries.side_effect = map_boundaries
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
        # The schedule re-read belongs to the transaction; the competitor runs last.
        assert operations == [
            "maps",
            "boundary_read",
            "boundaries",
            "promote",
            "maps",
            "command",
            "command",
        ]
        assert coordinator.client.async_command.await_args_list == [
            call("robot", {"ability": "scheduling.show"}),
            call("robot", {"ability": "state.show"}),
        ]
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
    """Restore the upload guard after reload and clear it on verified promotion."""
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


@pytest.mark.parametrize("readback", [CannotConnect("Read failed"), [{"id": "map1"}]])
async def test_unverified_promotion_keeps_repair_and_guard(
    boundary_coordinator, readback
):
    """Keep the repair when readback cannot confirm a promoted floorplan."""
    coordinator = boundary_coordinator
    coordinator.client.async_maps.side_effect = [
        [{"id": "map1", "promoted": True}],
        readback,
    ]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert caught.value.translation_key == "map_promotion_failed"
    assert coordinator.robots["robot"].maps in ([], [{"id": "map1"}])
    with pytest.raises(ServiceValidationError):
        coordinator.validate_map_recovery("robot")


async def test_failed_promotion_and_failed_readback_clear_stale_active_map(
    boundary_coordinator,
):
    """Discard stale active-map data when upload and confirmation both fail."""
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
    """Persist recovery and discard stale maps when promotion is cancelled."""
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
    """Cache the server's demoted map and block cleaning after upload failure."""
    coordinator = boundary_coordinator
    coordinator.client.async_map_action.side_effect = CannotConnect("Upload timed out")
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_map_operation("robot", "map1", "promote")
    assert caught.value.translation_key == "map_promotion_failed"
    assert coordinator.robots["robot"].maps == [{"id": "map1", "promoted": False}]
    with pytest.raises(ServiceValidationError):
        coordinator.validate_map_recovery("robot")
    assert coordinator.entry.options["pending_map_promotions"] == {"robot": "map1"}
    coordinator.client.async_map_action.assert_awaited_once()


async def test_removing_server_cleans_up_its_persistent_repair(boundary_coordinator):
    """Delete the server's map-promotion repair when its entry is removed."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id)
    await async_remove_entry(coordinator.hass, coordinator.entry)
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize("operation", ["promote", "boundaries"])
async def test_offline_promotion_never_saves_or_creates_recovery(
    boundary_coordinator, operation
):
    """Reject offline uploads before writing boundaries or recording recovery."""
    coordinator = boundary_coordinator
    coordinator.robots["robot"].connected = False
    with pytest.raises(HomeAssistantError) as error:
        if operation == "promote":
            await coordinator.async_map_operation("robot", "map1", "promote")
        else:
            await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert error.value.translation_key == "offline"
    coordinator.client.async_map_action.assert_not_awaited()
    assert not coordinator.has_pending_map_recovery("robot")
    assert "pending_map_promotions" not in coordinator.entry.options
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None


async def test_inactive_boundaries_can_still_be_saved_offline(boundary_coordinator):
    """Save an inactive map offline without promoting it or creating a guard."""
    coordinator = boundary_coordinator
    coordinator.robots["robot"].connected = False
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": False}]
    await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    coordinator.client.async_map_action.assert_awaited_once_with(
        "robot", "map1", "boundaries", BOUNDARIES
    )
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("operation", ["promote", "boundaries"])
async def test_rejected_promotion_distinguishes_unchanged_map_from_saved_boundaries(
    boundary_coordinator, operation
):
    """Preserve recovery for new boundaries despite an unchanged active-map flag."""
    coordinator = boundary_coordinator
    failure = MapRequestRejected(status=400)
    coordinator.client.async_map_action.side_effect = (
        failure if operation == "promote" else [{"saved": True}, failure]
    )
    with pytest.raises(HomeAssistantError) as error:
        if operation == "promote":
            await coordinator.async_map_operation("robot", "map1", "promote")
        else:
            await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert error.value.translation_key == "map_promotion_failed"
    assert coordinator.has_pending_map_recovery("robot") is (operation == "boundaries")
    if operation == "promote":
        coordinator.validate_map_recovery("robot")
    assert coordinator.robots["robot"].maps == [{"id": "map1", "promoted": True}]
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert (
        ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None
    ) is (operation == "promote")


@pytest.mark.parametrize(
    ("maps", "resolved"),
    [
        # Promoted again, here or in the Vacuula app.
        ([{"id": "map1", "promoted": True}], True),
        # Another floorplan is active, so no-go protection exists.
        ([{"id": "map1"}, {"id": "another", "promoted": True}], True),
        # The map is no longer in use.
        ([], True),
        # Demoted exactly as a failed upload leaves it: keep the guard.
        ([{"id": "map1", "promoted": False}], False),
    ],
)
@pytest.mark.parametrize("refresh", ["explicit", "poll"])
async def test_authoritative_maps_resolve_external_recovery_or_demotion(
    boundary_coordinator, maps, resolved, refresh
):
    """Reconcile recovery from fresh map data without issuing map mutations."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    coordinator.client.async_maps.return_value = maps
    coordinator.client.async_runs = AsyncMock(return_value=[])
    if refresh == "explicit":
        await coordinator.async_refresh_maps("robot")
    else:
        await coordinator._async_refresh_resources("robot", full=True)
    assert coordinator.robots["robot"].maps == maps
    assert coordinator.has_pending_map_recovery("robot") is not resolved
    assert bool(coordinator.entry.options["pending_map_promotions"]) is not resolved
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    issue = ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id)
    assert (issue is None) is resolved
    coordinator.client.async_map_action.assert_not_awaited()


async def test_pending_upload_bypasses_recent_resource_cache(boundary_coordinator):
    """Read maps despite a recent poll while promotion recovery is pending."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    coordinator._last_resources["robot"] = monotonic()
    coordinator.client.async_runs = AsyncMock(return_value=[])
    coordinator.client.async_maps.return_value = [{"id": "map1", "promoted": True}]
    await coordinator._async_refresh_resources("robot")
    coordinator.client.async_maps.assert_awaited_once_with("robot")
    assert not coordinator.has_pending_map_recovery("robot")


async def test_poll_cannot_reconcile_previous_map_during_promotion(
    boundary_coordinator,
):
    """Keep polling behind the upload lock until promotion can be confirmed."""
    coordinator = boundary_coordinator
    upload_started = asyncio.Event()
    finish_upload = asyncio.Event()

    async def promote(*args):
        """Hold a successful promotion open until the concurrent poll is queued."""
        upload_started.set()
        await finish_upload.wait()
        return {"promoted": True}

    coordinator.client.async_map_action.side_effect = promote
    coordinator.client.async_runs = AsyncMock(return_value=[])
    upload = asyncio.create_task(
        coordinator.async_map_operation("robot", "map1", "promote")
    )
    poll = None
    try:
        async with asyncio.timeout(5):
            await upload_started.wait()
            poll = asyncio.create_task(
                coordinator._async_refresh_resources("robot", full=True)
            )
            await asyncio.sleep(0)
            # Only the in-use check before the upload has read the maps.
            assert coordinator.client.async_maps.await_count == 1
            assert coordinator.has_pending_map_recovery("robot")
            finish_upload.set()
            await asyncio.gather(upload, poll)
        assert coordinator.client.async_maps.await_count == 3
        assert not coordinator.has_pending_map_recovery("robot")
    finally:
        finish_upload.set()
        tasks = [upload] if poll is None else [upload, poll]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("action", ["demote", "delete"])
async def test_successful_explicit_demotion_clears_uncertain_upload(
    boundary_coordinator, action
):
    """Clear pending recovery after a confirmed demotion or map deletion."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    await coordinator.async_map_operation("robot", "map1", action)
    assert not coordinator.has_pending_map_recovery("robot")


async def test_failed_demotion_keeps_uncertain_upload(boundary_coordinator):
    """Retain the recovery marker when the demotion request fails."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    coordinator.client.async_map_action.side_effect = CannotConnect("Disconnected")
    with pytest.raises(CannotConnect):
        await coordinator.async_map_operation("robot", "map1", "demote")
    assert coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize(
    "map_state", [{"id": "map1"}, {"id": "map1", "promoted": None}]
)
async def test_unreadable_or_unknown_map_status_retains_recovery(
    boundary_coordinator, map_state
):
    """Keep recovery and invalidate cached maps when status is uncertain."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    coordinator.client.async_maps.return_value = [map_state]
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    coordinator.client.async_maps.side_effect = CannotConnect("Read failed")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    assert coordinator.robots["robot"].maps == []
    assert coordinator.robots["robot"].capabilities["maps_available"] is False


async def test_removed_robot_cleans_only_its_persistent_repair(boundary_coordinator):
    """Remove only the missing robot's recovery marker and repair notice."""
    coordinator = boundary_coordinator
    coordinator.robots["other"] = RobotData("other", "Upstairs", GEN4, True)
    coordinator._set_pending_promotion("robot", "map1")
    coordinator._set_pending_promotion("other", "map2")
    await coordinator._async_snapshot({"other": {"type": GEN4, "connected": True}})
    assert coordinator.entry.options["pending_map_promotions"] == {"other": "map2"}
    registry = ir.async_get(coordinator.hass)
    assert (
        registry.async_get_issue(
            DOMAIN, f"{coordinator.entry.entry_id}_robot_map_promotion"
        )
        is None
    )
    assert registry.async_get_issue(
        DOMAIN, f"{coordinator.entry.entry_id}_other_map_promotion"
    )


async def test_removed_robot_loaded_from_options_also_cleans_repair(
    boundary_coordinator,
):
    """Clear a persisted repair for a robot absent from the first snapshot."""
    coordinator = boundary_coordinator
    coordinator._set_pending_promotion("robot", "map1")
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    assert not replacement.robots
    await replacement._async_snapshot({})
    assert not coordinator.entry.options["pending_map_promotions"]
    issue_id = f"{coordinator.entry.entry_id}_robot_map_promotion"
    assert ir.async_get(coordinator.hass).async_get_issue(DOMAIN, issue_id) is None


async def test_repair_identifies_each_robot_and_floorplan_after_reload(
    boundary_coordinator,
):
    """Rebuild each repair with the current robot name and stored map ID."""
    coordinator = boundary_coordinator
    coordinator.robots["other"] = RobotData("other", "Upstairs", GEN4, True)
    coordinator._set_pending_promotion("robot", "map1")
    coordinator._set_pending_promotion("other", "map2")
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    await replacement._async_snapshot(
        {
            "robot": {"name": "Downstairs", "type": GEN4, "connected": False},
            "other": {"name": "Upstairs", "type": GEN4, "connected": False},
        }
    )
    registry = ir.async_get(coordinator.hass)
    for serial, name, map_id in (
        ("robot", "Downstairs", "map1"),
        ("other", "Upstairs", "map2"),
    ):
        issue = registry.async_get_issue(
            DOMAIN, f"{coordinator.entry.entry_id}_{serial}_map_promotion"
        )
        assert issue.translation_placeholders == {"robot": name, "map_id": map_id}
