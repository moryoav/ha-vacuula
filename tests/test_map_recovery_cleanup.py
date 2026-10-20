"""Remove persistent recovery notices even after an interrupted options update."""

from unittest.mock import MagicMock

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula import async_remove_entry
from custom_components.vacuula.const import DOMAIN
from custom_components.vacuula.coordinator import VacuulaCoordinator


async def test_entry_removal_cleans_reconstructed_inflight_issue(hass):
    """Do not orphan Repairs when only the pre-dispatch fence survived a crash."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "pending_map_operations": {
                "robot": {"action": "promote", "map_id": "map1", "boot_id": None}
            },
        },
    )
    entry.add_to_hass(hass)
    VacuulaCoordinator(hass, entry, MagicMock())
    issue_id = f"{entry.entry_id}_robot_map_promotion"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    assert not entry.options.get("pending_map_promotions")
    await async_remove_entry(hass, entry)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_reloaded_notice_names_the_original_guarded_map(hass):
    """Keep recovery instructions tied to dirty zones during an alternate request."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options={
            "pending_map_promotions": {"robot": "map1"},
            "pending_boundary_sync": {"robot": False},
            "pending_map_operations": {
                "robot": {"action": "promote", "map_id": "map2", "boot_id": None}
            },
        },
    )
    entry.add_to_hass(hass)
    VacuulaCoordinator(hass, entry, MagicMock())
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_robot_map_promotion"
    )
    assert issue is not None
    assert issue.translation_placeholders["map_id"] == "map1"
