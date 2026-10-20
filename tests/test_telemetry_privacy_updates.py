"""Apply diagnostic text privacy rules after entities have been discovered."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.sensor import create_entities
from custom_components.vacuula.telemetry import safe_text, scalars


@pytest.fixture
def telemetry(hass):
    """Discover a text diagnostic from each supported source using safe values."""
    entry = MockConfigEntry(domain=DOMAIN, data={"url": "http://test.local:81"})
    entry.add_to_hass(hass)
    coordinator = VacuulaCoordinator(hass, entry, MagicMock())
    active_run = {"state": "in_progress", "message": "ready"}
    last_subrun = {"state": "done", "message": "ready"}
    last_run = {
        "timing": {"start": "2026-10-20T08:00:00Z"},
        "message": "ready",
        "runs": [last_subrun],
    }
    robot = RobotData(
        "robot",
        "D10",
        GEN4,
        connected=True,
        state={"message": "ready"},
        info={"message": "ready"},
        stats={"message": "ready", "runs": [active_run]},
        runs=[last_run],
    )
    coordinator.robots[robot.serial] = robot
    entities = {
        entity.entity_description.key: entity
        for entity in create_entities(coordinator, robot)
    }
    sources = {
        "state": robot.state,
        "info": robot.info,
        "stats": robot.stats,
        "active_run": active_run,
        "last_run": last_run,
        "last_subrun": last_subrun,
    }
    return entities, sources


@pytest.mark.parametrize(
    "source", ["state", "info", "stats", "active_run", "last_run", "last_subrun"]
)
@pytest.mark.parametrize(
    "private",
    [
        "https://private.example/path?token=private-token",
        "user@example.com",
        "-----BEGIN PRIVATE KEY----- secret",
    ],
)
async def test_private_transition_is_never_a_diagnostic_value(
    telemetry, source, private
):
    """Suppress private values on existing sensors and recover on safe telemetry."""
    entities, sources = telemetry
    entity = entities[f"telemetry_{source}_message"]
    assert entity.native_value == "ready"
    assert entity.available

    sources[source]["message"] = private
    assert entity.native_value is None
    assert not entity.available
    assert private not in str(entity.native_value)

    sources[source]["message"] = "recovered"
    assert entity.native_value == "recovered"
    assert entity.available


@pytest.mark.parametrize("value", [None, 7, False, {}, []])
async def test_text_diagnostic_rejects_later_nontext_values(telemetry, value):
    """Keep a discovered text diagnostic unknown when its field changes shape."""
    entities, sources = telemetry
    entity = entities["telemetry_stats_message"]
    sources["stats"]["message"] = value
    assert entity.native_value is None
    assert not entity.available


async def test_runtime_text_uses_the_same_full_value_rules_as_discovery(telemetry):
    """Filter private markers before truncation and preserve bounded safe text."""
    entities, sources = telemetry
    entity = entities["telemetry_stats_message"]
    private = "x" * 300 + "HTTPS://private.example/token"
    sources["stats"]["message"] = private
    assert list(scalars({"message": private})) == []
    assert entity.native_value is None
    sources["stats"]["message"] = "x" * 300
    assert entity.native_value == "x" * 255
    assert entity.available
    sources["stats"]["message"] = ""
    assert entity.native_value == ""
    assert entity.available


def test_safe_text_preserves_empty_and_filters_case_insensitively():
    """Share bounded text validation without converting malformed scalar values."""
    assert safe_text("") == ""
    assert safe_text("x" * 300) == "x" * 255
    assert safe_text("-----BeGiN PRIVATE KEY-----") is None
    assert safe_text("HtTpS://private.example") is None
