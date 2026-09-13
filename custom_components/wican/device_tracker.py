"""Device tracker platform for WiCAN integration.

This platform tracks the GPS location of the WiCAN device (typically in a vehicle).
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import WiCANConfigEntry

_LOGGER = logging.getLogger(__name__)

# Entity will be named "WiCAN Device Location" with has_entity_name=True
TRACKER_NAME = "Location"

# Persisted once a device proves it can report a fix, so a firmware update that
# adds GPS to a model this integration otherwise assumes can't isn't permanently
# missing a tracker.
CONF_HAS_REPORTED_GPS = "has_reported_gps"


def _expects_gps(config_entry: WiCANConfigEntry) -> bool:
    """Return whether to create the tracker up front, or wait for a real fix.

    Creating it unconditionally left it permanently "unknown" on WiCAN-OBD and
    -USB hardware: no current firmware build populates the webhook's "gps" key
    on those models, only WiCAN-PRO ships a GPS module.

    This is an allow-list on "pro", not a deny-list on "obd"/"usb", for two
    reasons:

    * The firmware reports hw_version as "WiCAN-" + one of "OBD" / "USB" /
      "OBD-PRO" (wican-fw CMakeLists.txt). A deny-list containing "obd" matches
      "WiCAN-OBD-PRO" too, so it skipped the tracker on the one model that
      actually has GPS.
    * **hw_version is not in config_entry.data when this runs on a fresh
      install.** Only the webhook handler writes it, from the first POST, and
      the platforms are set up well before that. A deny-list therefore saw an
      empty string, fell through to "create it", and every device got the
      permanently-unavailable entity this was meant to prevent - which survived
      as an orphaned registry entry even after hw_version arrived.

    An unknown hw_version now waits for a fix instead of guessing. A PRO that
    has not been configured before gets its tracker one webhook late, and
    eagerly on every start after that.

    "pro" in hw_version is how __init__.py and update.py already identify a PRO.
    """
    if config_entry.data.get(CONF_HAS_REPORTED_GPS):
        return True

    return "pro" in str(config_entry.data.get("hw_version", "")).lower()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: WiCANConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform.

    Creates a single device_tracker entity that represents the GPS location
    of the WiCAN device (typically mounted in a vehicle).
    """
    if _expects_gps(config_entry):
        async_add_entities([WiCANDeviceTrackerEntity(config_entry)])
        _LOGGER.debug("Device tracker entity created for %s", config_entry.title)
        return

    _LOGGER.debug(
        "Deferring device tracker for %s (hw_version=%r) until a GPS fix arrives",
        config_entry.title,
        config_entry.data.get("hw_version"),
    )

    coordinator = config_entry.runtime_data.coordinator

    # The coordinator remover raises KeyError if called twice, and this one is
    # reachable from two places: the callback below, once a fix has arrived, and
    # the entry unload. Collapse them into one idempotent call - an exception
    # raised out of _async_process_on_unload() aborts the unload, so the entry
    # could no longer be reloaded, reconfigured or deleted.
    unsub: CALLBACK_TYPE | None = None

    @callback
    def _unsubscribe() -> None:
        nonlocal unsub
        if unsub is not None:
            unsub()
            unsub = None

    @callback
    def _create_tracker_on_first_fix() -> None:
        gps_data = coordinator.data.get("gps", {}) if coordinator.data else {}
        if gps_data.get("latitude") is None or gps_data.get("longitude") is None:
            return

        _unsubscribe()
        new_data = dict(config_entry.data)
        new_data[CONF_HAS_REPORTED_GPS] = True
        hass.config_entries.async_update_entry(config_entry, data=new_data)

        async_add_entities([WiCANDeviceTrackerEntity(config_entry)])
        _LOGGER.debug(
            "First GPS fix received from %s; device tracker created",
            config_entry.title,
        )

    unsub = coordinator.async_add_listener(_create_tracker_on_first_fix)
    config_entry.async_on_unload(_unsubscribe)


class WiCANDeviceTrackerEntity(CoordinatorEntity, TrackerEntity, RestoreEntity):
    """Represents the GPS location of the WiCAN device.

    This entity tracks the physical location of the WiCAN device using
    GPS coordinates from the device's webhook updates.
    """

    _attr_has_entity_name = True
    _attr_name = TRACKER_NAME
    _attr_icon = "mdi:map-marker"

    def __init__(self, config_entry: WiCANConfigEntry) -> None:
        """Initialize the device tracker entity."""
        # Initialize CoordinatorEntity directly, not WiCANEntity (which requires entity_description)
        CoordinatorEntity.__init__(self, config_entry.runtime_data.coordinator)

        self.config_entry = config_entry
        self.webhook_id = config_entry.runtime_data.webhook_id

        # Unique ID based on config entry
        self._attr_unique_id = f"{config_entry.entry_id}_device_tracker"

        # GPS state
        self._attr_latitude: float | None = None
        self._attr_longitude: float | None = None
        self._attr_location_accuracy: int = 0
        self._attr_location_name: str | None = None

        # Additional attributes
        self._altitude: float | None = None
        self._speed: float | None = None
        self._heading: float | None = None

    @property
    def device_info(self):
        """Return device info for this entity."""
        info = self.config_entry.data
        device_id = info.get("device_id") or self.config_entry.entry_id
        config_url = info.get("mdns")
        if not isinstance(config_url, str) or not config_url.startswith("http"):
            config_url = None

        device_info_dict = {
            "identifiers": {(DOMAIN, device_id)},
            "manufacturer": "MeatPi",
            "model": info.get("hw_version", "Unknown"),
            "name": "WiCAN Device",
            "sw_version": info.get("fw_version", "Unknown"),
            "configuration_url": config_url,
        }

        mac_address = info.get("mac")
        if mac_address:
            device_info_dict["connections"] = {(CONNECTION_NETWORK_MAC, mac_address)}

        if info.get("device_id"):
            device_info_dict["serial_number"] = info.get("device_id")

        return DeviceInfo(**device_info_dict)

    @property
    def source_type(self) -> SourceType:
        """Return the source type (GPS)."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        return self._attr_latitude

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        return self._attr_longitude

    @property
    def location_accuracy(self) -> int:
        """Return the location accuracy in meters."""
        return self._attr_location_accuracy

    @property
    def location_name(self) -> str | None:
        """Return the name of the current location (zone name if in zone)."""
        return self._attr_location_name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        attrs = {}

        if self._altitude is not None:
            attrs["altitude"] = self._altitude
        if self._speed is not None:
            attrs["speed"] = self._speed
        if self._heading is not None:
            attrs["heading"] = self._heading

        return attrs

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Entity is available if we have valid GPS coordinates.
        """
        return (
            self.coordinator.last_update_success
            and self._attr_latitude is not None
            and self._attr_longitude is not None
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        gps_data = self.coordinator.data.get("gps", {})

        if not gps_data:
            # No GPS data available
            _LOGGER.debug("No GPS data in coordinator update")
            return

        # Update GPS coordinates
        latitude = gps_data.get("latitude")
        longitude = gps_data.get("longitude")

        if latitude is not None and longitude is not None:
            try:
                # Validate coordinates are within valid ranges
                lat = float(latitude)
                lon = float(longitude)

                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    self._attr_latitude = lat
                    self._attr_longitude = lon

                    # Update accuracy (default to 0 if not provided)
                    accuracy = gps_data.get("accuracy", 0)
                    self._attr_location_accuracy = int(accuracy) if accuracy else 0

                    # Update optional attributes
                    self._altitude = gps_data.get("altitude")
                    if self._altitude is not None:
                        self._altitude = float(self._altitude)

                    self._speed = gps_data.get("speed")
                    if self._speed is not None:
                        self._speed = float(self._speed)

                    self._heading = gps_data.get("heading")
                    if self._heading is not None:
                        self._heading = float(self._heading)

                    _LOGGER.debug(
                        "Updated GPS location: %s, %s (accuracy: %sm)",
                        self._attr_latitude,
                        self._attr_longitude,
                        self._attr_location_accuracy,
                    )
                else:
                    _LOGGER.warning(
                        "Invalid GPS coordinates: lat=%s, lon=%s (out of range)",
                        lat,
                        lon,
                    )
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Failed to parse GPS data: %s", err)

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore last known location when entity is added."""
        await super().async_added_to_hass()

        # Restore last known GPS location
        last_state = await self.async_get_last_state()
        if last_state:
            # Restore coordinates
            if "latitude" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._attr_latitude = float(last_state.attributes["latitude"])

            if "longitude" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._attr_longitude = float(last_state.attributes["longitude"])

            # Restore accuracy
            if "gps_accuracy" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._attr_location_accuracy = int(last_state.attributes["gps_accuracy"])

            # Restore optional attributes
            if "altitude" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._altitude = float(last_state.attributes["altitude"])

            if "speed" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._speed = float(last_state.attributes["speed"])

            if "heading" in last_state.attributes:
                with contextlib.suppress(ValueError, TypeError):
                    self._heading = float(last_state.attributes["heading"])

            _LOGGER.debug(
                "Restored GPS location: %s, %s",
                self._attr_latitude,
                self._attr_longitude,
            )
