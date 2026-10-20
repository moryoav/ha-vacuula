"""Keep malformed optional language settings isolated from cleaning controls."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.select import VacuulaSelect, create_entities


@pytest.fixture
def coordinator(hass):
    """Provide a connected Gen4 robot with a working language selector."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    coordinator.robots["robot"] = RobotData(
        "robot",
        "D10",
        GEN4,
        connected=True,
        preferences={"language": "en", "available_languages": ["en", "de"]},
    )
    coordinator.async_command = AsyncMock()
    return coordinator


@pytest.mark.parametrize("languages", [None, 7, "en", {"en": True}, [], [None, ""]])
async def test_malformed_language_choices_disable_only_language(coordinator, languages):
    """Reject malformed choices after discovery and recover when valid data returns."""
    robot = coordinator.robots["robot"]
    language = VacuulaSelect(coordinator, robot.serial, "robot_language")
    assert language.options == ["en", "de"]
    assert language.available

    robot.preferences["available_languages"] = languages
    assert language.options == []
    assert language.current_option is None
    assert not language.available
    assert {entity.key for entity in create_entities(coordinator, robot)} == {
        "cleaning_mode",
        "navigation_mode",
        "cleaning_category",
    }
    for entity in create_entities(coordinator, robot):
        assert entity.available
        assert entity.options
    with pytest.raises(ServiceValidationError):
        await language.async_select_option("en")
    coordinator.async_command.assert_not_awaited()

    robot.preferences["available_languages"] = ["de", "en"]
    assert language.options == ["de", "en"]
    assert language.current_option == "en"
    assert language.available
    await language.async_select_option("de")
    coordinator.async_command.assert_awaited_once_with(
        "robot", {"ability": "settings.set_robot_language", "language": "de"}
    )


async def test_mixed_language_choices_are_filtered_and_deduplicated(coordinator):
    """Keep valid advertised choices in order without exposing empty or scalar data."""
    robot = coordinator.robots["robot"]
    robot.preferences["available_languages"] = [
        "de",
        None,
        7,
        False,
        {},
        [],
        "",
        "   ",
        "en",
        "de",
    ]
    language = VacuulaSelect(coordinator, robot.serial, "robot_language")
    assert language.options == ["de", "en"]
    assert language.available
    assert "robot_language" in {
        entity.key for entity in create_entities(coordinator, robot)
    }
    for invalid in ("", "   ", "e", "fr"):
        with pytest.raises(ServiceValidationError):
            await language.async_select_option(invalid)
    coordinator.async_command.assert_not_awaited()


@pytest.mark.parametrize("current", [None, 7, "unsupported"])
async def test_known_language_choices_can_repair_unknown_current_value(
    coordinator, current
):
    """Allow selecting an advertised language when the current value is unknown."""
    robot = coordinator.robots["robot"]
    robot.preferences["language"] = current
    language = VacuulaSelect(coordinator, robot.serial, "robot_language")
    assert language.current_option is None
    assert language.available
    await language.async_select_option("de")
    coordinator.async_command.assert_awaited_once_with(
        "robot", {"ability": "settings.set_robot_language", "language": "de"}
    )


async def test_missing_current_language_preserves_choices(coordinator):
    """Keep a valid language list usable when its current-language field is absent."""
    robot = coordinator.robots["robot"]
    robot.preferences.pop("language")
    language = VacuulaSelect(coordinator, robot.serial, "robot_language")
    assert language.options == ["en", "de"]
    assert language.current_option is None
    assert language.available
