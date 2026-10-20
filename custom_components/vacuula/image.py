"""Private floor map images, rendered locally without an external image service."""

import struct
import zlib
from datetime import datetime
from time import monotonic
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import VacuulaConfigEntry
from .client import VacuulaError
from .const import DOMAIN
from .coordinator import VacuulaCoordinator
from .entity import (
    async_remove_stale_entity,
    async_remove_stale_records,
    set_name_placeholders,
)

PARALLEL_UPDATES = 0

MAX_PIXELS = 4_194_304


def _chunk(kind: bytes, value: bytes) -> bytes:
    """Encode a PNG chunk with its length, type, payload and CRC checksum."""
    return (
        struct.pack(">I", len(value))
        + kind
        + value
        + struct.pack(">I", zlib.crc32(kind + value))
    )


def render_map(data: dict[str, Any]) -> bytes:
    """Validate a bounded RLE floor grid and encode an indexed-color PNG."""
    width, height = data.get("width"), data.get("height")
    if (
        type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
        or width * height > MAX_PIXELS
    ):
        raise ValueError("Invalid map dimensions")
    rle = data.get("rle")
    if not isinstance(rle, list) or len(rle) > MAX_PIXELS:
        raise ValueError("Invalid map encoding")
    cells = bytearray()
    count = width * height
    for pair in rle:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Invalid map run")
        value, repeat = pair
        if (
            type(value) is not int
            or value < 0
            or type(repeat) is not int
            or repeat < 1
            or repeat > count - len(cells)
        ):
            raise ValueError("Invalid map run size")
        cells.extend(bytes([0 if value == 0 else 1 if value == 1 else 2]) * repeat)
    if len(cells) != count:
        raise ValueError("Incomplete map")
    raw = b"".join(b"\0" + cells[row : row + width] for row in range(0, count, width))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
        + _chunk(b"PLTE", bytes([23, 29, 36, 226, 234, 240, 48, 171, 160]))
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VacuulaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover stored map images and reconcile deleted records on each update."""
    coordinator = entry.runtime_data
    # Shared through the coordinator so removed maps and robots can return.
    known = coordinator.known_entities.setdefault(__name__, {})

    @callback
    def add_maps() -> None:
        """Remove stale map records and add images for newly discovered map IDs."""
        entities: list[VacuulaMapImage] = []
        for serial, robot in coordinator.robots.items():
            if not robot.listed:
                continue
            map_ids = {
                map_id
                for floor_map in robot.maps
                if isinstance(map_id := floor_map.get("id"), str) and map_id
            }
            if robot.capabilities.get("maps_available") is True:
                async_remove_stale_records(
                    coordinator,
                    serial,
                    __name__,
                    "image",
                    f"{serial}_map_",
                    {f"{serial}_map_{map_id}" for map_id in map_ids},
                )
            added = known.setdefault(serial, set())
            for map_id in sorted(map_ids):
                if f"{serial}_map_{map_id}" in added:
                    continue
                added.add(f"{serial}_map_{map_id}")
                entities.append(VacuulaMapImage(coordinator, serial, map_id))
        if entities:
            async_add_entities(entities)

    add_maps()
    entry.async_on_unload(coordinator.async_add_listener(add_maps))


class VacuulaMapImage(CoordinatorEntity[VacuulaCoordinator], ImageEntity):
    """Render the selected stored map only when its image is requested."""

    _attr_has_entity_name = True
    _attr_translation_key = "floor_map"
    _attr_content_type = "image/png"

    def __init__(
        self, coordinator: VacuulaCoordinator, serial: str, map_id: str
    ) -> None:
        """Bind a stored map to its robot and initialize the lazy PNG cache."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self.serial = serial
        self.map_id = map_id
        self._attr_unique_id = f"{serial}_map_{map_id}"
        self._image: bytes | None = None
        self._fetched = 0.0
        self._updated: datetime | None = None
        self._update_map_name()

    @property
    def device_info(self) -> DeviceInfo:
        """Associate the map image with the existing robot device."""
        return DeviceInfo(identifiers={(DOMAIN, self.serial)})

    def _update_map_name(self) -> None:
        """Refresh the translated entity name from the current stored map name."""
        robot = self.coordinator.robots[self.serial]
        name = next(
            (
                str(m.get("name") or "Floor")
                for m in robot.maps
                if str(m.get("id")) == self.map_id
            ),
            "Floor",
        )
        set_name_placeholders(self, {"map_name": name})

    @property
    def available(self) -> bool:
        """Require a successful update and a map still present in the cached list."""
        return super().available and any(
            str(m.get("id")) == self.map_id
            for m in self.coordinator.robots[self.serial].maps
        )

    @property
    def image_last_updated(self) -> datetime | None:
        """Return the last image fetch or cache-invalidation timestamp."""
        return self._updated

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remove deleted maps, follow renames and expire PNGs after five minutes."""
        robot = self.coordinator.robots[self.serial]
        if (
            robot.listed
            and robot.capabilities.get("maps_available") is True
            and not any(str(m.get("id")) == self.map_id for m in robot.maps)
        ):
            # The server's map list was read and no longer has this map.
            async_remove_stale_entity(self, self.serial)
            return
        self._update_map_name()
        if self._image is not None and monotonic() - self._fetched >= 300:
            self._image = None
            self._updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Render a map off the event loop and cache its PNG for five minutes."""
        if not self.available:
            return None
        if self._image is not None and monotonic() - self._fetched < 300:
            return self._image
        try:
            data = await self.coordinator.client.async_map(self.serial, self.map_id)
            image = await self.hass.async_add_executor_job(render_map, data)
        except VacuulaError, ValueError:
            return None
        self._image = image
        self._fetched = monotonic()
        self._updated = dt_util.utcnow()
        return image
