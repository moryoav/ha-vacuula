"""Verify setup and unload own every background subscription."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula import (
    async_remove_config_entry_device,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN


async def test_setup(hass):
    """Register the integration's services during domain setup."""
    assert await async_setup(hass, {})
    assert hass.services.has_service(DOMAIN, "clean")


async def test_entry_lifecycle(hass):
    """Own coordinator startup and shutdown and guard removal of listed robots."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_start = AsyncMock()
    coordinator.async_stop = AsyncMock()
    with (
        patch("custom_components.vacuula.VacuulaCoordinator", return_value=coordinator),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
        ),
    ):
        assert await async_setup_entry(hass, entry)
        assert entry.runtime_data is coordinator
        coordinator.async_start.assert_awaited_once()
    for result in (False, True):
        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=result),
        ):
            assert await async_unload_entry(hass, entry) is result
    coordinator.async_stop.assert_awaited_once()
    coordinator.client.async_robots = AsyncMock(return_value={"robot": {}})
    device = MagicMock(identifiers={(DOMAIN, "robot")})
    assert not await async_remove_config_entry_device(hass, entry, device)
    device.identifiers = {(DOMAIN, "removed")}
    assert await async_remove_config_entry_device(hass, entry, device)


async def test_first_refresh_error(hass):
    """Leave an unreachable entry retryable when its initial refresh fails."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    with patch("custom_components.vacuula.VacuulaCoordinator") as cls:
        cls.return_value.async_config_entry_first_refresh = AsyncMock(
            side_effect=ConfigEntryNotReady("offline")
        )
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)


async def test_remove_device_when_runtime_not_loaded(hass):
    """Query the server before allowing device removal from an unloaded entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    device = MagicMock(identifiers={(DOMAIN, "old_robot")})
    with patch("custom_components.vacuula.VacuulaClient") as cls:
        cls.return_value.async_robots = AsyncMock(return_value={})
        assert await async_remove_config_entry_device(hass, entry, device)
        cls.return_value.async_robots.side_effect = CannotConnect("offline")
        assert not await async_remove_config_entry_device(hass, entry, device)
