"""Base entity for the MeatPi integration."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import MeatPiDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription

    from . import MeatPiConfigEntry


class MeatPiEntity(CoordinatorEntity[MeatPiDataUpdateCoordinator]):
    """Base entity using DataUpdateCoordinator."""

    _attr_has_entity_name = True

    # Entities that carry a device reading set this. With the
    # keep-last-values option on they stay available on their last value
    # while the device is away. Entities that act on the device or describe
    # its live links leave it off and always follow the device.
    _keeps_last_value = False

    def __init__(
        self,
        config_entry: MeatPiConfigEntry,
        entity_description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(config_entry.runtime_data.coordinator)
        self.config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_{entity_description.key}"
        self.entity_description = entity_description
        self.webhook_id = config_entry.runtime_data.webhook_id
        # An explicit description name (dynamic PID sensors, catalog-defined
        # sensors) wins. Static descriptions carry a translation_key and
        # MUST NOT get _attr_name set — Entity resolves _attr_name before
        # consulting platform translations, so setting it here would show
        # users raw keys like "batt_voltage" instead of "Battery Voltage".
        description_name = entity_description.name
        if isinstance(description_name, str):
            self._attr_name = description_name
        elif not getattr(entity_description, "translation_key", None):
            # Neither an explicit name nor a translation: fall back to the
            # key rather than leaving the entity unnamed.
            self._attr_name = entity_description.key
        self._attr_device_info = DeviceInfo(
            connections={(DOMAIN, config_entry.entry_id)},
            manufacturer=MANUFACTURER,
            model=config_entry.runtime_data.device_profile.model,
            name=config_entry.title,
        )

    @abstractmethod
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle the WiCAN event.

        This method is kept for backward compatibility during migration.
        Subclasses should override _handle_coordinator_update() instead.
        """

    async def async_added_to_hass(self) -> None:
        """Register event callback."""
        await super().async_added_to_hass()

        # Keep dispatcher for backward compatibility during migration
        @callback
        def _handle_event_filtered(webhook_id: str, data: dict[str, str]) -> None:
            if webhook_id != self.webhook_id:
                return
            self._async_handle_event(webhook_id, data)

        self.async_on_remove(
            async_dispatcher_connect(self.hass, DOMAIN, _handle_event_filtered),
        )

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Default implementation - subclasses should override this
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Follow the device, unless this entity keeps its last value."""
        return super().available or (
            self._keeps_last_value and self.coordinator.keep_last_values
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        info = self.config_entry.data
        config_url = info.get("mdns")
        if not isinstance(config_url, str) or not config_url.startswith("http"):
            config_url = None

        # Use device_id or MAC as stable identifier (survives hostname changes)
        device_id = info.get("device_id") or self.config_entry.entry_id

        # The device-reported hardware version is the most precise model
        # string; fall back to the profile's display model until it arrives.
        profile = self.config_entry.runtime_data.device_profile
        device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=profile.manufacturer,
            model=info.get("hw_version") or profile.model,
            name=self.config_entry.title,
            sw_version=info.get("fw_version", "Unknown"),
            configuration_url=config_url,
        )

        # Add MAC address connection if available (from firmware)
        mac_address = info.get("mac")
        if mac_address:
            device_info["connections"] = {(CONNECTION_NETWORK_MAC, mac_address)}

        # Add serial number if device_id available
        if info.get("device_id"):
            device_info["serial_number"] = info.get("device_id")

        return device_info
