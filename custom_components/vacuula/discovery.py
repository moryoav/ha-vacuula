"""Bounded manual discovery using Home Assistant's shared mDNS instance."""

import asyncio
from contextlib import suppress

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from zeroconf import (
    BadTypeInNameException,
    NamePartTooLongException,
    NotRunningException,
    ServiceStateChange,
    Zeroconf,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

SERVICE_TYPE = "_vacuula._tcp.local."
DISCOVERY_TIMEOUT = 3.0
RESOLVE_TIMEOUT_MS = 1000


async def async_discover_servers(hass: HomeAssistant) -> list[ZeroconfServiceInfo]:
    """Browse advertised server names, cleaning up before returning or cancelling."""
    servers: dict[str, ZeroconfServiceInfo] = {}
    pending: dict[str, asyncio.Task[None]] = {}
    seen: set[str] = set()
    tasks: list[asyncio.Task[None]] = []

    async def async_resolve(service_type: str, name: str) -> None:
        """Resolve one advertisement without letting a bad record break discovery."""
        try:
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(instance, RESOLVE_TIMEOUT_MS):
                if (server := zeroconf.info_from_service(info)) is not None:
                    servers[name] = server
        except (
            BadTypeInNameException,
            NamePartTooLongException,
            NotRunningException,
            OSError,
            ValueError,
        ):
            return

    @callback
    def async_service_update(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Resolve additions once and discard records that disappear during browsing."""
        if service_type != SERVICE_TYPE:
            return
        if state_change is ServiceStateChange.Removed:
            seen.discard(name)
            servers.pop(name, None)
            if task := pending.pop(name, None):
                task.cancel()
            return
        if name in seen:
            return
        seen.add(name)
        pending[name] = asyncio.create_task(async_resolve(service_type, name))
        tasks.append(pending[name])

    browser: AsyncServiceBrowser | None = None
    try:
        instance = await zeroconf.async_get_instance(hass)
        browser = AsyncServiceBrowser(
            instance, SERVICE_TYPE, handlers=[async_service_update]
        )
        await asyncio.sleep(DISCOVERY_TIMEOUT)
    except NotRunningException, OSError:
        return []
    finally:
        if browser is not None:
            with suppress(NotRunningException, OSError):
                await browser.async_cancel()
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return [servers[name] for name in sorted(servers)]
