"""Binary sensor platform for WiCAN integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.restore_state import RestoreEntity

from .attributes import BINARY_SENSOR_DESCRIPTIONS, MeatPiBinarySensorEntityDescription, get_sensor_attributes
from .entity import MeatPiEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import MeatPiConfigEntry

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0

TRUE_STRINGS = {"enable", "true", "online"}

def is_true_status(value: Any) -> bool:
    """Interpret the device's truthy strings/scalars as a boolean."""
    if isinstance(value, str):
        return value.strip().lower() in TRUE_STRINGS
    return bool(value)

async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: MeatPiConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    profile = config_entry.runtime_data.device_profile
    async_add_entities(
        [
            MeatPiBinarySensorEntity(config_entry, description)
            for description in BINARY_SENSOR_DESCRIPTIONS
            if profile.supports_obd_pids or not description.requires_obd
        ]
        + [MeatPiConnectedBinarySensorEntity(config_entry)],
    )

class MeatPiBinarySensorEntity(MeatPiEntity, BinarySensorEntity, RestoreEntity):
    """A binary sensor entity."""

    __slots__ = ("_attr_extra_state_attributes", "_attr_is_on")

    entity_description: MeatPiBinarySensorEntityDescription

    def __init__(
        self,
        config_entry: MeatPiConfigEntry,
        entity_description: MeatPiBinarySensorEntityDescription,
    ) -> None:
        super().__init__(config_entry, entity_description)
        self._attr_unique_id = f"{config_entry.entry_id}_{entity_description.key}"
        self._attr_is_on = None
        self._attr_extra_state_attributes = {}

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        key = self.entity_description.key
        data = self.coordinator.data or {}
        status = data.get("status", {})
        if not isinstance(status, dict):
            status = {}

        # If key not present, don't change state. Availability handled below.
        # A non-scalar value (nested object/array from a glitching device)
        # says nothing about on/off, so keep the last known state.
        if key in status and isinstance(status[key], (str, bool, int, float)):
            self._attr_is_on = is_true_status(status[key])
            self._attr_extra_state_attributes = get_sensor_attributes(
                self.entity_description, data,
            )

        # Availability: if we have a status dict, entity is available; if device stopped pushing,
        # HA will keep last state, but we still emit state writes on updates to ensure logbook records.
        # Write state to Home Assistant.
        self.async_write_ha_state()

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (backward compatibility)."""
        # Coordinator update will trigger _handle_coordinator_update()

    async def async_added_to_hass(self) -> None:
        """Restore entity state."""
        # Restore last known state so logbook has a baseline before first push
        last_state = await self.async_get_last_state()
        if last_state is not None and self._attr_is_on is None:
            self._attr_is_on = last_state.state == "on"
        await super().async_added_to_hass()


class MeatPiConnectedBinarySensorEntity(MeatPiEntity, BinarySensorEntity):
    """Whether the device is currently pushing data.

    Always available, like Last seen: it is the one entity that says the
    device is away, which matters most with the keep-last-values option
    on, when the readings stay on their last value instead of going
    unavailable. Unknown until the device has been heard from at all.
    """

    __slots__ = ()

    def __init__(self, config_entry: MeatPiConfigEntry) -> None:
        super().__init__(
            config_entry,
            MeatPiBinarySensorEntityDescription(
                key="connected",
                translation_key="connected",
                device_class=BinarySensorDeviceClass.CONNECTIVITY,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    @property
    def available(self) -> bool:
        """Stay available while the device is away: that is what it reports."""
        return True

    @property
    def is_on(self) -> bool | None:
        """Return whether pushes are arriving (None before the first one)."""
        return self.coordinator.device_online

    @callback
    def _async_handle_event(self, webhook_id: str, data: dict[str, str]) -> None:
        """Handle webhook event (this sensor follows the coordinator)."""
