"""Binary sensor processing for WiCAN Integration.

Purpose: provide binary sensor data for available WiCAN sensors.
"""

from homeassistant.core import HomeAssistant

from .const import DOMAIN, STATUS_BINARY_SENSORS, WiCanBinarySensorEntityDescription
from .entity import WiCanPidEntity, WiCanStatusEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Create and provide list of binary sensors containing WiCanStatusEntities and WiCanPidEntities.

    Parameters
    ----------
    hass : HomeAssistant
        HomeAssistant object for coordinator.
    entry: any
        WiCAN entry in HomeAssistant data for coordinator.
    async_add_entities: any
        Object to be called with list of WiCanEntities.

    Returns
    -------
    async_add_entities: function:
        Calls function async_add_entities containing newly created list of WiCanEntities.

    """
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if not coordinator.data["status"]:
        return None

    entities.extend(
        WiCanStatusEntity(coordinator, description)
        for description in STATUS_BINARY_SENSORS
    )

    if not coordinator.ecu_online:
        async_add_entities(entities)

    if not coordinator.data["pid"]:
        return async_add_entities(entities)

    for key in coordinator.data["pid"]:
        if coordinator.data["pid"][key].get("sensor_type") is not None:
            if coordinator.data["pid"][key]["sensor_type"] == "binary_sensor":
                entities.append(
                    WiCanPidEntity(
                        coordinator,
                        WiCanBinarySensorEntityDescription(
                            key=key,
                            name=key,
                            device_class=coordinator.data["pid"][key]["class"],
                            target_state="on",
                        ),
                    )
                )

    return async_add_entities(entities)
