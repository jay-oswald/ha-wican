"""Initialize WiCan Integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import WiCanCoordinator
from .wican import WiCan

PLATFORMS: list[str] = [Platform.BINARY_SENSOR, Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """WiCan entry in HomeAssistant.

    Parameters
    ----------
    hass : HomeAssistant
        HomeAssistant object.
    entry: ConfigEntry
        WiCan configuration entry in HomeAssistant.

    Returns
    -------
    bool
        Returns True after platforms have been loaded for integration.

    """

    wican = WiCan(entry.data[CONF_IP_ADDRESS])

    coordinator = WiCanCoordinator(hass, entry, wican)

    # Preload snapshot if available so entities can be created in offline restarts
    try:
        await coordinator.async_preload_snapshot()
    except Exception:
        # Snapshot preload failures should not block setup; coordinator handles fallback
        _LOGGER.debug("Snapshot preload skipped due to error", exc_info=True)

    # First refresh may use live data or the preloaded snapshot; if neither
    # is available, the coordinator raises ConfigEntryNotReady to trigger retry
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload WiCan integration.

    Parameters
    ----------
    hass : HomeAssistant
        HomeAssistant object.
    entry: ConfigEntry
        WiCan configuration entry in HomeAssistant.

    Returns
    -------
    bool
        If integration has been unloaded successfully.

    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
