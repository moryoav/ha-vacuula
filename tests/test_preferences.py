"""Verify concurrent preference changes cannot overwrite unrelated settings."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData


def coordinator_for(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_maps = AsyncMock(return_value=[])
    client.async_runs = AsyncMock(return_value=[])
    coordinator = VacuulaCoordinator(hass, entry, client)
    coordinator.robots = {
        "robot": RobotData(
            "robot",
            "Neato",
            GEN3,
            True,
            preferences={"robotSounds": True, "filterChangeReminderInterval": 43200},
        )
    }
    return coordinator


async def test_concurrent_changes_read_latest_before_merge(hass):
    coordinator = coordinator_for(hass)
    server = {"robotSounds": True, "filterChangeReminderInterval": 43200}
    writes = []

    async def command(serial, payload):
        await asyncio.sleep(0)
        if payload["cmd"] == "getPreferences":
            return {"result": "ok", "data": dict(server)}
        if payload["cmd"] == "setPreferences":
            writes.append(dict(payload["params"]))
            server.update(payload["params"])
            return {"result": "ok", "data": {}}
        return {"result": "ok", "data": {}}

    coordinator.client.async_command = AsyncMock(side_effect=command)
    await asyncio.gather(
        coordinator.async_command(
            "robot", {"cmd": "setPreferences", "params": {"robotSounds": False}}
        ),
        coordinator.async_command(
            "robot",
            {
                "cmd": "setPreferences",
                "params": {"filterChangeReminderInterval": 64800},
            },
        ),
    )
    assert server == {"robotSounds": False, "filterChangeReminderInterval": 64800}
    assert len(writes) == 2
    assert writes[1]["robotSounds"] is False


@pytest.mark.parametrize(
    "reply", [{"result": "ok", "data": {}}, {"result": "ok", "data": {"other": 1}}]
)
async def test_missing_fresh_preferences_prevent_write(hass, reply):
    coordinator = coordinator_for(hass)
    coordinator.client.async_command = AsyncMock(return_value=reply)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(
            "robot", {"cmd": "setPreferences", "params": {"robotSounds": False}}
        )
    coordinator.client.async_command.assert_awaited_once_with(
        "robot", {"cmd": "getPreferences"}
    )


async def test_failed_preference_read_does_not_retry_or_write(hass):
    coordinator = coordinator_for(hass)
    coordinator.client.async_command = AsyncMock(side_effect=CannotConnect("offline"))
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(
            "robot", {"cmd": "setPreferences", "params": {"robotSounds": False}}
        )
    assert coordinator.client.async_command.await_count == 1


@pytest.mark.parametrize(
    ("write_ability", "read_ability", "settings", "change"),
    [
        (
            "settings.set_robot_language",
            "settings.robot_language",
            {
                "language": "en",
                "available_languages": ["en", "de"],
                "ability": "settings.factory_reset",
            },
            {"language": "de"},
        ),
        (
            "settings.set_cleaning_options",
            "settings.cleaning_options",
            {"force_floorplan": False, "other_setting": True},
            {"force_floorplan": True},
        ),
    ],
)
async def test_gen4_settings_preserve_exact_read_body(
    hass, write_ability, read_ability, settings, change
):
    coordinator = coordinator_for(hass)
    coordinator.robots["robot"].generation = GEN4

    async def command(serial, payload):
        if payload["ability"] == read_ability:
            return {"code": 200, "body": dict(settings)}
        return {"code": 200, "body": {}}

    coordinator.client.async_command = AsyncMock(side_effect=command)
    await coordinator.async_command("robot", {"ability": write_ability, **change})
    assert coordinator.client.async_command.await_args_list[1].args[1] == {
        **settings,
        **change,
        "ability": write_ability,
    }


async def test_failed_gen4_settings_read_prevents_write(hass):
    coordinator = coordinator_for(hass)
    coordinator.robots["robot"].generation = GEN4
    coordinator.client.async_command = AsyncMock(return_value={"code": 200, "body": {}})
    with pytest.raises(HomeAssistantError):
        await coordinator.async_command(
            "robot", {"ability": "settings.set_robot_language", "language": "de"}
        )
    coordinator.client.async_command.assert_awaited_once_with(
        "robot", {"ability": "settings.robot_language"}
    )
