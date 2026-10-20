"""Reject ambiguous boundary replacements before they can lose no-go regions."""

# ruff: noqa: F811

from copy import deepcopy

import pytest
from homeassistant.exceptions import ServiceValidationError
from test_services import call, setup  # noqa: F401

from custom_components.vacuula.const import DOMAIN, GEN3, GEN4


def boundary(identifier):
    """Build a valid no-go line without generation-specific polygon normalization."""
    return {
        "id": identifier,
        "name": "Keep clear",
        "color": "#ff0000",
        "type": "polyline",
        "vertices": [[0.1, 0.1], [0.2, 0.2]],
        "enabled": True,
        "function": "nogo",
    }


@pytest.mark.parametrize("generation", [GEN3, GEN4])
async def test_duplicate_boundary_ids_never_reach_network_or_robot(
    hass,
    setup,
    generation,  # noqa: F811
):
    """Copied IDs cannot silently overwrite an existing no-go region upstream."""
    coordinator, robot, _, _ = setup
    robot.generation = generation
    first = boundary("existing-zone")
    second = {
        **deepcopy(first),
        "name": "Another region",
        "vertices": [[0.7, 0.7], [0.9, 0.9]],
    }
    with pytest.raises(ServiceValidationError) as error:
        await call(
            hass,
            setup,
            "set_boundaries",
            map_id="map1",
            boundaries=[first, second],
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "duplicate_boundary_ids"
    coordinator.client.async_map.assert_not_awaited()
    coordinator.async_command.assert_not_awaited()
    coordinator.async_set_map_boundaries.assert_not_awaited()


@pytest.mark.parametrize("generation", [GEN3, GEN4])
@pytest.mark.parametrize("identifiers", [[], ["third", "first", "second"]])
async def test_distinct_boundary_ids_preserve_requested_order(
    hass,
    setup,
    generation,
    identifiers,  # noqa: F811
):
    """Keep valid replacement order and continue supporting explicit empty lists."""
    coordinator, robot, _, _ = setup
    robot.generation = generation
    boundaries = [boundary(identifier) for identifier in identifiers]
    original = deepcopy(boundaries)
    await call(
        hass,
        setup,
        "set_boundaries",
        map_id="map1",
        boundaries=boundaries,
    )
    if generation == GEN3:
        written = coordinator.command_payloads[-1]["params"]["boundaries"]
    else:
        written = coordinator.async_set_map_boundaries.await_args.args[2]
    assert written == original
    assert boundaries == original
