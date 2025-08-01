"""Constants for WiCAN integration."""

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityDescription

DOMAIN = "wican"
CONF_DEFAULT_SCAN_INTERVAL = 30


@dataclass(kw_only=True)
class WiCanEntityDescription(EntityDescription):
    """Base class for WiCAN Entity Descriptions."""

    attributes: dict | None = None
    target_state: str | None = None
    process_status_voltage: bool | None = None


@dataclass(kw_only=True)
class WiCanBinarySensorEntityDescription(
    BinarySensorEntityDescription, WiCanEntityDescription
):
    """Data class for WiCAN Binary Sensor Entity Descriptions."""


@dataclass(kw_only=True)
class WiCanSensorEntityDescription(SensorEntityDescription, WiCanEntityDescription):
    """Data class for WiCAN Sensor Entity Descriptions."""


STATUS_BINARY_SENSORS = [
    WiCanBinarySensorEntityDescription(
        key="ble_status",
        translation_key="ble_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bluetooth",
        target_state="enable",
    ),
    WiCanBinarySensorEntityDescription(
        key="sleep_status",
        translation_key="sleep_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:power-sleep",
        attributes={"voltage": "sleep_volt"},
        target_state="enable",
    ),
    WiCanBinarySensorEntityDescription(
        key="batt_alert",
        translation_key="batt_alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:battery-alert",
        attributes={
            "wifi": "batt_alert_ssid",
            "voltage": "batt_alert_volt",
            "url": "batt_alert_url",
            "port": "batt_alert_port",
            "user": "batt_mqtt_user",
        },
        target_state="enable",
    ),
    WiCanBinarySensorEntityDescription(
        key="mqtt_en",
        translation_key="mqtt_en",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:broadcast",
        attributes={
            "url": "mqtt_url",
            "port": "mqtt_port",
            "user": "mqtt_user",
        },
        target_state="enable",
    ),
    WiCanBinarySensorEntityDescription(
        key="ecu_status",
        translation_key="ecu_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:chip",
        target_state="online",
    ),
]

STATUS_SENSORS = [
    WiCanSensorEntityDescription(
        key="batt_voltage",
        translation_key="batt_voltage",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement="V",
        icon="mdi:battery-charging",
        process_status_voltage=True,
    ),
    WiCanSensorEntityDescription(
        key="sta_ip",
        translation_key="sta_ip",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ip-network",
    ),
    WiCanSensorEntityDescription(
        key="protocol",
        translation_key="protocol",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:protocol",
    ),
]

DEVICE_CLASSES_SENSORS = {
    "battery": SensorDeviceClass.BATTERY,
    "current": SensorDeviceClass.CURRENT,
    "distance": SensorDeviceClass.DISTANCE,
    "frequency": SensorDeviceClass.FREQUENCY,
    "power": SensorDeviceClass.POWER,
    "pressure": SensorDeviceClass.PRESSURE,
    "speed": SensorDeviceClass.SPEED,
    "temperature": SensorDeviceClass.TEMPERATURE,
    "voltage": SensorDeviceClass.VOLTAGE,
    "none": None,
}
