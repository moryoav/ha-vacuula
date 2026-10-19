"""Load the integration through HA and exercise real platform service routing."""

from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import VacuulaClient
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4, PLATFORMS


class LocalServer:
    """A stateful, in-memory substitute for the server protocol."""

    def __init__(self, generation):
        self.robots = {}
        self.state = {}
        self.preferences = {}
        self.language = {}
        self.client = MagicMock(spec=VacuulaClient)
        self.client.async_server_info = AsyncMock(return_value={"version": "1.0.0"})
        self.client.async_robots = AsyncMock(side_effect=lambda: deepcopy(self.robots))
        self.client.async_command = AsyncMock(side_effect=self.command)
        self.client.async_maps = AsyncMock(
            return_value=[{"id": "floor", "name": "Downstairs", "enabled": True}]
        )
        self.client.async_runs = AsyncMock(side_effect=self.runs)
        self.client.async_map = AsyncMock(
            return_value={"width": 2, "height": 1, "rle": [[0, 1], [1, 1]]}
        )
        self.add_robot("TEST-robot", generation)

    def add_robot(self, serial, generation):
        self.robots[serial] = {
            "type": generation,
            "name": "D7" if generation == GEN3 else "D10",
            "connected": True,
            "label_serial": "AAAAN2TEST" if generation == GEN4 else "D7TEST",
        }
        if generation == GEN3:
            self.state[serial] = {
                "state": 1,
                "action": 0,
                "error": None,
                "alert": None,
                "details": {
                    "charge": 100,
                    "isCharging": False,
                    "isDocked": True,
                    "isScheduleEnabled": False,
                    "dockHasBeenSeen": True,
                },
                "cleaning": {
                    "mode": 1,
                    "category": 2,
                    "navigationMode": 1,
                    "modifier": 1,
                    "spotHeight": 200,
                    "spotWidth": 200,
                },
                "availableCommands": {
                    "start": True,
                    "stop": True,
                    "pause": True,
                    "resume": False,
                    "goToBase": True,
                },
                "availableServices": {
                    "findMe": True,
                    "spotCleaning": True,
                    "maps": True,
                    "preferences": True,
                    "logCopy": True,
                },
            }
            self.preferences[serial] = {
                "robotSounds": True,
                "dirtbinAlertReminderInterval": 60,
                "filterChangeReminderInterval": 60 * 1440,
                "brushChangeReminderInterval": 180 * 1440,
            }
        else:
            self.state[serial] = {
                "state": "idle",
                "action": "idle",
                "errors": [],
                "details": {
                    "charge": 100,
                    "is_charging": False,
                    "is_docked": True,
                    "is_quickboost": False,
                    "base_type": "standard",
                },
                "available_commands": {
                    "start": True,
                    "cancel": True,
                    "pause": True,
                    "resume": False,
                    "return_to_base": True,
                },
                "meta": {"modelName": "D10", "firmware": "1.4.0"},
            }
            self.preferences[serial] = {"mode": "eco", "force_floorplan": False}
            self.language[serial] = {
                "language": "en",
                "available_languages": ["en", "de"],
            }

    async def runs(self, serial):
        if self.robots[serial]["type"] == GEN3:
            return [
                {
                    "id": "last-run",
                    "run": {
                        "start_at": "2026-10-19T10:00:00Z",
                        "end_at": "2026-10-19T10:20:00Z",
                        "cleaned_area": 12.5,
                        "status": "done",
                        "launched_from": "app",
                        "run_charge_at_start": 100,
                        "run_charge_at_end": 80,
                        "picked_up_count": 0,
                    },
                }
            ]
        return [
            {
                "id": "last-run",
                "run": {
                    "timing": {
                        "start": "2026-10-19T10:00:00Z",
                        "end": "2026-10-19T10:20:00Z",
                    }
                },
                "runs": [{"state": "done", "stats": {"area": 12.5, "pickup_count": 0}}],
                "cleaning_complete": True,
                "started_by": "app",
                "battery_consumed": 20,
            }
        ]

    async def command(self, serial, payload):
        """Persist setters so the coordinator's confirming read observes them."""
        gen3 = self.robots[serial]["type"] == GEN3
        command = payload.get("cmd") if gen3 else payload.get("ability")
        state = self.state[serial]
        if command in {"getRobotState", "state.show"}:
            value = state
        elif command in {"getGeneralInfo", "info.robot"}:
            value = {
                "model": "D7" if gen3 else "D10",
                "firmware": "1.4.0",
                "productNumber": "NEATO-TEST",
                "battery": {
                    "timeToEmpty": 120,
                    "timeToFullCharge": 0,
                    "totalCharges": 12,
                    "manufacturingDate": "2024-01-02",
                    "vendor": "Neato",
                    "authorizationStatus": 1,
                },
            }
        elif command in {"getLocalStats", "cleaning.show"}:
            value = {"cleanedArea": 125.0, "cleaningTime": 1800}
        elif command in {"getPreferences", "settings.cleaning_options"}:
            value = self.preferences[serial]
        elif command == "settings.robot_language":
            value = self.language[serial]
        elif command in {"getSchedule", "scheduling.show"}:
            value = {"enabled": False, "events": []}
        else:
            value = {}
            if command == "setPreferences":
                self.preferences[serial].update(payload["params"])
            elif command in {"enableSchedule", "disableSchedule"}:
                state["details"]["isScheduleEnabled"] = command == "enableSchedule"
            elif command == "settings.set_robot_language":
                self.language[serial]["language"] = payload["language"]
            elif command == "settings.set_cleaning_options":
                self.preferences[serial]["force_floorplan"] = payload["force_floorplan"]
            elif command in {
                "startCleaning",
                "cleaning.start",
                "resumeCleaning",
                "cleaning.resume",
            }:
                state.update(
                    state=2 if gen3 else "busy", action=1 if gen3 else "cleaning"
                )
                state["details"]["isDocked" if gen3 else "is_docked"] = False
                state["availableCommands" if gen3 else "available_commands"][
                    "resume"
                ] = False
            elif command in {"pauseCleaning", "cleaning.pause"}:
                state["state"] = 3 if gen3 else "paused"
                state["availableCommands" if gen3 else "available_commands"][
                    "resume"
                ] = True
            elif command in {"sendToBase", "navigation.return_to_base"}:
                state.update(
                    state=2 if gen3 else "busy", action=4 if gen3 else "docking"
                )
            elif command in {"stopCleaning", "cleaning.cancel"}:
                state.update(state=1 if gen3 else "idle", action=0 if gen3 else "idle")
                state["availableCommands" if gen3 else "available_commands"][
                    "resume"
                ] = False
        if gen3:
            return {"result": "ok", "data": deepcopy(value)}
        return {"code": 200, "body": deepcopy(value)}


@pytest.fixture(params=[GEN3, GEN4])
async def loaded(hass, request):
    server = LocalServer(request.param)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Vacuula",
        unique_id="http://test.local:81",
        data={"url": "http://test.local:81", "robot_ids": ["TEST-robot"]},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.vacuula.VacuulaClient", return_value=server.client),
        patch(
            "custom_components.vacuula.coordinator.VacuulaCoordinator.async_start",
            new=AsyncMock(),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        yield entry, server, request.param
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


def entity_id(hass, domain, key, serial="TEST-robot"):
    result = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{serial}_{key}")
    assert result is not None, (domain, serial, key)
    return result


async def test_every_platform_loads_translated_entities_and_device(
    hass, loaded, caplog
):
    entry, server, generation = loaded
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    expected_domains = {str(platform) for platform in PLATFORMS}
    if generation == GEN4:
        expected_domains.remove("number")
    assert {entity.domain for entity in entities} == expected_domains
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "TEST-robot"), entry.entry_id
    )
    assert device is not None
    assert device.name == ("D7" if generation == GEN3 else "D10")
    assert device.manufacturer == "Neato"
    assert device.model == ("D7" if generation == GEN3 else "D10")
    assert device.sw_version == "1.4.0"
    assert device.configuration_url == "http://test.local:81/web"
    battery = hass.states.get(entity_id(hass, "sensor", "battery"))
    assert battery.state == "100"
    assert battery.attributes["unit_of_measurement"] == "%"
    assert battery.attributes["friendly_name"] == f"{device.name} Battery"
    assert hass.states.get(entity_id(hass, "vacuum", "vacuum")).state == "docked"
    assert hass.states.get(entity_id(hass, "sensor", "last_run_area")).state == "12.5"
    assert (
        hass.states.get(entity_id(hass, "sensor", "last_run_duration")).state
        == "1200.0"
    )
    assert hass.states.get(entity_id(hass, "binary_sensor", "charging")).state == "off"
    assert hass.states.get(entity_id(hass, "binary_sensor", "connected")).state == "on"
    if generation == GEN3:
        upload = registry.async_get(entity_id(hass, "button", "button_upload_logs"))
        assert upload.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert not [
        record
        for record in caplog.get_records("setup") + caplog.records
        if record.levelno >= 30
        and record.name.startswith(("homeassistant.", "custom_components.vacuula"))
        and "custom integration" not in record.message
    ]
    assert server.client.async_command.await_count >= 5


async def test_native_vacuum_services_execute_once_and_confirm_state(hass, loaded):
    _entry, server, generation = loaded
    vacuum = entity_id(hass, "vacuum", "vacuum")
    expected = {
        "start": "startCleaning" if generation == GEN3 else "cleaning.start",
        "pause": "pauseCleaning" if generation == GEN3 else "cleaning.pause",
        "return_to_base": "sendToBase"
        if generation == GEN3
        else "navigation.return_to_base",
        "stop": "stopCleaning" if generation == GEN3 else "cleaning.cancel",
        "locate": "findMe" if generation == GEN3 else "utilities.find_me",
    }
    for service, command in expected.items():
        server.client.async_command.reset_mock()
        await hass.services.async_call(
            "vacuum", service, {"entity_id": vacuum}, blocking=True
        )
        await hass.async_block_till_done()
        sent = [call.args[1] for call in server.client.async_command.await_args_list]
        assert (sent[0].get("cmd") or sent[0].get("ability")) == command
        assert (
            sum((item.get("cmd") or item.get("ability")) == command for item in sent)
            == 1
        )
        if service == "start":
            assert hass.states.get(vacuum).state == "cleaning"
        elif service == "pause":
            assert hass.states.get(vacuum).state == "paused"
        elif service == "return_to_base":
            assert hass.states.get(vacuum).state == "returning"


async def test_native_button_services_share_confirmed_vacuum_actions(hass, loaded):
    _entry, server, generation = loaded
    gen3 = generation == GEN3
    actions = [
        ("start", "startCleaning" if gen3 else "cleaning.start"),
        ("pause", "pauseCleaning" if gen3 else "cleaning.pause"),
        ("resume", "resumeCleaning" if gen3 else "cleaning.resume"),
        ("return_to_base", "sendToBase" if gen3 else "navigation.return_to_base"),
        ("stop", "stopCleaning" if gen3 else "cleaning.cancel"),
        ("locate", "findMe" if gen3 else "utilities.find_me"),
    ]
    if gen3:
        actions.append(("clean_spot", "startCleaning"))
    for action, expected_command in actions:
        server.client.async_command.reset_mock()
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, "button", f"button_{action}")},
            blocking=True,
        )
        commands = [
            call.args[1] for call in server.client.async_command.await_args_list
        ]
        assert (
            commands[0].get("cmd") or commands[0].get("ability")
        ) == expected_command
        assert (
            sum(
                (item.get("cmd") or item.get("ability")) == expected_command
                for item in commands
            )
            == 1
        )
        if action == "clean_spot":
            assert commands[0]["params"]["category"] == 3


async def test_native_settings_services_preserve_preferences(hass, loaded):
    entry, server, generation = loaded
    original_count = server.client.async_command.await_count
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id(hass, "select", "cleaning_mode"), "option": "turbo"},
        blocking=True,
    )
    await hass.services.async_call(
        "vacuum",
        "set_fan_speed",
        {"entity_id": entity_id(hass, "vacuum", "vacuum"), "fan_speed": "eco"},
        blocking=True,
    )
    assert server.client.async_command.await_count == original_count
    assert entry.options["cleaning_options"]["TEST-robot"]["mode"] == "eco"
    if generation == GEN3:
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id(hass, "switch", "robot_sounds")},
            blocking=True,
        )
        assert server.preferences["TEST-robot"]["robotSounds"] is False
        assert (
            server.preferences["TEST-robot"]["filterChangeReminderInterval"]
            == 60 * 1440
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": entity_id(hass, "number", "bin_reminder"), "value": 90},
            blocking=True,
        )
        assert server.preferences["TEST-robot"]["dirtbinAlertReminderInterval"] == 90
        assert (
            hass.states.get(entity_id(hass, "number", "bin_reminder")).state == "90.0"
        )
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": entity_id(hass, "switch", "schedule")},
            blocking=True,
        )
        assert hass.states.get(entity_id(hass, "switch", "schedule")).state == "on"
    else:
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": entity_id(hass, "select", "robot_language"), "option": "de"},
            blocking=True,
        )
        assert server.language["TEST-robot"]["language"] == "de"
        assert (
            hass.states.get(entity_id(hass, "select", "robot_language")).state == "de"
        )
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": entity_id(hass, "switch", "force_floorplan")},
            blocking=True,
        )
        assert server.preferences["TEST-robot"]["force_floorplan"] is True
        assert (
            hass.states.get(entity_id(hass, "switch", "force_floorplan")).state == "on"
        )


async def test_reload_and_unload_keep_registry_identity(hass, loaded):
    entry, _server, _generation = loaded
    original = {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }
    coordinator = entry.runtime_data
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator._stopping
    assert entry.runtime_data is not coordinator
    assert {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    } == original
    assert hass.states.get(entity_id(hass, "vacuum", "vacuum")).state == "docked"
    reloaded_coordinator = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert reloaded_coordinator._stopping
    unavailable = hass.states.get(entity_id(hass, "vacuum", "vacuum"))
    assert unavailable.state == "unavailable"
    assert unavailable.attributes["restored"] is True


async def test_offline_robot_keeps_cached_map_and_refresh_action(hass, loaded):
    entry, server, _generation = loaded
    coordinator = entry.runtime_data
    server.robots["TEST-robot"]["connected"] = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "vacuum", "vacuum")).state == "unavailable"
    assert hass.states.get(entity_id(hass, "binary_sensor", "connected")).state == "off"
    assert hass.states.get(entity_id(hass, "image", "map_floor")).state != "unavailable"
    commands = server.client.async_command.await_count
    maps = server.client.async_maps.await_count
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, "button", "button_refresh")},
        blocking=True,
    )
    assert server.client.async_command.await_count == commands
    assert server.client.async_maps.await_count == maps + 1
    server.robots["TEST-robot"]["connected"] = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "vacuum", "vacuum")).state == "docked"
    assert hass.states.get(entity_id(hass, "binary_sensor", "connected")).state == "on"


async def test_push_readiness_blocks_start_and_routes_resume(hass, loaded):
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    gen3 = generation == GEN3
    key = "availableCommands" if gen3 else "available_commands"
    partial = {key: {"start": False, "resume": False}}

    def event(state):
        return (
            {"data": {"type": "state", "data": state}}
            if gen3
            else {"data": {"to": {"service": "peer.state"}, "body": state}}
        )

    coordinator._handle_event("TEST-robot", event(partial))
    await hass.async_block_till_done()
    assert (
        hass.states.get(entity_id(hass, "button", "button_start")).state
        == "unavailable"
    )
    commands = server.client.async_command.await_count
    vacuum = entity_id(hass, "vacuum", "vacuum")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "vacuum", "start", {"entity_id": vacuum}, blocking=True
        )
    assert server.client.async_command.await_count == commands
    partial[key]["resume"] = True
    coordinator._handle_event("TEST-robot", event(partial))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "vacuum", "start", {"entity_id": vacuum}, blocking=True
    )
    first = server.client.async_command.await_args_list[commands].args[1]
    assert first == (
        {"cmd": "resumeCleaning"} if gen3 else {"ability": "cleaning.resume"}
    )


async def test_late_robot_and_telemetry_add_once(hass, loaded):
    entry, server, generation = loaded
    coordinator = entry.runtime_data
    registry = er.async_get(hass)
    initial = len(er.async_entries_for_config_entry(registry, entry.entry_id))
    late_generation = GEN4 if generation == GEN3 else GEN3
    server.add_robot("LATE-robot", late_generation)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    late_vacuum = entity_id(hass, "vacuum", "vacuum", "LATE-robot")
    assert hass.states.get(late_vacuum).state == "docked"
    after = len(er.async_entries_for_config_entry(registry, entry.entry_id))
    assert after > initial
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == after
    if generation == GEN3:
        server.preferences["TEST-robot"]["robotSounds"] = False
    else:
        server.state["TEST-robot"]["cleaning_center"] = {"bag_status": "empty"}
    await coordinator.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    final = len(er.async_entries_for_config_entry(registry, entry.entry_id))
    await coordinator.async_refresh_robot("TEST-robot", full=True)
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == final
