"""UI setup, network discovery and reconfiguration for Vacuula."""

from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from yarl import URL

from .client import VacuulaClient, VacuulaError
from .const import CONF_URL, DEFAULT_URL, DOMAIN


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
    return str(url.origin())


class VacuulaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one server, automatically discovering its robots."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_url: str | None = None

    async def _validate(self, base_url: str) -> tuple[dict[str, Any], list[str]]:
        client = VacuulaClient(async_get_clientsession(self.hass), base_url)
        info = await client.async_server_info()
        robots = await client.async_robots()
        return info, sorted(robots)

    def _duplicate(self, base_url: str, robot_ids: list[str]) -> bool:
        return any(
            entry.data.get(CONF_URL) == base_url
            or bool(set(entry.data.get("robot_ids", [])) & set(robot_ids))
            for entry in self._async_current_entries()
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
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
                if self._duplicate(base_url, robot_ids):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Vacuula",
                    data={CONF_URL: base_url, "robot_ids": robot_ids},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_URL, default=self._discovered_url or DEFAULT_URL
                    ): str
                }
            ),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> config_entries.ConfigFlowResult:
        scheme = discovery_info.properties.get("protocol", "http")
        host = discovery_info.host
        self._discovered_url = str(
            URL.build(
                scheme=scheme if scheme in {"http", "https"} else "http",
                host=host,
                port=discovery_info.port,
            )
        )
        try:
            _, robot_ids = await self._validate(self._discovered_url)
        except VacuulaError:
            return self.async_abort(reason="cannot_connect")
        if self._duplicate(self._discovered_url, robot_ids):
            for entry in self._async_current_entries():
                if entry.data.get(CONF_URL) != self._discovered_url and set(
                    entry.data.get("robot_ids", [])
                ) & set(robot_ids):
                    self.hass.config_entries.async_update_entry(
                        entry,
                        unique_id=self._discovered_url,
                        data={
                            **entry.data,
                            CONF_URL: self._discovered_url,
                            "robot_ids": robot_ids,
                        },
                    )
                    if entry.state in {
                        ConfigEntryState.LOADED,
                        ConfigEntryState.SETUP_RETRY,
                    }:
                        await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(self._discovered_url)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {CONF_HOST: host}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_user({CONF_URL: self._discovered_url})
        return self.async_show_form(step_id="confirm")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
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
                if any(
                    other.entry_id != entry.entry_id
                    and (
                        other.data.get(CONF_URL) == base_url
                        or bool(set(other.data.get("robot_ids", [])) & set(robot_ids))
                    )
                    for other in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                old_ids = set(entry.data.get("robot_ids", []))
                if old_ids and robot_ids and not old_ids.intersection(robot_ids):
                    errors["base"] = "wrong_server"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=base_url,
                        data_updates={CONF_URL: base_url, "robot_ids": robot_ids},
                    )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {vol.Required(CONF_URL, default=entry.data[CONF_URL]): str}
            ),
            errors=errors,
        )
