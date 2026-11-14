"""Device tracker platform for WiCAN integration.

This platform tracks the GPS location of the WiCAN device (typically in a vehicle).
"""

from __future__ import annotations

import contextlib
import logging
from math import isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.components.device_tracker.const import SourceType
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    GPS_ACCURACY_THRESHOLD,
    MAX_GPS_LATITUDE,
    MAX_GPS_LONGITUDE,
    MIN_GPS_LATITUDE,
    MIN_GPS_LONGITUDE,
)
from .coordinator import MeatPiDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MeatPiConfigEntry

_LOGGER = logging.getLogger(__name__)

# Push-based integration; entities are read-only and never poll the device.
PARALLEL_UPDATES = 0


def _as_finite_float(value: Any) -> float | None:
    """Best-effort float conversion, rejecting bools and non-finite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MeatPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device tracker platform.

    Creates a single device_tracker entity that represents the GPS location
    of the WiCAN device (typically mounted in a vehicle).
    """

    # Always create the tracker entity - it will show as unavailable if no GPS data
    entity = MeatPiDeviceTrackerEntity(config_entry)
    async_add_entities([entity])

    _LOGGER.debug("Device tracker entity created for %s", config_entry.title)


class MeatPiDeviceTrackerEntity(
    CoordinatorEntity[MeatPiDataUpdateCoordinator], TrackerEntity, RestoreEntity,
):
    """Represents the GPS location of the WiCAN device.

    This entity tracks the physical location of the WiCAN device using
    GPS coordinates from the device's webhook updates.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "location"
    _attr_icon = "mdi:map-marker"

    def __init__(self, config_entry: MeatPiConfigEntry) -> None:
        """Initialize the device tracker entity."""
        # Initialize CoordinatorEntity directly, not MeatPiEntity (which requires entity_description)
        CoordinatorEntity.__init__(self, config_entry.runtime_data.coordinator)

        self.config_entry = config_entry
        self.webhook_id = config_entry.runtime_data.webhook_id

        # Unique ID based on config entry
        self._attr_unique_id = f"{config_entry.entry_id}_device_tracker"

        # GPS state
        self._attr_latitude: float | None = None
        self._attr_longitude: float | None = None
        self._attr_location_accuracy: int = 0

        # Additional attributes
        self._altitude: float | None = None
        self._speed: float | None = None
        self._heading: float | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        info = self.config_entry.data
        device_id = info.get("device_id") or self.config_entry.entry_id
        config_url = info.get("mdns")
        if not isinstance(config_url, str) or not config_url.startswith("http"):
            config_url = None

        # Derive manufacturer/model exactly like MeatPiEntity.device_info:
        # the tracker registers against the same device, and a mismatch
        # makes the registry entry flip-flop depending on which entity
        # registered last (a catalog-branded product showed "MeatPi").
        profile = self.config_entry.runtime_data.device_profile
        device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=profile.manufacturer,
            model=info.get("hw_version") or profile.model,
            # Match the device name used by every other WiCAN entity so the
            # tracker shares their device. Existing installs keep their
            # already-registered entity_id (HA pins it at creation); only new
            # installs pick up the consistent, title-based entity_id.
            name=self.config_entry.title,
            sw_version=info.get("fw_version", "Unknown"),
            configuration_url=config_url,
        )

        mac_address = info.get("mac")
        if mac_address:
            device_info["connections"] = {(CONNECTION_NETWORK_MAC, mac_address)}

        if info.get("device_id"):
            device_info["serial_number"] = info.get("device_id")

        return device_info

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

        Entity is available if we have valid GPS coordinates and the
        device is pushing. With the keep-last-values option on, the last
        known location stays while the device is away: where a parked
        car is matters most exactly then.
        """
        return (
            (self.coordinator.last_update_success or self.coordinator.keep_last_values)
            and self._attr_latitude is not None
            and self._attr_longitude is not None
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data or {}
        gps_data = data.get("gps", {})

        if isinstance(gps_data, dict) and gps_data:
            # Every field is parsed independently so one malformed field can
            # never take the others (or the whole tracker) down with it.
            self._apply_gps_fix(gps_data)
        else:
            _LOGGER.debug("No GPS data in coordinator update")

        # Written even without a fix in this notification: availability
        # flips (the staleness health-check, the keep-last-values option)
        # must render on a tracker showing a restored location too.
        self.async_write_ha_state()

    def _apply_gps_fix(self, gps_data: dict[str, Any]) -> None:
        """Apply a GPS fix from the device, ignoring anything unusable."""
        lat = _as_finite_float(gps_data.get("latitude"))
        lon = _as_finite_float(gps_data.get("longitude"))

        if lat is None or lon is None:
            if gps_data.get("latitude") is not None or gps_data.get("longitude") is not None:
                _LOGGER.debug(
                    "Ignoring GPS fix with unusable coordinates: lat=%r, lon=%r",
                    gps_data.get("latitude"),
                    gps_data.get("longitude"),
                )
            return

        if not (
            MIN_GPS_LATITUDE <= lat <= MAX_GPS_LATITUDE
            and MIN_GPS_LONGITUDE <= lon <= MAX_GPS_LONGITUDE
        ):
            _LOGGER.warning(
                "Invalid GPS coordinates: lat=%s, lon=%s (out of range)", lat, lon,
            )
            return

        accuracy = _as_finite_float(gps_data.get("accuracy"))
        accuracy_m = int(accuracy) if accuracy is not None and accuracy > 0 else 0

        # A very poor fix (cold start, parking garage) must not teleport an
        # already-located vehicle; accept anything when we have no location yet.
        if (
            self._attr_latitude is not None
            and accuracy_m > GPS_ACCURACY_THRESHOLD
        ):
            _LOGGER.debug(
                "Ignoring low-accuracy GPS fix (%sm > %sm threshold)",
                accuracy_m,
                GPS_ACCURACY_THRESHOLD,
            )
            return

        self._attr_latitude = lat
        self._attr_longitude = lon
        self._attr_location_accuracy = accuracy_m

        # Optional attributes: unusable values clear the attribute rather than
        # retaining a stale reading from a previous fix.
        self._altitude = _as_finite_float(gps_data.get("altitude"))
        self._speed = _as_finite_float(gps_data.get("speed"))
        self._heading = _as_finite_float(gps_data.get("heading"))

        _LOGGER.debug(
            "Updated GPS location: %s, %s (accuracy: %sm)",
            self._attr_latitude,
            self._attr_longitude,
            self._attr_location_accuracy,
        )

    async def async_added_to_hass(self) -> None:
        """Restore last known location when entity is added."""
        await super().async_added_to_hass()

        # Restore last known GPS location
        last_state = await self.async_get_last_state()
        if last_state:
            # Restore coordinates as a validated pair: a corrupt restore
            # (lat=999, NaN, one coordinate missing) gets the same range
            # gate as a live fix instead of resurrecting an impossible
            # location as an available tracker.
            lat = _as_finite_float(last_state.attributes.get("latitude"))
            lon = _as_finite_float(last_state.attributes.get("longitude"))
            if (
                lat is not None
                and lon is not None
                and MIN_GPS_LATITUDE <= lat <= MAX_GPS_LATITUDE
                and MIN_GPS_LONGITUDE <= lon <= MAX_GPS_LONGITUDE
            ):
                self._attr_latitude = lat
                self._attr_longitude = lon

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
