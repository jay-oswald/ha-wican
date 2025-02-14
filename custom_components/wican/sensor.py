import logging
from homeassistant.components.number import NumberEntity, NumberDeviceClass

from homeassistant.const import (
    EntityCategory,
)
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .entity import WiCanStatusEntity, WiCanPidEntity

_LOGGER = logging.getLogger(__name__)


def process_status_voltage(i):
    return float(i[:-1])


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.data["status"] == False:
        return

    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "batt_voltage",
                "name": "Battery Voltage",
                "class": NumberDeviceClass.VOLTAGE,
                "unit": "V",
                "category": EntityCategory.DIAGNOSTIC,
            },
            process_status_voltage,
        )
    )
    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "sta_ip",
                "name": "IP Address",
                "category": EntityCategory.DIAGNOSTIC,
            },
        )
    )
    entities.append(
        WiCanStatusEntity(
            coordinator,
            {"key": "protocol", "name": "Mode", "category": EntityCategory.DIAGNOSTIC},
        )
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
            entities.append(
                WiCanPidEntity(
                    coordinator,
                    {
                        "key": key,
                        "name": key,
                        "class": coordinator.data["pid"][key]["class"],
                        "unit": coordinator.data["pid"][key]["unit"],
                    },
                )
            )

    return async_add_entities(entities)
