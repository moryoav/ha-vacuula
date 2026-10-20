"""Upgrade old house defaults without silently opting out of no-go lines."""

from copy import deepcopy

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula import async_migrate_entry
from custom_components.vacuula.const import DOMAIN


async def test_old_house_default_becomes_automatic_and_keeps_other_preferences(hass):
    """Migrate legacy whole-home defaults without mutating unrelated preferences."""
    old = {
        "other_option": True,
        "cleaning_options": {
            "first": {
                "category": "house",
                "mode": "turbo",
                "navigation_mode": "deep",
                "spot_width": 300,
                "spot_height": 250,
            },
            "second": {"category": "spot", "mode": "eco"},
        },
    }
    original = deepcopy(old)
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, options=old)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 1
    assert entry.minor_version == 3
    expected = deepcopy(original)
    expected["cleaning_options"]["first"]["category"] = "auto"
    assert dict(entry.options) == expected
    assert old == original


@pytest.mark.parametrize(
    "options", [{}, {"cleaning_options": []}, {"cleaning_options": {"bad": None}}]
)
async def test_empty_or_invalid_old_defaults_do_not_break_upgrade(hass, options):
    """Advance the entry version while preserving absent or malformed defaults."""
    entry = MockConfigEntry(domain=DOMAIN, options=options, minor_version=1)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 3
    assert dict(entry.options) == options


async def test_new_explicit_mapless_choice_is_preserved(hass):
    """Keep an explicit whole-home choice made after the safety migration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        options={"cleaning_options": {"robot": {"category": "house"}}},
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.options["cleaning_options"]["robot"]["category"] == "house"


async def test_future_major_version_is_not_downgraded(hass):
    """Refuse migration of an unsupported newer major entry version."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, minor_version=1)
    entry.add_to_hass(hass)
    assert not await async_migrate_entry(hass, entry)
    assert entry.version == 2


@pytest.mark.parametrize("minor_version", [1, 2])
async def test_legacy_pending_uploads_gain_unknown_operation_fence(hass, minor_version):
    """Do not assume an older persisted upload guard describes completed HTTP work."""
    old = {
        "pending_map_promotions": {"robot": "map1"},
        "pending_boundary_sync": {"robot": False},
        "cleaning_options": {"robot": {"category": "spot", "mode": "turbo"}},
        "untouched": {"value": 1},
    }
    original = deepcopy(old)
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, minor_version=minor_version, options=old
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 3
    assert entry.options["pending_map_operations"] == {
        "robot": {"action": "unknown", "map_id": "map1"}
    }
    for key, value in original.items():
        assert entry.options[key] == value
    assert old == original


async def test_migration_keeps_existing_operation_evidence(hass):
    """Keep existing operation evidence instead of replacing it with a legacy fence."""
    operation = {
        "action": "promote",
        "map_id": "map2",
        "boot_id": '"111111111111"',
        "needs_upload": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        options={
            "pending_map_promotions": {"robot": "map1"},
            "pending_map_operations": {"robot": deepcopy(operation)},
        },
    )
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.options["pending_map_operations"]["robot"] == operation
