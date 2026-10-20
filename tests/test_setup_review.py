"""Regression cases found while reviewing setup after version 0.1.5."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_IGNORE
from pytest_homeassistant_custom_component.common import MockConfigEntry
from test_server_identity import api  # noqa: F401

from custom_components.vacuula.const import DOMAIN


async def test_reconfigure_address_owned_by_ignored_discovery(hass, api):  # noqa: F811
    """An ignored announcement must not crash an existing server's address edit."""
    old_url = "http://192.0.2.10:81"
    new_url = "http://192.0.2.20:81"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=old_url,
        data={"url": old_url, "robot_ids": ["TEST-robot"]},
    )
    entry.add_to_hass(hass)
    ignored = MockConfigEntry(
        domain=DOMAIN, source=SOURCE_IGNORE, unique_id=new_url, data={}
    )
    ignored.add_to_hass(hass)
    api.return_value.async_robots.return_value = {"TEST-robot": {}}
    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
            data={"url": new_url},
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["url"] == new_url
    assert entry.unique_id == old_url
    assert ignored.unique_id == new_url
    assert ignored.data == {}
    assert (
        len(
            [
                configured
                for configured in hass.config_entries.async_entries(DOMAIN)
                if configured.unique_id == new_url
            ]
        )
        == 1
    )
