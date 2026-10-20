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
    assert entry.minor_version == 2
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
    assert entry.minor_version == 2
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
