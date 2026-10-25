"""Original asynchronous implementation of the local Vacuula app protocol."""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from yarl import URL

JSON_LIMIT = 16 * 1024 * 1024
SSE_LIMIT = 2 * 1024 * 1024
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=10, sock_read=40)
# Vacuula waits up to 60 seconds for the robot to acknowledge map promotion.
MAP_PROMOTE_TIMEOUT = aiohttp.ClientTimeout(total=90, connect=10, sock_read=80)
EVENT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=45)


class VacuulaError(Exception):
    """A protocol error containing only safe, locally generated text."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Keep a safe error message and optional HTTP or robot status code."""
        super().__init__(message)
        self.status = status


class CannotConnect(VacuulaError):
    """The connection or server is unavailable."""


class MapRequestNotSent(CannotConnect):
    """A map request failed to establish its connection before HTTP dispatch."""


class InvalidAuth(VacuulaError):
    """The server or reverse proxy rejected authentication."""


class CommandError(VacuulaError):
    """The server or robot rejected a command."""


class CommandTimedOut(CommandError):
    """Vacuula sent the command, but the robot did not answer within its wait.

    The robot may still have run the command, so this is not a rejection.
    """


class MapRequestRejected(CommandError):
    """A recognized Vacuula map-error reply proves its HTTP handler completed."""

    def __init__(self, *, status: int) -> None:
        """Retain a status and fixed message without storing server response text."""
        super().__init__(
            "Vacuula completed and rejected the map request", status=status
        )


class UnsupportedCommand(CommandError):
    """The robot explicitly reports an unsupported command."""


def _nonfinite(value: str) -> None:
    """Reject JSON NaN and infinity literals instead of accepting invalid numbers."""
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    """Decode a JSON float, rejecting exponents that overflow to infinity."""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def _decode_json(raw: bytes) -> Any:
    """Decode UTF-8 JSON with finite numbers and safe protocol errors."""
    try:
        return json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=_nonfinite,
            parse_float=_finite_float,
        )
    except (UnicodeError, ValueError, RecursionError) as err:
        raise VacuulaError("Invalid JSON response") from err


def _object(value: Any) -> dict[str, Any]:
    """Require a JSON object before exposing a server response to callers."""
    if not isinstance(value, dict):
        raise VacuulaError("Expected a JSON object")
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    """Require a list containing only JSON objects."""
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise VacuulaError("Expected a list of JSON objects")
    return value


def _robots(value: Any) -> dict[str, dict[str, Any]]:
    """Require robot metadata objects keyed by their server identifiers."""
    result = _object(value)
    if any(not isinstance(v, dict) for v in result.values()):
        raise VacuulaError("Invalid robot list")
    return result


def _completed_map_rejection(action: str, status: int, value: Any) -> bool:
    """Recognize exact endpoint-specific failures from Vacuula's finished handlers.

    A completed rejection may follow demotion, so this does not establish that
    the old floorplan is still active. Generic proxy failures remain ambiguous.
    """
    if not isinstance(value, dict) or set(value) != {"error"}:
        return False
    error = value["error"]
    if not isinstance(error, str):
        return False
    if action in {"demote", "delete"}:
        return status == 400 and error == "Robot not connected"
    if action == "boundaries":
        return status == 400 and error in {
            "not all required keys",
            "Invalid JSON body",
            "Invalid Content-Length header",
        }
    if action != "promote":
        return False
    if status == 400:
        return error == "Robot not connected"
    if status == 404:
        return error == "Map not found"
    if status == 502:
        return (
            error == "Failed to send promote to robot"
            or re.fullmatch(r"Robot rejected promote \(status [0-9]{3}\)", error)
            is not None
        )
    return status == 504 and error == "Robot did not acknowledge promote in time"


def unwrap(response: dict[str, Any]) -> dict[str, Any]:
    """Normalize envelopes without losing Gen3 root-level state telemetry."""
    if isinstance(response.get("body"), dict):
        return dict(response["body"])
    data = response.get("data")
    result = dict(data) if isinstance(data, dict) else {}
    result.update(
        (k, v)
        for k, v in response.items()
        if k not in {"version", "reqId", "result", "data"}
    )
    return result


class _SSEParser:
    """Parse bounded SSE frames split across arbitrary network chunks."""

    def __init__(self) -> None:
        """Initialize the partial-line buffer and state for the first SSE frame."""
        self.buffer = bytearray()
        self.size = 0
        self.event = "message"
        self.data: list[bytes] = []
        self.first_line = True

    def feed(self, chunk: bytes) -> list[tuple[str, bytes]]:
        """Return complete SSE frames while retaining bounded partial lines."""
        self.buffer.extend(chunk)
        frames: list[tuple[str, bytes]] = []
        while self.buffer:
            positions = [
                p for p in (self.buffer.find(b"\n"), self.buffer.find(b"\r")) if p >= 0
            ]
            if not positions:
                break
            end = min(positions)
            if self.buffer[end] == 13 and end == len(self.buffer) - 1:
                break
            delimiter = 2 if self.buffer[end : end + 2] == b"\r\n" else 1
            line = bytes(self.buffer[:end])
            del self.buffer[: end + delimiter]
            self.size += len(line) + delimiter
            if self.size > SSE_LIMIT:
                raise VacuulaError("Event frame exceeds size limit")
            if self.first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                self.first_line = False
            if not line:
                if self.data:
                    frames.append((self.event, b"\n".join(self.data)))
                self.event, self.data, self.size = "message", [], 0
                continue
            if line.startswith(b":"):
                continue
            field, _, value = line.partition(b":")
            value = value.removeprefix(b" ")
            if field == b"data":
                self.data.append(value)
            elif field == b"event":
                try:
                    self.event = value.decode("utf-8")
                except UnicodeError as err:
                    raise VacuulaError("Invalid event encoding") from err
        if self.size + len(self.buffer) > SSE_LIMIT:
            raise VacuulaError("Event frame exceeds size limit")
        return frames


class VacuulaClient:
    """Use an externally managed aiohttp session; never retry robot commands."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str) -> None:
        """Validate a credential-free HTTP origin and retain the shared session."""
        parts = urlsplit(base_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("A plain HTTP or HTTPS server URL is required")
        self._session = session
        self.base_url = base_url.rstrip("/")

    def _url(self, *segments: str) -> URL:
        # Vacuula matches raw paths without decoding their identifiers. Gen3
        # map/run IDs contain timestamp colons; retain those URI-safe characters
        # while still escaping path, query and fragment delimiters.
        """Build an API URL while preserving timestamp colons in raw server paths."""
        path = "/".join(quote(segment, safe=":") for segment in segments)
        return URL(f"{self.base_url}/api/{path}", encoded=True)

    @staticmethod
    async def _read(response: aiohttp.ClientResponse) -> bytes:
        """Read a response incrementally, rejecting bodies larger than JSON_LIMIT."""
        if response.content_length is not None and response.content_length > JSON_LIMIT:
            raise VacuulaError("Response exceeds size limit")
        content = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            content.extend(chunk)
            if len(content) > JSON_LIMIT:
                raise VacuulaError("Response exceeds size limit")
        return bytes(content)

    @staticmethod
    async def _map_delete_no_retry(
        request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType
    ) -> aiohttp.ClientResponse:
        """Prevent aiohttp from replaying a map DELETE after uncertain delivery."""
        try:
            return await handler(request)
        except aiohttp.ClientConnectorError:
            # No connection was established, so callers can safely distinguish
            # this from a request whose response was lost after dispatch.
            raise
        except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError) as err:
            # aiohttp retries idempotent HTTP methods after these exceptions.
            # Demotion commands must be sent once even though they use DELETE.
            raise CannotConnect("Connection to Vacuula failed") from err

    @staticmethod
    def _raise_status(status: int, *, command: bool = False) -> None:
        """Map HTTP failures to authentication, connectivity or command errors."""
        if status in (401, 403):
            raise InvalidAuth("Authentication rejected", status=status)
        if 300 <= status < 400:
            raise CannotConnect("Server redirects are not supported", status=status)
        if status == 501:
            raise UnsupportedCommand("Command is not supported", status=status)
        if status >= 500 and not command:
            raise CannotConnect("Server is unavailable", status=status)
        if status == 504:
            # Vacuula answers 504 after its robot wait; delivery is unknown.
            raise CommandTimedOut("Robot did not answer in time", status=status)
        if not 200 <= status < 300:
            raise CommandError("Request was rejected", status=status)

    async def _request(
        self,
        method: str,
        *segments: str,
        payload: Any = None,
        command: bool = False,
        allow_ok: bool = False,
        map_action: str | None = None,
        timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ) -> Any:
        """Execute one bounded request and validate status and response encoding.

        Do not follow redirects or retry commands. Accept a plain OK response only
        when the endpoint explicitly permits it.
        """
        try:
            # Request middleware replaces the shared session's redirect guard.
            # Redirects are disabled below, so that guard has no work to do;
            # the shared connector, headers and session state stay untouched.
            middlewares = (
                (self._map_delete_no_retry,)
                if map_action is not None and method == "DELETE"
                else None
            )
            async with self._session.request(
                method,
                self._url(*segments),
                json=payload,
                timeout=timeout,
                allow_redirects=False,
                middlewares=middlewares,
            ) as response:
                if response.status in (401, 403) or 300 <= response.status < 400:
                    self._raise_status(response.status, command=command)
                raw = await self._read(response)
                if allow_ok and 200 <= response.status < 300 and raw.strip() == b"OK":
                    return None
                try:
                    decoded = _decode_json(raw)
                except VacuulaError:
                    self._raise_status(response.status, command=command)
                    raise
                if map_action is not None and _completed_map_rejection(
                    map_action, response.status, decoded
                ):
                    raise MapRequestRejected(status=response.status)
                if command and isinstance(decoded, dict):
                    self._check_command(decoded, response.status)
                self._raise_status(response.status, command=command)
                return decoded
        except aiohttp.ClientConnectorError as err:
            if map_action is not None:
                raise MapRequestNotSent(
                    "Connection to Vacuula failed before sending the map request"
                ) from err
            raise CannotConnect("Connection to Vacuula failed") from err
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise CannotConnect("Connection to Vacuula failed") from err

    @staticmethod
    def _check_command(response: dict[str, Any], http_status: int) -> None:
        """Reject failed Gen3 results and failed or malformed Gen4 status codes."""
        result, code = response.get("result"), response.get("code")
        if result in ("command_not_found", "not_implemented") or code == 501:
            raise UnsupportedCommand(
                "Command is not supported", status=501 if code == 501 else http_status
            )
        if result is not None and result != "ok":
            raise CommandError("Robot rejected the command", status=http_status)
        if code is not None:
            if isinstance(code, bool) or not isinstance(code, int):
                raise VacuulaError("Invalid robot response code", status=http_status)
            if not 200 <= code < 300:
                raise CommandError("Robot rejected the command", status=code)

    async def async_server_info(self) -> dict[str, Any]:
        """Read the local server metadata as a JSON object."""
        return _object(await self._request("GET", "server"))

    async def async_server_boot_id(self) -> str | None:
        """Read the frontend's per-process ETag without caching or reading its body.

        Vacuula generates a quoted 12-character hexadecimal token on startup.
        Missing frontends, proxy responses and network failures leave the boot
        identity unknown rather than manufacturing evidence of a restart.
        """
        try:
            async with self._session.request(
                "GET",
                URL(f"{self.base_url}/web", encoded=True),
                headers={"Cache-Control": "no-cache"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                etag = response.headers.get("ETag")
                if isinstance(etag, str) and re.fullmatch(r'"[0-9a-f]{12}"', etag):
                    return etag
                return None
        except aiohttp.ClientError, TimeoutError, OSError:
            return None

    async def async_robots(self) -> dict[str, dict[str, Any]]:
        """Read robot metadata keyed by each server-side serial identifier."""
        return _robots(await self._request("GET", "robots"))

    async def async_command(
        self, serial: str, command: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one Gen3 or Gen4 command and require its success envelope."""
        gen3 = isinstance(command.get("cmd"), str)
        gen4 = isinstance(command.get("ability"), str)
        if gen3 == gen4:
            raise ValueError("A command must specify either cmd or ability")
        result = _object(
            await self._request(
                "POST", "robot", serial, "command", payload=command, command=True
            )
        )
        if gen3 and result.get("result") != "ok":
            raise VacuulaError("Robot response has no success result")
        if gen4 and (
            isinstance(result.get("code"), bool)
            or not isinstance(result.get("code"), int)
        ):
            raise VacuulaError("Robot response has no status code")
        return result

    async def async_robot_state(self, serial: str, generation: str) -> dict[str, Any]:
        """Read and unwrap the state command for the requested robot generation."""
        if generation == "neato.gen3":
            command = {"cmd": "getRobotState"}
        elif generation == "neato.gen4":
            command = {"ability": "state.show"}
        else:
            raise UnsupportedCommand("Robot generation is not supported")
        return unwrap(await self.async_command(serial, command))

    async def async_maps(self, serial: str) -> list[dict[str, Any]]:
        """Read the map records exposed by the server for one robot."""
        return _objects(await self._request("GET", "robot", serial, "maps"))

    async def async_runs(self, serial: str) -> list[dict[str, Any]]:
        """Read the server-stored cleaning history for one robot."""
        return _objects(await self._request("GET", "robot", serial, "runs"))

    async def async_map(self, serial: str, map_id: str) -> dict[str, Any]:
        """Read one saved map from the robot-scoped server endpoint."""
        return _object(await self._request("GET", "robot", serial, "maps", map_id))

    async def async_run(self, serial: str, run_id: str) -> dict[str, Any]:
        """Read one saved cleaning run from the robot-scoped server endpoint."""
        return _object(await self._request("GET", "robot", serial, "run", run_id))

    async def async_map_boundaries(
        self, serial: str, map_id: str
    ) -> list[dict[str, Any]]:
        """Read the boundary objects stored by the server for a Gen4 map."""
        return _objects(
            await self._request("GET", "robot", serial, "maps", map_id, "boundaries")
        )

    async def async_boundaries(self, serial: str, map_id: str) -> list[dict[str, Any]]:
        """Expose server-stored map boundaries through the client alias."""
        return await self.async_map_boundaries(serial, map_id)

    async def async_update_robot(self, serial: str, name: str) -> None:
        """Persist the robot name in Vacuula without changing its firmware settings."""
        await self._request(
            "POST", "robot", serial, payload={"name": name}, allow_ok=True
        )

    async def async_update_map(
        self, serial: str, map_id: str, name: str, enabled: bool
    ) -> None:
        """Write the name and enabled flag together for a saved Gen3 map."""
        await self._request(
            "POST",
            "robot",
            serial,
            "maps",
            map_id,
            payload={"name": name, "enabled": enabled},
            allow_ok=True,
        )

    async def async_map_action(
        self, serial: str, map_id: str, action: str, payload: Any = None
    ) -> Any:
        """Apply a supported map action, allowing the longer promotion timeout."""
        if action in ("demote", "delete"):
            return await self._request(
                "DELETE",
                "robot",
                serial,
                "maps",
                map_id,
                allow_ok=True,
                map_action=action,
            )
        if action not in ("promote", "use", "unuse", "boundaries"):
            raise ValueError("Unknown map action")
        return await self._request(
            "POST",
            "robot",
            serial,
            "maps",
            map_id,
            action,
            payload=payload,
            allow_ok=True,
            map_action=action,
            timeout=MAP_PROMOTE_TIMEOUT if action == "promote" else REQUEST_TIMEOUT,
        )

    async def _events(self, *segments: str) -> AsyncGenerator[tuple[str | None, Any]]:
        """Yield validated SSE events, announcing connection with a None pair."""
        try:
            async with self._session.request(
                "GET",
                self._url(*segments),
                headers={"Accept": "text/event-stream"},
                timeout=EVENT_TIMEOUT,
                allow_redirects=False,
            ) as response:
                self._raise_status(response.status)
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
                    raise VacuulaError("Expected an event stream")
                yield None, None
                parser = _SSEParser()
                async for chunk in response.content.iter_chunked(65536):
                    for event, raw in parser.feed(chunk):
                        yield event, _decode_json(raw)
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise CannotConnect("Vacuula event connection failed") from err

    async def async_server_events(
        self,
    ) -> AsyncGenerator[dict[str, dict[str, Any]]]:
        """Yield robot-list snapshots from the server event stream."""
        async with aclosing(self._events("events")) as stream:
            async for event, value in stream:
                if event == "robots":
                    yield _robots(value)

    async def async_robot_events(self, serial: str) -> AsyncGenerator[dict[str, Any]]:
        """Yield this robot's events and an initial stream-opened marker."""
        async with aclosing(self._events("robot", serial, "connect")) as stream:
            async for event, value in stream:
                if event is None:
                    yield {"_vacuula_stream_opened": True}
                    continue
                result = _object(value)
                if result.get("serial") != serial:
                    raise VacuulaError("Unexpected robot event")
                yield result
