"""Test bounded map decoding, PNG output, caching and dynamic maps."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import CannotConnect
from custom_components.vacuula.const import DOMAIN, GEN3
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.image import (
    VacuulaMapImage,
    async_setup_entry,
    render_map,
)


def test_render():
    png = render_map({"width": 3, "height": 2, "rle": [[0, 2], [1, 2], [255, 2]]})
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        assert image.size == (3, 2)
        assert [image.getpixel((x, y)) for y in range(2) for x in range(3)] == [
            0,
            0,
            1,
            1,
            2,
            2,
        ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"width": True, "height": 1},
        {"width": 5000, "height": 5000},
        {"width": 0, "height": 2},
        {"width": 1, "height": 1, "rle": "bad"},
        {"width": 1, "height": 1, "rle": [[1]]},
        {"width": 1, "height": 1, "rle": [[1, 2]]},
        {"width": 1, "height": 1, "rle": [[1, -1]]},
        {"width": 1, "height": 1, "rle": [[-1, 1]]},
        {"width": 1, "height": 1, "rle": []},
    ],
)
def test_bad_maps(payload):
    with pytest.raises(ValueError):
        render_map(payload)


async def test_image_entity(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_map = AsyncMock(
        return_value={"width": 1, "height": 1, "rle": [[1, 1]]}
    )
    coordinator = VacuulaCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    robot = RobotData("test", "Neato", GEN3, maps=[{"id": "map1", "name": "Floor"}])
    coordinator.robots = {"test": robot}
    coordinator.async_set_updated_data(coordinator.robots)
    entity = VacuulaMapImage(coordinator, "test", "map1")
    entity.hass = hass
    assert entity.device_info["identifiers"] == {(DOMAIN, "test")}
    assert entity.translation_placeholders == {"map_name": "Floor"}
    assert entity.image_last_updated is None
    assert await entity.async_image()
    assert await entity.async_image()
    client.async_map.assert_awaited_once()
    assert entity.image_last_updated is not None
    entity._fetched = -1000
    client.async_map.side_effect = CannotConnect("offline")
    assert await entity.async_image() is None
    robot.maps = []
    assert not entity.available
    assert await entity.async_image() is None
    assert entity.translation_placeholders == {"map_name": "Floor"}
    add = MagicMock()
    await async_setup_entry(hass, entry, add)
    add.assert_not_called()
    robot.maps = [{"id": "map2"}, {}]
    coordinator.async_set_updated_data(coordinator.robots)
    add.assert_called_once()
    coordinator.async_set_updated_data(coordinator.robots)
    add.assert_called_once()
