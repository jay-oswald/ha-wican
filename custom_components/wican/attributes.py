from dataclasses import dataclass, field

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntityDescription
from homeassistant.helpers.entity import EntityCategory


@dataclass
class WiCANBinarySensorEntityDescription(BinarySensorEntityDescription):
    extra_attributes: list[str] | None = field(default_factory=list)

@dataclass
class WiCANSensorEntityDescription(SensorEntityDescription):
    extra_attributes: list[str] | None = field(default_factory=list)

SENSOR_DESCRIPTIONS: tuple[WiCANSensorEntityDescription, ...] = (
    WiCANSensorEntityDescription(
        key="wifi_mode",
        translation_key="wifi_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
        extra_attributes=[
            "ap_ch",
            "ap_auto_disable",
            "sta_status",
            "mdns",
        ],
    ),
    WiCANSensorEntityDescription(
        key="batt_voltage",
        translation_key="batt_voltage",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:car-battery",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        suggested_display_precision=1,
        extra_attributes=[
            "batt_alert",
            "batt_alert_volt",
            "batt_alert_protocol",
            "batt_alert_topic",
            "batt_alert_time",
        ],
    ),
    WiCANSensorEntityDescription(
        key="vpn_status",
        translation_key="vpn_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:shield-key",
        extra_attributes=[
            "vpn_ip",
        ],
    ),
    WiCANSensorEntityDescription(
        key="uptime",
        translation_key="uptime",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-outline",
    ),
)

BINARY_SENSOR_DESCRIPTIONS: tuple[WiCANBinarySensorEntityDescription, ...] = (
    WiCANBinarySensorEntityDescription(
        key="ble_status",
        translation_key="ble_status",
        icon="mdi:bluetooth",
        entity_category=EntityCategory.DIAGNOSTIC,
        extra_attributes=[
            "ble_power",
        ],
    ),
    WiCANBinarySensorEntityDescription(
        key="ecu_status",
        translation_key="ecu_status",
        icon="mdi:car-connected",
        entity_category=EntityCategory.DIAGNOSTIC,
        extra_attributes=[
            "obd_chip_status",
        ],
    ),
)

# Not part of BINARY_SENSOR_DESCRIPTIONS: it isn't a status key from the
# device payload, so the generic WiCANBinarySensorEntity has nothing to read
# for it. See WiCANReportingBinarySensorEntity in binary_sensor.py.
REPORTING_BINARY_SENSOR_DESCRIPTION = WiCANBinarySensorEntityDescription(
    key="reporting",
    translation_key="reporting",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)

def get_sensor_attributes(entity_description, data: dict) -> dict:
    status = data.get("status", {})
    attrs = {}
    for attr_key in getattr(entity_description, "extra_attributes", []):
        if status.get(attr_key) is not None:
            attrs[attr_key] = status[attr_key]
    return attrs

