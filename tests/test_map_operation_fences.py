"""Keep interrupted server map requests fenced until completion is established."""

import asyncio
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

BOOT_ONE = '"111111111111"'
BOOT_TWO = '"222222222222"'

BOUNDARIES = [
    {
        "id": "protected",
        "name": "Keep clear",
        "type": "polygon",
        "color": "#ff0000",
        "function": "nogo",
        "enabled": True,
        "vertices": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]],
    }
]


@pytest.fixture
def map_fence(hass):
    """Expose independent server map state, boundaries, boot identity and requests."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local"})
    entry.add_to_hass(hass)
    client = MagicMock()
    remote = {
        "maps": [{"id": "map1", "promoted": True}],
        "boundaries": [],
        "boot": BOOT_ONE,
    }
    client.async_maps = AsyncMock(side_effect=lambda serial: deepcopy(remote["maps"]))
    client.async_map_boundaries = AsyncMock(
        side_effect=lambda serial, map_id: deepcopy(remote["boundaries"])
    )
    client.async_server_boot_id = AsyncMock(side_effect=lambda: remote["boot"])
    client.async_map_action = AsyncMock(side_effect=CannotConnect("Reply lost"))
    client.async_command = AsyncMock(return_value={"code": 200, "body": {}})
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots["robot"] = RobotData(
        "robot",
        "Neato",
        GEN4,
        True,
        state={"details": {"is_docked": True}},
    )
    return coordinator, remote


async def interrupted_request(coordinator, remote, action, map_id="map1"):
    """Interrupt a request after dispatch with its pre-request metadata captured."""
    if action == "boundaries":
        coordinator._set_pending_promotion("robot", map_id, boundary_change=True)
    async with coordinator._lock("robot"):
        with pytest.raises(CannotConnect):
            await coordinator._async_map_request_locked(
                "robot",
                map_id,
                action,
                BOUNDARIES if action == "boundaries" else None,
                require_connection=action == "boundaries",
            )


def reload_coordinator(coordinator):
    """Rebuild recovery solely from persisted entry options and cached robot data."""
    replacement = VacuulaCoordinator(
        coordinator.hass, coordinator.entry, coordinator.client
    )
    replacement.robots = deepcopy(coordinator.robots)
    return replacement


async def assert_fenced(coordinator):
    """Refuse further map writes and mapped starts while the old handler may run."""
    with pytest.raises(ServiceValidationError) as error:
        async with coordinator._lock("robot"):
            await coordinator._async_require_map_operation_settled("robot")
    assert error.value.translation_key == "map_operation_pending"
    with pytest.raises(ServiceValidationError) as error:
        start_payload(coordinator, "robot")
    assert error.value.translation_key == "map_promotion_required"


@pytest.mark.parametrize("reload_entry", [False, True])
async def test_unknown_save_with_unchanged_data_keeps_its_fence(
    map_fence, reload_entry
):
    """An unchanged boundary read cannot establish that a delayed save has finished."""
    coordinator, remote = map_fence
    await interrupted_request(coordinator, remote, "boundaries")
    if reload_entry:
        coordinator = reload_coordinator(coordinator)
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)
    assert remote["boundaries"] == []


async def test_matching_boundaries_do_not_prove_our_delayed_handler_finished(map_fence):
    """Another app can store identical boundaries before our delayed save runs."""
    coordinator, remote = map_fence
    await interrupted_request(coordinator, remote, "boundaries")
    remote["boundaries"] = deepcopy(BOUNDARIES)
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)
    serialized = repr(dict(coordinator.entry.options))
    assert "Keep clear" not in serialized
    assert "vertices" not in serialized


@pytest.mark.parametrize("target", ["map1", "map2"])
async def test_unknown_promotion_cannot_use_unchanged_active_status(map_fence, target):
    """Neither the same old active map nor another old active map proves completion."""
    coordinator, remote = map_fence
    remote["maps"].append({"id": "map2", "promoted": False})
    await interrupted_request(coordinator, remote, "promote", target)
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)


async def test_external_promotion_transition_cannot_unfence_our_pending_request(
    map_fence,
):
    """An app's demotion and promotion cannot prove our other handler has finished."""
    coordinator, remote = map_fence
    await interrupted_request(coordinator, remote, "promote")
    remote["maps"] = [{"id": "map1", "promoted": False}]
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)
    coordinator = reload_coordinator(coordinator)
    remote["maps"] = [{"id": "map1", "promoted": True}]
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)


async def test_server_restart_unfences_but_still_requires_map_confirmation(map_fence):
    """A new server process permits recovery writes without authorizing cleaning."""
    coordinator, remote = map_fence
    await interrupted_request(coordinator, remote, "promote")
    remote["boot"] = BOOT_TWO
    async with coordinator._lock("robot"):
        await coordinator._async_require_map_operation_settled("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    coordinator.client.async_map_action.side_effect = None
    coordinator.client.async_map_action.return_value = {}
    await coordinator.async_map_operation("robot", "map1", "promote")
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize(
    ("baseline", "later"),
    [(BOOT_ONE, BOOT_ONE), (BOOT_ONE, None), (None, None), (None, BOOT_TWO)],
)
async def test_unchanged_or_missing_boot_evidence_keeps_fence(
    map_fence, baseline, later
):
    """Require a changed known baseline, rather than merely a readable boot token."""
    coordinator, remote = map_fence
    remote["boot"] = baseline
    await interrupted_request(coordinator, remote, "promote")
    remote["boot"] = later
    await assert_fenced(coordinator)
    await assert_fenced(reload_coordinator(coordinator))


async def test_restarted_before_dispatch_is_the_unknown_request_baseline(map_fence):
    """A restart before POST cannot prove that the POST's new handler has stopped."""
    coordinator, remote = map_fence
    # Any token observed earlier belongs to a process that did not receive POST.
    assert await coordinator.client.async_server_boot_id() == BOOT_ONE

    async def interrupted(serial, map_id, action, payload=None):
        """Accept POST on a restarted server and lose its still-pending reply."""
        remote["boot"] = BOOT_TWO
        raise CannotConnect("New server still handling the request")

    coordinator.client.async_map_action.side_effect = interrupted
    await interrupted_request(coordinator, remote, "promote")
    await assert_fenced(coordinator)
    await assert_fenced(reload_coordinator(coordinator))


async def test_admin_restart_confirmation_permits_only_recovery_writes(map_fence):
    """An explicit restart assertion ends overlap uncertainty, not map uncertainty."""
    coordinator, remote = map_fence
    remote["boot"] = None
    await interrupted_request(coordinator, remote, "promote")
    async with coordinator._lock("robot"):
        await coordinator._async_require_map_operation_settled(
            "robot", confirm_server_restarted=True
        )
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    coordinator.client.async_map_action.side_effect = None
    coordinator.client.async_map_action.return_value = {}
    await coordinator.async_map_operation("robot", "map1", "promote")
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("action", ["use", "unuse"])
async def test_external_visibility_transition_cannot_unfence_delayed_metadata(
    map_fence, action
):
    """Visibility can change through the app while our request is still pending."""
    coordinator, remote = map_fence
    if action == "use":
        remote["maps"] = []
    elif action == "unuse":
        remote["maps"] = [{"id": "map1", "promoted": False}]
    await interrupted_request(coordinator, remote, action)
    await assert_fenced(coordinator)
    remote["maps"] = [] if action == "unuse" else [{"id": "map1", "promoted": False}]
    await coordinator.async_refresh_maps("robot")
    await assert_fenced(coordinator)
    remote["boot"] = BOOT_TWO
    async with coordinator._lock("robot"):
        await coordinator._async_require_map_operation_settled("robot")
    if action == "unuse":
        assert coordinator.has_pending_map_recovery("robot")
        with pytest.raises(ServiceValidationError):
            start_payload(coordinator, "robot")
        coordinator.client.async_map_action.side_effect = None
        coordinator.client.async_map_action.return_value = {}
        await coordinator.async_map_operation("robot", "map1", "demote")
    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"] is None


async def test_restart_during_unknown_demotion_still_requires_explicit_recovery(
    map_fence,
):
    """A restarted handler cannot establish whether its earlier robot command ran."""
    coordinator, remote = map_fence
    await interrupted_request(coordinator, remote, "demote")
    remote["maps"] = [{"id": "map1", "promoted": False}]
    await assert_fenced(coordinator)
    remote["boot"] = BOOT_TWO
    async with coordinator._lock("robot"):
        await coordinator._async_require_map_operation_settled("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    coordinator.client.async_map_action.side_effect = None
    coordinator.client.async_map_action.return_value = {}
    await coordinator.async_map_operation("robot", "map1", "demote")
    assert not coordinator.has_pending_map_recovery("robot")


async def test_inactive_boundary_save_needs_no_upload_after_server_restart(map_fence):
    """A stopped server ends a metadata-only save's newly introduced uncertainty."""
    coordinator, remote = map_fence
    remote["maps"] = [{"id": "map1", "promoted": False}]
    with pytest.raises(CannotConnect):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    await assert_fenced(coordinator)
    remote["boot"] = BOOT_TWO
    await coordinator.async_refresh_maps("robot")
    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"] is None


@pytest.mark.parametrize("admin_confirmation", [False, True])
async def test_external_promotion_before_late_boundary_commit_needs_reupload(
    map_fence, admin_confirmation
):
    """Keep new zones guarded if an inactive map was uploaded before its late save."""
    coordinator, remote = map_fence
    remote["maps"] = [{"id": "map1", "promoted": False}]
    robot_boundaries = []

    async def late_save(serial, map_id, action, payload=None):
        """Model the app taking old zones before the interrupted save commits."""
        assert action == "boundaries"
        robot_boundaries[:] = deepcopy(remote["boundaries"])
        remote["maps"][0]["promoted"] = True
        remote["boundaries"] = deepcopy(payload)
        raise CannotConnect("Late save committed after external promotion")

    coordinator.client.async_map_action.side_effect = late_save
    with pytest.raises(CannotConnect):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    assert robot_boundaries == []
    assert remote["boundaries"] == BOUNDARIES
    if admin_confirmation:
        remote["boot"] = None
        async with coordinator._lock("robot"):
            await coordinator._async_require_map_operation_settled(
                "robot", confirm_server_restarted=True
            )
    else:
        remote["boot"] = BOOT_TWO
        await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    assert coordinator._pending_boundary_sync["robot"] is False
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")

    async def upload(serial, map_id, action, payload=None):
        """Confirm the robot receives the currently stored zones on a new upload."""
        assert action == "promote"
        robot_boundaries[:] = deepcopy(remote["boundaries"])
        return {}

    coordinator.client.async_map_action.side_effect = upload
    await coordinator.async_map_operation("robot", "map1", "promote")
    assert robot_boundaries == BOUNDARIES
    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"]["nogo_enabled"]


@pytest.mark.parametrize("refresh_fails", [False, True])
async def test_restart_rechecks_boundary_target_after_a_stale_preboot_map_read(
    map_fence, refresh_fails
):
    """Do not resolve a save using inactive metadata fetched before the new boot."""
    coordinator, remote = map_fence
    inactive = [{"id": "map1", "promoted": False}]
    remote["maps"] = deepcopy(inactive)
    with pytest.raises(CannotConnect):
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    remote["boot"] = BOOT_TWO
    remote["maps"] = [{"id": "map1", "promoted": True}]
    coordinator.client.async_maps.reset_mock()
    coordinator.client.async_maps.side_effect = [
        deepcopy(inactive),
        CannotConnect("Post-restart read failed")
        if refresh_fails
        else deepcopy(remote["maps"]),
    ]
    if refresh_fails:
        with pytest.raises(HomeAssistantError):
            await coordinator.async_refresh_maps("robot")
        assert coordinator._map_operations["robot"]["action"] == "boundaries"
    else:
        await coordinator.async_refresh_maps("robot")
        assert coordinator._map_operations["robot"]["action"] == "recovery"
    assert coordinator.client.async_maps.await_count == 2
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")


@pytest.mark.parametrize("older_marker", [False, True])
@pytest.mark.parametrize("recovery", ["demote", "promote"])
async def test_late_unuse_cannot_hide_a_newly_promoted_map_and_enable_fallback(
    map_fence, older_marker, recovery
):
    """Keep protection when a late metadata write may hide active no-go zones."""
    coordinator, remote = map_fence
    remote["maps"] = [{"id": "map1", "promoted": False}]
    await interrupted_request(coordinator, remote, "unuse")
    if older_marker:
        coordinator._map_operations["robot"]["needs_upload"] = False
        coordinator._persist_map_recovery()
        coordinator = reload_coordinator(coordinator)
    # The app promoted map1, then the old unuse handler hid it. The robot still
    # has that active floorplan, but GET/maps no longer exposes its promotion.
    hidden_map = {"id": "map1", "promoted": True}
    remote["maps"] = []
    remote["boot"] = BOOT_TWO
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")

    async def recover(serial, map_id, action, payload=None):
        """Demote the hidden floorplan or expose and reupload it deliberately."""
        if action == "use":
            remote["maps"] = [deepcopy(hidden_map)]
        elif action == "demote":
            hidden_map["promoted"] = False
        elif action == "promote":
            assert remote["maps"]
            remote["maps"][0]["promoted"] = True
        return {}

    coordinator.client.async_map_action.side_effect = recover
    if recovery == "promote":
        await coordinator.async_map_operation("robot", "map1", "use")
        assert coordinator.has_pending_map_recovery("robot")
    await coordinator.async_map_operation("robot", "map1", recovery)
    assert not coordinator.has_pending_map_recovery("robot")


async def test_acknowledged_inactive_unuse_does_not_create_an_upload_guard(map_fence):
    """Keep the normal confirmed inactive-map visibility action unchanged."""
    coordinator, remote = map_fence
    remote["maps"] = [{"id": "map1", "promoted": False}]

    async def unuse(serial, map_id, action, payload=None):
        """Confirm the hidden inactive map without affecting any robot floorplan."""
        assert action == "unuse"
        remote["maps"] = []
        return {}

    coordinator.client.async_map_action.side_effect = unuse
    await coordinator.async_map_operation("robot", "map1", "unuse")
    await coordinator.async_refresh_maps("robot")
    assert not coordinator.has_pending_map_recovery("robot")
    assert start_payload(coordinator, "robot")["runs"][0]["map"] is None


async def test_newly_active_late_save_retargets_an_older_inactive_guard(map_fence):
    """Recover an older request without letting old-map demotion clear new zones."""
    coordinator, remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator._persist_map_recovery()
    remote["maps"] = [
        {"id": "map1", "promoted": False},
        {"id": "map2", "promoted": False},
    ]
    async with coordinator._lock("robot"):
        with pytest.raises(CannotConnect):
            await coordinator._async_map_request_locked(
                "robot", "map2", "boundaries", BOUNDARIES
            )
    assert coordinator._pending_promotions["robot"] == "map1"
    remote["maps"][1]["promoted"] = True
    remote["boundaries"] = deepcopy(BOUNDARIES)
    remote["boot"] = BOOT_TWO
    await coordinator.async_refresh_maps("robot")
    assert coordinator._pending_promotions["robot"] == "map2"
    assert coordinator.entry.options["pending_map_promotions"] == {"robot": "map2"}
    coordinator.client.async_map_action.side_effect = None
    coordinator.client.async_map_action.return_value = {}
    await coordinator.async_map_operation("robot", "map1", "demote")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    await coordinator.async_map_operation("robot", "map2", "promote")
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("action", ["unuse", "boundaries"])
async def test_recovery_refuses_another_maps_zone_or_visibility_write(
    map_fence, action
):
    """Keep one unresolved floorplan from gaining a second conflicting uncertainty."""
    coordinator, remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator._persist_map_recovery()
    remote["maps"] = [
        {"id": "map1", "promoted": True},
        {"id": "map2", "promoted": False},
    ]
    coordinator.client.async_map_action.reset_mock()
    with pytest.raises(ServiceValidationError) as error:
        if action == "boundaries":
            await coordinator.async_set_map_boundaries("robot", "map2", BOUNDARIES)
        else:
            await coordinator.async_map_operation("robot", "map2", action)
    assert error.value.translation_key == "map_promotion_required"
    coordinator.client.async_map_action.assert_not_awaited()
    assert coordinator._pending_promotions == {"robot": "map1"}
    assert coordinator._map_operations["robot"]["action"] == "recovery"


async def test_cross_map_recovery_does_not_retarget_from_stale_preboot_metadata(
    map_fence,
):
    """Keep the current guard despite an older read naming another active map."""
    coordinator, remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator._persist_map_recovery()
    async with coordinator._lock("robot"):
        with pytest.raises(CannotConnect):
            await coordinator._async_map_request_locked(
                "robot", "map2", "boundaries", BOUNDARIES
            )
    remote["boot"] = BOOT_TWO
    stale_maps = [
        {"id": "map1", "promoted": False},
        {"id": "map2", "promoted": True},
    ]
    current_maps = [
        {"id": "map1", "promoted": True},
        {"id": "map2", "promoted": False},
    ]
    coordinator.client.async_maps.side_effect = [stale_maps, current_maps]
    await coordinator.async_refresh_maps("robot")
    assert coordinator._pending_promotions == {"robot": "map1"}
    assert coordinator.robots["robot"].maps == current_maps
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")


async def test_metadata_restart_preserves_older_boundary_upload_uncertainty(map_fence):
    """Resolving a later metadata request cannot clear an earlier strict guard."""
    coordinator, remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator._persist_map_recovery()
    await interrupted_request(coordinator, remote, "use", "map2")
    remote["maps"].append({"id": "map2", "promoted": False})
    await assert_fenced(coordinator)
    remote["boot"] = BOOT_TWO
    async with coordinator._lock("robot"):
        await coordinator._async_require_map_operation_settled("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
    assert coordinator.entry.options["pending_map_promotions"] == {"robot": "map1"}


@pytest.mark.parametrize("action", ["use", "unuse", "boundaries"])
async def test_noop_metadata_does_not_dispatch_or_create_uncertainty(map_fence, action):
    """Skip metadata writes whose desired state is already established."""
    coordinator, remote = map_fence
    remote["maps"] = [] if action == "unuse" else [{"id": "map1", "promoted": False}]
    if action == "boundaries":
        remote["boundaries"] = deepcopy(BOUNDARIES)
        await coordinator.async_set_map_boundaries("robot", "map1", BOUNDARIES)
    else:
        await coordinator.async_map_operation("robot", "map1", action)
    coordinator.client.async_map_action.assert_not_awaited()
    assert not coordinator.has_pending_map_recovery("robot")


@pytest.mark.parametrize("cancelled", [False, True])
async def test_completed_or_cancelled_recovery_request_keeps_older_strict_guard(
    map_fence, cancelled
):
    """A rejected or cancelled later request cannot discard earlier boundary doubt."""
    coordinator, remote = map_fence
    coordinator._set_pending_promotion("robot", "map1", boundary_change=True)
    coordinator._map_operations["robot"] = {"action": "recovery", "map_id": "map1"}
    coordinator._persist_map_recovery()
    failure = asyncio.CancelledError() if cancelled else MapRequestRejected(status=400)
    coordinator.client.async_map_action.side_effect = failure
    async with coordinator._lock("robot"):
        with pytest.raises(asyncio.CancelledError if cancelled else MapRequestRejected):
            await coordinator._async_map_request_locked(
                "robot",
                "map1",
                "promote",
            )
    coordinator = reload_coordinator(coordinator)
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
