"""Privacy and no-I/O guarantees for diagnostic exports."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.diagnostics import (
    _snapshot,
    async_get_config_entry_diagnostics,
)


async def test_cached_diagnostics(hass):
    """Export useful cached telemetry without secrets, mutation or network I/O."""
    serial, name = "PRIVATE-SERIAL", "Private robot name"
    client = SimpleNamespace(
        base_url="http://private-host:81",
        async_command=AsyncMock(),
        async_robots=AsyncMock(),
        async_server_info=AsyncMock(),
    )
    robot = RobotData(
        serial,
        name,
        GEN3,
        connected=True,
        state={
            "charge": 84,
            "isCharging": True,
            "details": {
                "charge": 84,
                "isDocked": True,
                "isCharging": True,
                "serial": serial,
                "name": name,
            },
            "meta": {
                "firmware": "4.5.3-189",
                "modelName": "BotVacConnectedD5",
                "serial": serial,
            },
            "error": None,
            "availableServices": {"houseCleaning": "1.0", "token": "secret"},
            "cleaning": {"mode": "eco", "coordinates": [1.23, 4.56]},
            "errors": [{"code": 7, "message": serial}],
            "status": f"Connected robot {serial}",
            "cert": "PRIVATE-CERT",
            "new_private_field": "PRIVATE-UNKNOWN",
            serial: "PRIVATE-KEY",
        },
        info={
            "model": "D7",
            "firmware": "4.6",
            "version": 2,
            "meta": {"firmware": "4.5.3-189", "name": name, "ssid": "PRIVATE-SSID"},
            "serial": serial,
            "name": name,
            "mac": "PRIVATE-MAC",
            "wifi": {"ssid": "PRIVATE-SSID"},
            "software": {"token": "PRIVATE-NESTED"},
        },
        stats={"area": 24.5, "count": 3, "url": "PRIVATE-URL"},
        preferences={"sound": True, "language": "en", "name": name},
        schedule={"enabled": True, "events": [{"room": "Private room"}]},
        maps=[{"name": "Private floor", "coordinates": [1, 2]}],
        runs=[{"path": "Private cleaning path"}],
        capabilities={"houseCleaning": "1.0", "secret": "PRIVATE-CAPABILITY"},
    )
    before = deepcopy(robot)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": client.base_url, "robot_ids": [serial], "token": "PRIVATE-TOKEN"},
        options={"name": name},
    )
    entry.runtime_data = SimpleNamespace(
        client=client,
        robots={serial: robot},
        last_update_success=True,
        server_info={"version": "1.2.3", "host": "PRIVATE-HOST"},
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["server"] == {"version": "1.2.3"}
    exported = result["robots"][0]
    assert exported["info"] == {
        "model": "D7",
        "firmware": "4.6",
        "version": 2,
        "meta": {"firmware": "4.5.3-189"},
    }
    assert exported["state"]["details"] == {
        "charge": 84,
        "isDocked": True,
        "isCharging": True,
    }
    assert exported["state"]["meta"] == {
        "firmware": "4.5.3-189",
        "modelName": "BotVacConnectedD5",
    }
    assert exported["state"]["status"] == "**REDACTED**"
    assert exported["state"]["errors"] == [{"code": 7}]
    assert exported["state"]["cleaning"] == {"mode": "eco"}
    assert exported["schedule"] == {"enabled": True, "event_count": 1}
    assert exported["map_count"] == exported["run_count"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert "Private" not in json.dumps(result)
    assert robot == before
    client.async_command.assert_not_called()
    client.async_robots.assert_not_called()
    client.async_server_info.assert_not_called()


async def test_empty_and_gen4(hass):
    """Handle empty entries and unknown Gen4 schedule events in diagnostics."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.runtime_data = SimpleNamespace(
        client=SimpleNamespace(base_url=""),
        robots={},
        server_info={},
        last_update_success=False,
    )
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "server": {},
        "last_update_success": False,
        "robots": [],
    }
    robot = RobotData(
        "serial", "name", GEN4, schedule={"isEnabled": False, "events": None}
    )
    entry.runtime_data.robots[robot.serial] = robot
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["robots"][0]["schedule"] == {"enabled": False, "event_count": None}


def test_sanitizer():
    """Redact private strings and unsupported objects; preserve safe scalars."""
    assert _snapshot(
        {
            "status": "http://hidden.local:81",
            "error": "-----BEGIN CERTIFICATE-----",
            "model": "email@example.com",
            "software": "RobotName firmware",
            "errors": ({"code": 3},),
            "count": 4,
            "result": object(),
            42: "private",
        },
        ("RobotName",),
    ) == {
        "status": "**REDACTED**",
        "error": "**REDACTED**",
        "model": "**REDACTED**",
        "software": "**REDACTED**",
        "errors": [{"code": 3}],
        "count": 4,
        "result": "**REDACTED**",
    }
