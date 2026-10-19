"""Saved-map safety and controls that work during an active cleaning run."""

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.button import VacuulaButton
from custom_components.vacuula.const import DOMAIN, GEN3, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.select import VacuulaSelect, create_entities
from custom_components.vacuula.vacuum import (
    VacuulaVacuum,
    cleaning_target,
    start_payload,
)


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    coordinator.robots["robot"] = RobotData(
        "robot",
        "Neato",
        GEN3,
        connected=True,
        state={
            "state": 1,
            "details": {"isDocked": True},
            "availableCommands": {"start": True, "resume": False},
        },
        capabilities={"maps": "advanced-1", "spotCleaning": "basic-1"},
    )
    coordinator.async_command = AsyncMock(return_value={})
    coordinator.async_refresh_maps = AsyncMock()
    return coordinator


def prepare_robot(coordinator, generation):
    robot = coordinator.robots["robot"]
    robot.generation = generation
    robot.state = {
        "state": 1 if generation == GEN3 else "idle",
        "details": {"isDocked" if generation == GEN3 else "is_docked": True},
        "availableCommands" if generation == GEN3 else "available_commands": {
            "start": True,
            "resume": False,
        },
    }
    robot.maps = [
        {
            "id": "2026-10-19T10:00:00Z",
            "enabled" if generation == GEN3 else "promoted": True,
        }
    ]
    return robot


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_default_start_refreshes_maps_and_preserves_no_go_lines(
    coordinator, generation
):
    robot = prepare_robot(coordinator, generation)
    maps = robot.maps
    robot.maps = []  # A map was created after the last periodic resource refresh.

    async def refresh_maps(serial):
        assert serial == "robot"
        robot.maps = maps

    coordinator.async_refresh_maps.side_effect = refresh_maps
    await VacuulaVacuum(coordinator, "robot").async_start()
    coordinator.async_refresh_maps.assert_awaited_once_with("robot")
    if generation == GEN3:
        expected = {
            "cmd": "startCleaning",
            "params": {"category": 4, "mode": 1, "navigationMode": 1},
        }
    else:
        expected = {
            "ability": "cleaning.start",
            "runs": [
                {
                    "map": {
                        "rank_id": "2026-10-19T10:00:00Z",
                        "track_id": None,
                        "nogo_enabled": True,
                    },
                    "settings": {"mode": "eco", "navigation_mode": "normal"},
                }
            ],
        }
    coordinator.async_command.assert_awaited_once_with("robot", expected)


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("docked", [False, None])
async def test_mapped_default_does_not_fall_back_when_not_docked(
    coordinator, generation, docked
):
    robot = prepare_robot(coordinator, generation)
    robot.state["details"] = {"isDocked" if generation == GEN3 else "is_docked": docked}
    with pytest.raises(ServiceValidationError) as error:
        await VacuulaVacuum(coordinator, "robot").async_start()
    assert error.value.translation_key == "map_cleaning_requires_dock"
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("maps", [[], [{"id": "a"}], [{"id": "", "active": True}]])
def test_explicit_map_requires_valid_active_map(coordinator, generation, maps):
    robot = prepare_robot(coordinator, generation)
    robot.maps = [
        {
            **item,
            "enabled" if generation == GEN3 else "promoted": item.get("active", False),
        }
        for item in maps
    ]
    with pytest.raises(ServiceValidationError) as error:
        cleaning_target(robot, "map")
    assert error.value.translation_key == "map_unavailable"


@pytest.mark.parametrize("generation", [GEN3, GEN4])
def test_default_refuses_disabled_or_unpromoted_saved_maps(coordinator, generation):
    robot = prepare_robot(coordinator, generation)
    robot.maps[0]["enabled" if generation == GEN3 else "promoted"] = False
    with pytest.raises(ServiceValidationError) as error:
        cleaning_target(robot)
    assert error.value.translation_key == "map_unavailable"


def test_gen3_matches_dock_across_enabled_maps_without_inventing_selection(coordinator):
    robot = prepare_robot(coordinator, GEN3)
    robot.maps.append({"id": "second-floor", "enabled": True})
    assert start_payload(coordinator, "robot")["params"]["category"] == 4
    assert "mapId" not in start_payload(coordinator, "robot")["params"]


def test_gen4_ambiguous_promoted_maps_fail_closed(coordinator):
    robot = prepare_robot(coordinator, GEN4)
    robot.maps.append({"id": "second-floor", "promoted": True})
    with pytest.raises(ServiceValidationError) as error:
        start_payload(coordinator, "robot")
    assert error.value.translation_key == "ambiguous_map"


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_explicit_house_is_deliberate_mapless_cleaning(coordinator, generation):
    robot = prepare_robot(coordinator, generation)
    robot.state["details"] = {}
    coordinator.cleaning_options["robot"] = {"category": "house"}
    await VacuulaVacuum(coordinator, "robot").async_start()
    coordinator.async_refresh_maps.assert_not_awaited()
    payload = coordinator.async_command.await_args.args[1]
    if generation == GEN3:
        assert payload["params"]["category"] == 2
    else:
        assert payload["runs"][0]["map"] is None


async def test_explicit_spot_preserves_size_and_does_not_use_a_map(coordinator):
    prepare_robot(coordinator, GEN3)
    coordinator.cleaning_options["robot"] = {
        "category": "spot",
        "spot_width": 250,
        "spot_height": 150,
    }
    await VacuulaVacuum(coordinator, "robot").async_start()
    coordinator.async_refresh_maps.assert_not_awaited()
    coordinator.async_command.assert_awaited_once_with(
        "robot",
        {
            "cmd": "startCleaning",
            "params": {
                "category": 3,
                "mode": 1,
                "navigationMode": 1,
                "spotWidth": 250,
                "spotHeight": 150,
            },
        },
    )


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_resume_preserves_current_run_instead_of_starting_another(
    coordinator, generation
):
    robot = prepare_robot(coordinator, generation)
    robot.state["availableCommands" if generation == GEN3 else "available_commands"][
        "resume"
    ] = True
    await VacuulaVacuum(coordinator, "robot").async_start()
    coordinator.async_refresh_maps.assert_not_awaited()
    coordinator.async_command.assert_awaited_once_with(
        "robot",
        {"cmd": "resumeCleaning"}
        if generation == GEN3
        else {"ability": "cleaning.resume"},
    )


async def test_failed_map_refresh_does_not_send_a_clean_command(coordinator):
    coordinator.async_refresh_maps.side_effect = HomeAssistantError("unavailable")
    with pytest.raises(HomeAssistantError):
        await VacuulaVacuum(coordinator, "robot").async_start()
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_cleaning_type_select_exposes_safe_and_deliberate_choices(
    coordinator, generation
):
    robot = prepare_robot(coordinator, generation)
    select = next(
        item
        for item in create_entities(coordinator, robot)
        if item.key == "cleaning_category"
    )
    assert select.current_option == "auto"
    assert {"auto", "map", "house"}.issubset(select.options)
    assert ("spot" in select.options) is (generation == GEN3)
    await select.async_select_option("map")
    assert coordinator.cleaning_options["robot"]["category"] == "map"
    assert (
        VacuulaSelect(coordinator, "robot", "cleaning_category").current_option == "map"
    )
    with pytest.raises(ServiceValidationError):
        cleaning_target(robot, "unrecognized")


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_return_home_button_remains_available_while_pause_is_possible(
    coordinator, generation
):
    robot = prepare_robot(coordinator, generation)
    flags = robot.state[
        "availableCommands" if generation == GEN3 else "available_commands"
    ]
    home = "goToBase" if generation == GEN3 else "return_to_base"
    flags.update({home: False, "pause": True})
    coordinator.async_return_to_base = AsyncMock()
    button = VacuulaButton(coordinator, "robot", "return_to_base")
    assert button.available
    await button.async_press()
    await VacuulaVacuum(coordinator, "robot").async_return_to_base()
    assert coordinator.async_return_to_base.await_args_list == [
        call("robot"),
        call("robot"),
    ]
    flags["pause"] = False
    assert not button.available
    with pytest.raises(ServiceValidationError):
        await button.async_press()


@pytest.mark.parametrize(
    ("generation", "state", "action", "activity"),
    [
        (GEN3, 2, 5, "cleaning"),
        (GEN3, 2, 7, "cleaning"),
        (GEN3, 2, 8, "cleaning"),
        (GEN3, 2, 9, "cleaning"),
        (GEN3, 2, 12, "cleaning"),
        (GEN3, 2, 13, "cleaning"),
        (GEN3, 2, 14, "cleaning"),
        (GEN3, 2, 15, "paused"),
        (GEN4, "busy", "diagnostic_test", "cleaning"),
        (GEN4, "busy", "updating_software", "cleaning"),
        (GEN4, "busy", "cleaning_standby", "paused"),
        (GEN4, "busy", "docking", "returning"),
        (GEN4, "error", "updating_software", "error"),
        (GEN3, 3, 12, "paused"),
    ],
)
def test_busy_maintenance_and_mapping_phases_have_standard_activity(
    coordinator, generation, state, action, activity
):
    robot = prepare_robot(coordinator, generation)
    robot.state.update(state=state, action=action)
    assert VacuulaVacuum(coordinator, "robot").activity == activity
