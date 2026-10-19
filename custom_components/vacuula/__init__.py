"""Local Home Assistant integration for the Vacuula server."""

from copy import deepcopy

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import VacuulaClient, VacuulaError
from .const import CONF_URL, DOMAIN, PLATFORMS
from .coordinator import VacuulaCoordinator
from .services import async_setup_services

type VacuulaConfigEntry = ConfigEntry[VacuulaCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions even when a server is temporarily unavailable."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: VacuulaConfigEntry) -> bool:
    """Do not reinterpret an old house default as consent to ignore saved maps."""
    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        options = deepcopy(dict(entry.options))
        cleaning_options = options.get("cleaning_options", {})
        if isinstance(cleaning_options, dict):
            for defaults in cleaning_options.values():
                if isinstance(defaults, dict) and defaults.get("category") == "house":
                    defaults["category"] = "auto"
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VacuulaConfigEntry) -> bool:
    """Validate the connection before loading entity platforms."""
    client = VacuulaClient(async_get_clientsession(hass), entry.data[CONF_URL])
    coordinator = VacuulaCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VacuulaConfigEntry) -> bool:
    """Release subscriptions and platforms without closing HA's shared session."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: VacuulaConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow removing robots no longer registered on this server."""
    coordinator = getattr(entry, "runtime_data", None)
    client = (
        coordinator.client
        if coordinator is not None
        else VacuulaClient(async_get_clientsession(hass), entry.data[CONF_URL])
    )
    try:
        robots = await client.async_robots()
    except VacuulaError:
        return False
    return not any(
        domain == DOMAIN and serial in robots
        for domain, serial in device_entry.identifiers
    )
