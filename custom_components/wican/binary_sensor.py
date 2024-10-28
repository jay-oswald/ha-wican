from .const import DOMAIN
from .entity import WiCanStatusEntity, WiCanPidEntity

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from homeassistant.const import EntityCategory, STATE_ON, STATE_OFF


def binary_state(target_state: str):
    return lambda state: STATE_ON if state == target_state else STATE_OFF


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.data["status"] == False:
        return

    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "ble_status",
                "name": "Bluetooth Status",
                "category": EntityCategory.DIAGNOSTIC,
            },
            binary_state("enable"),
        )
    )
    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "sleep_status",
                "name": "Sleep Status",
                "category": EntityCategory.DIAGNOSTIC,
                "attributes": {"voltage": "sleep_volt"},
            },
            binary_state("enable"),
        )
    )
    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "mqtt_en",
                "name": "MQTT Status",
                "category": EntityCategory.DIAGNOSTIC,
                "attributes": {
                    "url": "mqtt_url",
                    "port": "mqtt_port",
                    "user": "mqtt_user",
                },
            },
            binary_state("enable"),
        )
    )
    entities.append(
        WiCanStatusEntity(
            coordinator,
            {
                "key": "ecu_status",
                "name": "ECU Status",
                "category": EntityCategory.DIAGNOSTIC,
                "target_state": "online",
            },
            binary_state("online"),
        )
    )

    if not coordinator.ecu_online:
        async_add_entities(entities)

    if not coordinator.data["pid"]:
        return async_add_entities(entities)

    for key in coordinator.data["pid"]:
        if coordinator.data["pid"][key]["sensor_type"] == "binary_sensor":
            entities.append(
                WiCanPidEntity(
                    coordinator,
                    {
                        "key": key,
                        "name": key,
                        "class": coordinator.data["pid"][key]["class"],
                    },
                    binary_state("on"),
                )
            )

    return async_add_entities(entities)
