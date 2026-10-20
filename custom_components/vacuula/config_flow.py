"""UI setup, network discovery and reconfiguration for Vacuula."""

import asyncio
from ipaddress import ip_address
from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import (
    HomeAssistantTCPConnector,
    async_get_clientsession,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from yarl import URL

from .client import VacuulaClient, VacuulaError
from .const import CONF_URL, DEFAULT_URL, DOMAIN
from .discovery import async_discover_servers

CONF_URL_ALIASES = "url_aliases"
IDENTITY_RESOLVE_TIMEOUT = 2.0


def normalize_url(value: str) -> str:
    """Accept an HTTP origin, without secrets or paths that could leak them."""
    url = URL(value.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.user is not None
        or url.password is not None
        or url.query_string
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Enter the server's HTTP or HTTPS origin")
    return str(url.origin().with_host(url.host.rstrip(".")))


def discovery_url(discovery_info: ZeroconfServiceInfo) -> str:
    """Build an origin from the advertised address, protocol and actual port."""
    scheme = discovery_info.properties.get("protocol", "http")
    if not discovery_info.port:
        raise ValueError("Missing service port")
    return normalize_url(
        str(
            URL.build(
                scheme=scheme if scheme in {"http", "https"} else "http",
                host=discovery_info.host,
                port=discovery_info.port,
            )
        )
    )


def discovery_aliases(discovery_info: ZeroconfServiceInfo) -> set[str]:
    """Use the hostname and all addresses for the advertised scheme and port."""
    origin = URL(discovery_url(discovery_info))
    hosts = [discovery_info.hostname.rstrip("."), *discovery_info.addresses]
    return {normalize_url(str(origin.with_host(host))) for host in hosts if host} | {
        str(origin)
    }


def entry_aliases(entry: config_entries.ConfigEntry) -> set[str]:
    """Accept old entries without aliases and ignore malformed optional values."""
    result = set()
    aliases = entry.data.get(CONF_URL_ALIASES, [])
    for value in [
        entry.data.get(CONF_URL),
        *(aliases if isinstance(aliases, list) else []),
    ]:
        if isinstance(value, str):
            try:
                result.add(normalize_url(value))
            except ValueError, TypeError:
                continue
    return result


def _ip_origin(value: str) -> bool:
    """Return whether an origin contains a numeric address."""
    try:
        ip_address(URL(value).host or "")
    except ValueError:
        return False
    return True


def _shared_aliases(base_url: str, aliases: set[str], configured: set[str]) -> set[str]:
    """Do not identify conflicting numeric endpoints by a shared hostname alone."""
    shared = aliases & configured
    if _ip_origin(base_url) and any(_ip_origin(url) for url in configured):
        return {url for url in shared if _ip_origin(url)}
    return shared


class VacuulaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one server, automatically discovering its robots."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Initialize discovery candidates, aliases and per-flow DNS results."""
        self._discovered_url: str | None = None
        self._discovered_servers: dict[str, ZeroconfServiceInfo] = {}
        self._discovered_aliases: set[str] = set()
        self._resolved_urls: dict[str, set[str]] = {}

    async def _validate(self, base_url: str) -> tuple[dict[str, Any], list[str]]:
        """Verify server metadata and retrieve robot IDs for duplicate detection."""
        client = VacuulaClient(async_get_clientsession(self.hass), base_url)
        info = await client.async_server_info()
        robots = await client.async_robots()
        return info, sorted(robots)

    async def _async_resolve_urls(self, base_url: str) -> set[str]:
        """Resolve only when aliases cannot already establish server identity."""
        if base_url in self._resolved_urls:
            return self._resolved_urls[base_url]
        origin = URL(base_url)
        result = {base_url}
        assert origin.host is not None
        try:
            ip_address(origin.host)
        except ValueError:
            connector = async_get_clientsession(self.hass).connector
            if isinstance(connector, HomeAssistantTCPConnector):
                try:
                    async with asyncio.timeout(IDENTITY_RESOLVE_TIMEOUT):
                        addresses = await connector.async_resolve_host(origin.host)
                    result.update(
                        normalize_url(str(origin.with_host(address["host"])))
                        for address in addresses
                    )
                except OSError, TimeoutError:
                    pass
        self._resolved_urls[base_url] = result
        return result

    async def _async_duplicate(
        self,
        base_url: str,
        robot_ids: list[str],
        aliases: set[str],
        *,
        ignore_entry_id: str | None = None,
    ) -> config_entries.ConfigEntry | None:
        """Match robot identity or the same endpoint, including legacy hostnames."""
        candidate_robots = set(robot_ids)
        entries = [
            entry
            for entry in self._async_current_entries()
            if entry.entry_id != ignore_entry_id
            # Two servers behind one reverse proxy can share an address. Robot
            # lists that are both known and disjoint mean different servers.
            and not (
                candidate_robots
                and (configured := set(entry.data.get("robot_ids", [])))
                and not candidate_robots & configured
            )
        ]
        for entry in entries:
            matching_urls = _shared_aliases(base_url, aliases, entry_aliases(entry))
            if matching_urls or set(entry.data.get("robot_ids", [])) & candidate_robots:
                return entry
        origin = URL(base_url)
        for entry in entries:
            other_url = entry.data.get(CONF_URL)
            if not isinstance(other_url, str):
                continue
            other = URL(other_url)
            if (origin.scheme, origin.port) != (other.scheme, other.port):
                continue
            candidate_urls = await self._async_resolve_urls(base_url)
            configured_urls = await self._async_resolve_urls(other_url)
            matching_urls = _shared_aliases(
                base_url,
                aliases | candidate_urls,
                entry_aliases(entry) | configured_urls,
            )
            if matching_urls:
                aliases.update(candidate_urls | configured_urls)
                return entry
        return None

    def _remember_aliases(
        self,
        entry: config_entries.ConfigEntry,
        aliases: set[str],
        robot_ids: list[str] | None = None,
    ) -> None:
        """Enrich legacy entries when discovery verifies their alternate origins."""
        data = {
            **entry.data,
            CONF_URL_ALIASES: sorted(entry_aliases(entry) | aliases),
        }
        configured = set(entry.data.get("robot_ids", []))
        # Never replace another server's robot list with an unrelated one.
        if robot_ids and (not configured or configured & set(robot_ids)):
            data["robot_ids"] = robot_ids
        if data != entry.data:
            self.hass.config_entries.async_update_entry(entry, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Discover unconfigured servers before offering a manual address form."""
        if user_input is None:
            self._discovered_servers.clear()
            for server in await async_discover_servers(self.hass):
                try:
                    base_url = discovery_url(server)
                    aliases = discovery_aliases(server)
                except ValueError, TypeError:
                    continue
                if entry := await self._async_duplicate(base_url, [], aliases):
                    self._remember_aliases(entry, aliases)
                    continue
                self._discovered_servers[base_url] = server
            if len(self._discovered_servers) == 1:
                return await self.async_step_zeroconf(
                    next(iter(self._discovered_servers.values()))
                )
            if self._discovered_servers:
                return await self.async_step_select_server()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = normalize_url(user_input[CONF_URL])
                _, robot_ids = await self._validate(base_url)
            except ValueError, TypeError:
                errors[CONF_URL] = "invalid_url"
            except VacuulaError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish(base_url, robot_ids)
        return self._show_address_form(errors)

    def _show_address_form(
        self, errors: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Keep an address fallback when discovery cannot offer a new server."""
        self.context.pop("confirm_only", None)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL, default=self._discovered_url or DEFAULT_URL
                    ): str
                }
            ),
            errors=errors or {},
        )

    async def async_step_select_server(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user choose when several local servers advertise themselves."""
        if user_input is not None:
            return await self.async_step_zeroconf(
                self._discovered_servers[user_input[CONF_URL]]
            )
        return self.async_show_form(
            step_id="select_server",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL): vol.In(sorted(self._discovered_servers))}
            ),
        )

    async def _async_finish(
        self, base_url: str, robot_ids: list[str]
    ) -> config_entries.ConfigFlowResult:
        """Recheck duplicates immediately before creating the entry."""
        aliases = (
            set(self._discovered_aliases)
            if base_url == self._discovered_url
            else {base_url}
        )
        if entry := await self._async_duplicate(base_url, robot_ids, aliases):
            self._remember_aliases(entry, aliases, robot_ids)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(
            base_url, raise_on_progress=self.source != config_entries.SOURCE_USER
        )
        self._abort_if_unique_id_configured()
        data: dict[str, Any] = {CONF_URL: base_url, "robot_ids": robot_ids}
        if len(aliases) > 1:
            data[CONF_URL_ALIASES] = sorted(aliases)
        return self.async_create_entry(title="Vacuula", data=data)

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> config_entries.ConfigFlowResult:
        """Validate an advertisement and reconcile existing server identities.

        Update a known robot server's changed address and reload it when active.
        For manual setup, keep the address form available after discovery fails
        or finds an already configured server.
        """
        host = discovery_info.host
        try:
            self._discovered_url = discovery_url(discovery_info)
            self._discovered_aliases = discovery_aliases(discovery_info)
            _, robot_ids = await self._validate(self._discovered_url)
        except ValueError, TypeError, VacuulaError:
            if self.source == config_entries.SOURCE_USER:
                return self._show_address_form({"base": "cannot_connect"})
            return self.async_abort(reason="cannot_connect")
        if entry := await self._async_duplicate(
            self._discovered_url, robot_ids, self._discovered_aliases
        ):
            address_changed = entry.data.get(CONF_URL) != self._discovered_url and bool(
                set(entry.data.get("robot_ids", [])) & set(robot_ids)
            )
            self._remember_aliases(entry, self._discovered_aliases, robot_ids)
            if address_changed:
                updates: dict[str, Any] = {
                    "data": {**entry.data, CONF_URL: self._discovered_url}
                }
                # An ignored discovery of this address can already hold the
                # unique ID; keep the entry's old one rather than clash with it.
                owner = self.hass.config_entries.async_entry_for_domain_unique_id(
                    DOMAIN, self._discovered_url
                )
                if owner is None or owner.entry_id == entry.entry_id:
                    updates["unique_id"] = self._discovered_url
                self.hass.config_entries.async_update_entry(entry, **updates)
                if entry.state in {
                    ConfigEntryState.LOADED,
                    ConfigEntryState.SETUP_RETRY,
                }:
                    await self.hass.config_entries.async_reload(entry.entry_id)
            if self.source == config_entries.SOURCE_USER:
                return self._show_address_form()
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(
            self._discovered_url,
            raise_on_progress=self.source != config_entries.SOURCE_USER,
        )
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {CONF_HOST: host}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Revalidate a discovered server before creating its confirmed entry."""
        assert self._discovered_url is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                _, robot_ids = await self._validate(self._discovered_url)
            except VacuulaError:
                if self.source == config_entries.SOURCE_USER:
                    return self._show_address_form({"base": "cannot_connect"})
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish(self._discovered_url, robot_ids)
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"server": self._discovered_url},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Validate a replacement address without switching to unrelated robots."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = normalize_url(user_input[CONF_URL])
                _, robot_ids = await self._validate(base_url)
            except ValueError, TypeError:
                errors[CONF_URL] = "invalid_url"
            except VacuulaError:
                errors["base"] = "cannot_connect"
            else:
                aliases = {base_url}
                if await self._async_duplicate(
                    base_url, robot_ids, aliases, ignore_entry_id=entry.entry_id
                ):
                    return self.async_abort(reason="already_configured")
                old_ids = set(entry.data.get("robot_ids", []))
                if old_ids and robot_ids and not old_ids.intersection(robot_ids):
                    errors["base"] = "wrong_server"
                else:
                    if old_ids.intersection(robot_ids) or base_url in entry_aliases(
                        entry
                    ):
                        aliases.update(entry_aliases(entry))
                    owner = self.hass.config_entries.async_entry_for_domain_unique_id(
                        DOMAIN, base_url
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=base_url
                        if owner is None or owner.entry_id == entry.entry_id
                        else entry.unique_id,
                        data_updates={
                            CONF_URL: base_url,
                            "robot_ids": robot_ids,
                            CONF_URL_ALIASES: sorted(aliases),
                        },
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL, default=entry.data[CONF_URL]): str}
            ),
            errors=errors,
        )
