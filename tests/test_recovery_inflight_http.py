"""Exercise recovery while a disconnected HTTP handler still has work to commit."""

import asyncio
import json
import socket
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuula.client import VacuulaClient
from custom_components.vacuula.const import DOMAIN, GEN4
from custom_components.vacuula.coordinator import VacuulaCoordinator
from custom_components.vacuula.data import RobotData
from custom_components.vacuula.vacuum import start_payload


class UnfinishedMapServer(ThreadingHTTPServer):
    """Model Vacuula's threaded handlers continuing after an HTTP connection drops."""

    def __init__(self):
        """Expose an active floorplan and another map without connecting to hardware."""
        self.maps = [
            {"id": "mapA", "promoted": True},
            {"id": "mapB", "promoted": False},
        ]
        self.boundaries = []
        self.uploaded = []
        self.release_handler = threading.Event()
        self.mutation_finished = threading.Event()
        super().__init__(("127.0.0.1", 0), UnfinishedMapHandler)


class UnfinishedMapHandler(BaseHTTPRequestHandler):
    """Serve real HTTP reads while a dropped map-write request is unfinished."""

    def log_message(self, *_args):
        """Keep expected loopback requests out of test logs."""

    def respond(self, data):
        """Return one complete JSON response with a known content length."""
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Read the currently committed snapshot without waiting for other handlers."""
        if self.path.endswith("/boundaries"):
            self.respond(deepcopy(self.server.boundaries))
        elif self.path.endswith("/maps"):
            self.respond(deepcopy(self.server.maps))
        else:
            self.respond({})

    def do_POST(self):
        """Lose the reply before mutation, then finish only when the test allows it."""
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = json.loads(raw) if raw else None
        is_boundary_save = self.path.endswith("/boundaries")
        is_promotion = self.path.endswith("/promote")
        if not is_boundary_save and not is_promotion:
            self.respond({"body": {}})
            return

        # A disconnect does not cancel Vacuula's Python request handler. Another
        # thread can return the old map or boundary snapshot before this write.
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()
        if not self.server.release_handler.wait(10):
            return
        if is_boundary_save:
            self.server.boundaries = payload
        else:
            # Vacuula removes the active map before attempting the new upload.
            # If that upload fails, no promoted floorplan remains in its DB.
            for floor_map in self.server.maps:
                floor_map["promoted"] = False
        self.server.mutation_finished.set()


@pytest.fixture
async def unfinished_map_setup(hass, socket_enabled):
    """Use the real HTTP client with a loopback server and controlled commit order."""
    server = UnfinishedMapServer()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    entry = MockConfigEntry(domain=DOMAIN, data={"url": url})
    entry.add_to_hass(hass)
    try:
        async with aiohttp.ClientSession() as session:
            coordinator = VacuulaCoordinator(hass, entry, VacuulaClient(session, url))
            robot = RobotData("robot", "Neato", GEN4, True)
            robot.state = {"details": {"is_docked": True}}
            coordinator.robots[robot.serial] = robot
            yield coordinator, server
    finally:
        server.release_handler.set()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.to_thread(server_thread.join, 3)


async def test_unchanged_read_cannot_resolve_unfinished_boundary_post(
    unfinished_map_setup,
):
    """Keep recovery when an old boundary snapshot precedes the disconnected save."""
    coordinator, server = unfinished_map_setup
    boundaries = [
        {
            "id": "new-no-go",
            "name": "Keep clear",
            "color": "#ff0000",
            "type": "polygon",
            "vertices": [[0, 0], [1, 0], [1, 1]],
            "enabled": True,
            "function": "nogo",
        }
    ]
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_map_boundaries("robot", "mapA", boundaries)
    assert server.boundaries == []
    server.release_handler.set()
    assert await asyncio.to_thread(server.mutation_finished.wait, 3)
    assert server.boundaries == boundaries
    assert server.uploaded == []
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")


@pytest.mark.parametrize("map_id", ["mapA", "mapB"])
async def test_old_active_map_cannot_resolve_unfinished_promotion(
    unfinished_map_setup, map_id
):
    """Retain the guard when a failed HTTP request later removes the active map."""
    coordinator, server = unfinished_map_setup
    with pytest.raises(HomeAssistantError):
        await coordinator.async_map_operation("robot", map_id, "promote")
    assert server.maps[0]["promoted"] is True
    server.release_handler.set()
    assert await asyncio.to_thread(server.mutation_finished.wait, 3)
    assert not any(floor_map["promoted"] for floor_map in server.maps)
    await coordinator.async_refresh_maps("robot")
    assert coordinator.has_pending_map_recovery("robot")
    with pytest.raises(ServiceValidationError):
        start_payload(coordinator, "robot")
