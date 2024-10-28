import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .wican import WiCan
from .const import DOMAIN
from homeassistant.const import CONF_IP_ADDRESS, Platform

from .coordinator import WiCanCoordinator

PLATFORMS: list[str] = [Platform.BINARY_SENSOR, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    wican = WiCan(entry.data[CONF_IP_ADDRESS])

    coordinator = WiCanCoordinator(hass, wican)

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
