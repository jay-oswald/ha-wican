"""Sensor processing for WiCAN Integration.

Purpose: provide sensor data for available WiCAN sensors.
"""

import logging

from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant

from .const import (
    DEVICE_CLASSES_SENSORS,
    DOMAIN,
    STATUS_SENSORS,
    WiCanSensorEntityDescription,
)
from .entity import WiCanPidEntity, WiCanStatusEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Create and provide list of sensors containing WiCanStatusEntities and WiCanPidEntities.

    Parameters
    ----------
    hass : HomeAssistant
        HomeAssistant object for coordinator.
    entry: Any
        WiCan entry in HomeAssistant data for coordinator.
    async_add_entities: Any
        Object to be called with list of WiCanEntities.

    Returns
    -------
    async_add_entities: method:
        Calls function async_add_entities containing newly created list of WiCanEntities.

    """
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if not coordinator.data["status"]:
        return None

    entities.extend(
        WiCanStatusEntity(coordinator, description) for description in STATUS_SENSORS
    )

    if not coordinator.ecu_online:
        async_add_entities(entities)

    if not coordinator.data["pid"]:
        return async_add_entities(entities)

    for key in coordinator.data["pid"]:
        append = False
        if coordinator.data["pid"][key].get("sensor_type") is not None:
            if coordinator.data["pid"][key]["sensor_type"] != "binary_sensor":
                append = True
        else:
            append = True

        if append:
            translated_device_class = DEVICE_CLASSES_SENSORS.get(
                coordinator.data["pid"][key]["class"]
            )
            entities.append(
                WiCanPidEntity(
                    coordinator,
                    WiCanSensorEntityDescription(
                        key=key,
                        name=key,
                        device_class=translated_device_class,
                        unit_of_measurement=coordinator.data["pid"][key]["unit"],
                        state_class=SensorStateClass.MEASUREMENT,
                    ),
                )
            )

    return async_add_entities(entities)
